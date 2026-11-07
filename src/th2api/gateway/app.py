"""FastAPI application: an OpenAI-compatible gateway plus the panel API.

Two surfaces in one process:

* ``/v1/*`` -- what clients talk to.  Requests carry a gateway key
  (``sk-th2api-…``), are placed on a Token Harbor account from the pool, and
  are streamed straight back.
* ``/api/*`` -- what the panel talks to, guarded by the admin token.

The gateway deliberately does not translate protocols.  Token Harbor already
speaks OpenAI, so the value added here is routing, failover, and accounting.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from ..settings import Settings, load_settings
from .accounts import Account, AccountStore, refresh_free_tier
from .db import Database
from .jobs import JobManager
from .pool import Pool, is_free_model, remember_models
from .upstream import Upstream, UpstreamResult, parse_error

MAX_ATTEMPTS = 4
KEEPALIVE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[:2]}***{local[-1]}@{domain}"


def error_body(message: str, *, code: str, status: int, kind: str = "invalid_request_error") -> dict:
    return {"error": {"message": message, "type": kind, "code": code, "status": status}}


class Gateway:
    """Holds the long-lived collaborators; built once per process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.store = AccountStore(settings.accounts_path)
        self.pool = Pool(self.db, self.store)
        self.upstream = Upstream(settings.upstream)
        self.jobs = JobManager(settings.home)
        self.models_cache: tuple[float, list[str]] | None = None

    async def aclose(self) -> None:
        await self.upstream.aclose()
        self.db.close()

    # ---- usage accounting ---------------------------------------------

    def log(self, **fields: Any) -> None:
        try:
            self.db.log_request(**fields)
        except Exception:  # never let bookkeeping break a response
            pass

    # ---- model catalogue ----------------------------------------------

    async def fetch_models(self, account: Account) -> list[str]:
        try:
            response = await self.upstream.send("GET", "/v1/models", account.api_key)
        except httpx.HTTPError:
            return []
        try:
            if response.status_code == 401:
                # The key no longer authenticates; retire it so the pool stops
                # paying for the same 401 on every request.
                self.db.set_enabled(account.email, False)
                self.db.mark_error(account.email, "invalid_api_key on /v1/models")
                return []
            if response.status_code != 200:
                return []
            payload = response.json()
        finally:
            await response.aclose()
        return [
            item.get("id")
            for item in payload.get("data") or []
            if isinstance(item, dict) and item.get("id")
        ]

    async def model_list(self, *, refresh: bool = False) -> list[str]:
        if self.models_cache and not refresh and time.time() - self.models_cache[0] < 300:
            return self.models_cache[1]
        accounts = self.pool.candidates("")
        for account in accounts[:5]:
            models = await self.fetch_models(account)
            if models:
                remember_models(account.email, models)
                self.models_cache = (time.time(), models)
                return models
        return self.models_cache[1] if self.models_cache else []


def _peek_usage(tail: bytes) -> tuple[dict | None, str | None]:
    """Pull ``usage`` and the served model out of the last SSE frames."""
    for line in reversed(tail.split(b"\n")):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            frame = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(frame, dict):
            model = frame.get("model") if isinstance(frame.get("model"), str) else None
            if frame.get("usage"):
                return frame["usage"], model
            if model:
                return None, model
    return None, None


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        gateway = Gateway(resolved)
        gateway.pool.accounts()
        app.state.gateway = gateway
        try:
            yield
        finally:
            await gateway.aclose()

    app = FastAPI(title="TH2API", version="0.2.0", lifespan=lifespan, docs_url=None, redoc_url=None)

    # ------------------------------------------------------------------
    # authentication
    # ------------------------------------------------------------------

    def gw() -> Gateway:
        return app.state.gateway

    def auth_gateway(request: Request) -> dict:
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            raise HTTPException(401, "missing Authorization: Bearer <gateway key>")
        if token == resolved.admin_token:
            return {"id": "admin", "name": "admin"}
        key = gw().db.resolve_gateway_key(token)
        if not key:
            raise HTTPException(401, "invalid or disabled gateway key")
        return key

    def auth_admin(request: Request) -> None:
        token = request.headers.get("x-admin-token") or ""
        if not token:
            header = request.headers.get("authorization") or ""
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token != resolved.admin_token:
            raise HTTPException(401, "admin token required")

    # ------------------------------------------------------------------
    # OpenAI-compatible surface
    # ------------------------------------------------------------------

    async def dispatch(
        request: Request,
        path: str,
        *,
        body: bytes | None,
        stream: bool,
        key_name: str,
        model: str,
    ) -> Response:
        gateway = gw()
        started = time.time()
        candidates = gateway.pool.candidates(model)
        if not candidates:
            reason = "free_models_disabled" if is_free_model(model) else "no_healthy_accounts"
            gateway.log(
                key_name=key_name,
                model=model,
                status=503,
                error=reason,
                stream=stream,
                latency_ms=0,
            )
            return JSONResponse(
                error_body(
                    "no eligible Token Harbor account: every account is disabled, "
                    "cooling down, or out of free allowance",
                    code=reason,
                    status=503,
                    kind="service_unavailable",
                ),
                status_code=503,
            )

        last: UpstreamResult | None = None
        for account in candidates[:MAX_ATTEMPTS]:
            try:
                response = await gateway.upstream.send(
                    request.method,
                    path,
                    account.api_key,
                    raw_body=body,
                    incoming=dict(request.headers),
                    stream=stream,
                )
            except httpx.HTTPError as exc:
                last = UpstreamResult(
                    status=502,
                    body=b"",
                    headers={},
                    error_code="upstream_unreachable",
                    error_message=str(exc),
                )
                gateway.db.mark_error(account.email, str(exc)[:300], cooldown=15)
                continue

            if response.status_code >= 400:
                result = await gateway.upstream.read_error(response)
                await response.aclose()
                gateway.pool.record_failure(account, result, model)
                last = result
                if not result.retryable:
                    break
                continue

            if not stream:
                raw = await response.aread()
                await response.aclose()
                gateway.pool.record_success(account)
                payload = _safe_json(raw) or {}
                usage = payload.get("usage") or {}
                gateway.log(
                    key_name=key_name,
                    model=payload.get("model") or model,
                    account=account.email,
                    status=response.status_code,
                    latency_ms=int((time.time() - started) * 1000),
                    prompt_tokens=(usage or {}).get("prompt_tokens"),
                    completion_tokens=(usage or {}).get("completion_tokens"),
                    stream=False,
                )
                return Response(
                    content=raw,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type", "application/json"),
                    headers={
                        "x-th2api-account": mask_email(account.email),
                        "x-th2api-attempts": str(candidates.index(account) + 1),
                    },
                )

            # Streaming: peek one frame before committing, so a key that dies
            # mid-handshake still costs nothing visible to the client.
            iterator = response.aiter_raw()
            try:
                first = await iterator.__anext__()
            except StopAsyncIteration:
                first = b""
            except httpx.StreamConsumed:
                # Some transports hand back an already-buffered body (mock
                # transports, or a proxy that ignored ``stream``).  Serve it as
                # a normal response instead of failing the request.
                raw = response.content
                await response.aclose()
                gateway.pool.record_success(account)
                usage, served = _peek_usage(raw)
                if usage is None and served is None:
                    payload = _safe_json(raw) or {}
                    usage, served = payload.get("usage"), payload.get("model")
                elapsed = int((time.time() - started) * 1000)
                gateway.log(
                    key_name=key_name,
                    model=served or model,
                    account=account.email,
                    status=response.status_code,
                    latency_ms=elapsed,
                    ttft_ms=elapsed,
                    prompt_tokens=(usage or {}).get("prompt_tokens"),
                    completion_tokens=(usage or {}).get("completion_tokens"),
                    stream=True,
                )
                return Response(
                    content=raw,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type", "text/event-stream"),
                    headers={"x-th2api-account": mask_email(account.email)},
                )
            except httpx.HTTPError as exc:
                await response.aclose()
                last = UpstreamResult(
                    status=502, body=b"", headers={},
                    error_code="upstream_stream_error", error_message=str(exc),
                )
                gateway.db.mark_error(account.email, str(exc)[:300], cooldown=15)
                continue

            gateway.pool.record_success(account)
            ttft = int((time.time() - started) * 1000)
            tail = {"data": b""}

            async def body_stream():
                status = response.status_code
                error = None
                try:
                    if first:
                        yield first
                    async for chunk in iterator:
                        tail["data"] = (tail["data"] + chunk)[-16384:]
                        yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    # Client hung up mid-answer; the upstream call is aborted
                    # in ``finally`` and the request is logged as a cancel.
                    status = 499
                    error = "client_disconnected"
                    raise
                except httpx.HTTPError as exc:
                    status = 502
                    error = f"stream_error: {exc}"
                    raise
                finally:
                    await response.aclose()
                    usage, served = _peek_usage(tail["data"])
                    gateway.log(
                        key_name=key_name,
                        model=served or model,
                        account=account.email,
                        status=status,
                        latency_ms=int((time.time() - started) * 1000),
                        ttft_ms=ttft,
                        prompt_tokens=(usage or {}).get("prompt_tokens"),
                        completion_tokens=(usage or {}).get("completion_tokens"),
                        error=error,
                        stream=True,
                    )

            return StreamingResponse(
                body_stream(),
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "text/event-stream"),
                headers={
                    **KEEPALIVE_HEADERS,
                    "x-th2api-account": mask_email(account.email),
                    "x-th2api-attempts": str(candidates.index(account) + 1),
                },
            )

        status = last.status if last else 502
        code = (last.error_code if last else None) or "upstream_error"
        message = (last.error_message if last else None) or "all accounts failed"
        gateway.log(
            key_name=key_name,
            model=model,
            account=candidates[0].email if candidates else None,
            status=status,
            latency_ms=int((time.time() - started) * 1000),
            error=f"{code}: {message}"[:300],
            stream=stream,
        )
        return JSONResponse(
            error_body(f"upstream rejected every attempt: {message}", code=code, status=status),
            status_code=status if status >= 400 else 502,
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, key: dict = Depends(auth_gateway)):
        raw = await request.body()
        payload = _safe_json(raw)
        if payload is None:
            return JSONResponse(error_body("body must be JSON", code="invalid_json", status=400), 400)
        model = payload.get("model") or ""
        if not model:
            return JSONResponse(
                error_body("missing model", code="missing_model", status=400), 400
            )
        return await dispatch(
            request,
            "/v1/chat/completions",
            body=raw,
            stream=bool(payload.get("stream")),
            key_name=key["name"],
            model=model,
        )

    @app.get("/v1/models")
    async def list_models(key: dict = Depends(auth_gateway)):
        gateway = gw()
        models = await gateway.model_list()
        return {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "created": int(time.time()), "owned_by": "tokenharbor"}
                for model in models
            ],
        }

    @app.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def passthrough(path: str, request: Request, key: dict = Depends(auth_gateway)):
        """Everything else worth proxying: embeddings, responses, model reads.

        Streaming is honoured when the body asks for it, otherwise the response
        is buffered so usage can be logged.
        """
        raw = await request.body()
        payload = _safe_json(raw) if raw else None
        model = (payload or {}).get("model") or ""
        stream = bool((payload or {}).get("stream"))
        return await dispatch(
            request,
            f"/v1/{path}",
            body=raw or None,
            stream=stream,
            key_name=key["name"],
            model=model,
        )

    # ------------------------------------------------------------------
    # panel API
    # ------------------------------------------------------------------

    @app.post("/api/auth")
    async def auth_check(request: Request):
        body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
        token = (body or {}).get("token") or request.headers.get("x-admin-token", "")
        if token != resolved.admin_token:
            raise HTTPException(401, "wrong admin token")
        return {"ok": True}

    @app.get("/api/overview", dependencies=[Depends(auth_admin)])
    async def overview():
        gateway = gw()
        states = gateway.db.all_states()
        accounts = gateway.pool.accounts()
        now = time.time()
        enabled = [a for a in accounts if states.get(a.email, {}).get("enabled", 1)]
        cooling = [a for a in enabled if (states.get(a.email, {}).get("cooldown_until") or 0) > now]
        exhausted = [a for a in accounts if states.get(a.email, {}).get("free_exhausted")]
        dead = [a for a in accounts if not states.get(a.email, {}).get("enabled", 1)]
        pcts = [
            states[a.email]["free_pct"]
            for a in accounts
            if states.get(a.email, {}).get("free_pct") is not None
        ]
        stats = gateway.db.request_stats(now - 86400)
        models = await gateway.model_list()
        return {
            "accounts": {
                "total": len(accounts),
                "enabled": len(enabled),
                "cooling": len(cooling),
                "exhausted": len(exhausted),
                "disabled": len(dead),
                "free_ready": len([a for a in enabled if a.free_models_enabled]),
                "avg_free_pct": round(sum(pcts) / len(pcts), 1) if pcts else None,
            },
            "gateway_keys": len(gateway.db.list_gateway_keys()),
            "models": len(models),
            "requests_24h": {
                "total": stats.get("total") or 0,
                "ok": stats.get("ok") or 0,
                "avg_latency_ms": int(stats.get("avg_latency") or 0),
                "tokens": stats.get("tokens") or 0,
            },
            "upstream": resolved.upstream,
        }

    @app.get("/api/accounts", dependencies=[Depends(auth_admin)])
    async def accounts():
        gateway = gw()
        states = gateway.db.all_states()
        result = []
        for account in gateway.pool.accounts():
            state = states.get(account.email) or gateway.db.state(account.email)
            result.append(account.public(state))
        result.sort(key=lambda item: item["created_at"], reverse=True)
        return {"accounts": result}

    @app.post("/api/accounts/{email}/toggle", dependencies=[Depends(auth_admin)])
    async def toggle_account(email: str, request: Request):
        body = await request.json()
        gateway = gw()
        if not gateway.store.get(email):
            raise HTTPException(404, "account not found")
        gateway.db.set_enabled(email, bool(body.get("enabled")))
        return {"ok": True}

    @app.delete("/api/accounts/{email}", dependencies=[Depends(auth_admin)])
    async def delete_account(email: str):
        gateway = gw()
        removed = gateway.store.delete(email)
        if not removed:
            raise HTTPException(404, "account not found")
        gateway.db.set_enabled(email, False)
        return {"ok": True}

    @app.post("/api/accounts/{email}/refresh", dependencies=[Depends(auth_admin)])
    async def refresh_account(email: str):
        gateway = gw()
        account = gateway.store.get(email)
        if not account:
            raise HTTPException(404, "account not found")
        try:
            info = await asyncio.to_thread(refresh_free_tier, account, resolved)
        except Exception as exc:  # session expiry and network errors alike
            gateway.db.mark_error(email, f"free-tier refresh: {exc}"[:300])
            raise HTTPException(400, str(exc))
        gateway.db.set_free_tier(
            email,
            pct=info.get("pct"),
            reset_at=info.get("reset_at"),
            exhausted=bool(info.get("exhausted")),
        )
        if info.get("pct") is not None and info["pct"] < 100:
            gateway.db.set_enabled(email, True)
        return {"ok": True, **info}

    @app.post("/api/accounts/refresh", dependencies=[Depends(auth_admin)])
    async def refresh_all():
        gateway = gw()
        results = []
        for account in gateway.pool.accounts():
            if not account.cookies:
                results.append({"email": account.email, "error": "no session cookies"})
                continue
            try:
                info = await asyncio.to_thread(refresh_free_tier, account, resolved)
                gateway.db.set_free_tier(
                    account.email,
                    pct=info.get("pct"),
                    reset_at=info.get("reset_at"),
                    exhausted=bool(info.get("exhausted")),
                )
                results.append({"email": account.email, **info})
            except Exception as exc:
                gateway.db.mark_error(account.email, f"free-tier refresh: {exc}"[:300])
                results.append({"email": account.email, "error": str(exc)})
            await asyncio.sleep(0.4)
        return {"results": results}

    @app.get("/api/keys", dependencies=[Depends(auth_admin)])
    async def list_keys():
        return {"keys": gw().db.list_gateway_keys()}

    @app.post("/api/keys", dependencies=[Depends(auth_admin)])
    async def create_key(request: Request):
        body = await request.json()
        name = (body.get("name") or "default").strip()[:64]
        return gw().db.create_gateway_key(name, resolved.gateway_key_prefix)

    @app.delete("/api/keys/{key_id}", dependencies=[Depends(auth_admin)])
    async def delete_key(key_id: str):
        gw().db.delete_gateway_key(key_id)
        return {"ok": True}

    @app.get("/api/logs", dependencies=[Depends(auth_admin)])
    async def logs(limit: int = 100):
        return {"logs": gw().db.recent_requests(min(max(limit, 1), 500))}

    @app.get("/api/models", dependencies=[Depends(auth_admin)])
    async def models(refresh: bool = False):
        gateway = gw()
        return {"models": await gateway.model_list(refresh=refresh)}

    @app.post("/api/test", dependencies=[Depends(auth_admin)])
    async def test_chat(request: Request):
        """Send one prompt through the pool and return what came back."""
        gateway = gw()
        body = await request.json()
        model = body.get("model") or "deepseek-v4.1-flash:free"
        prompt = body.get("prompt") or "Say hello in five words."
        candidates = gateway.pool.candidates(model)
        if not candidates:
            raise HTTPException(503, "no eligible account for this model")
        started = time.time()
        account = candidates[0]
        response = await gateway.upstream.send(
            "POST",
            "/v1/chat/completions",
            account.api_key,
            json_body={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": int(body.get("max_tokens") or 256),
            },
        )
        raw = await response.aread()
        await response.aclose()
        elapsed = int((time.time() - started) * 1000)
        payload = _safe_json(raw) or {}
        if response.status_code >= 400:
            code, message = parse_error(raw)
            gateway.pool.record_failure(
                account,
                UpstreamResult(response.status_code, raw, {}, code, message),
                model,
            )
        else:
            gateway.pool.record_success(account)
        choice = (payload.get("choices") or [{}])[0] if isinstance(payload, dict) else {}
        message = (choice.get("message") or {}) if isinstance(choice, dict) else {}
        return {
            "status": response.status_code,
            "account": mask_email(account.email),
            "latency_ms": elapsed,
            "content": message.get("content"),
            "reasoning": message.get("reasoning_content"),
            "usage": payload.get("usage"),
            "error": payload.get("error"),
        }

    @app.post("/api/register", dependencies=[Depends(auth_admin)])
    async def register(request: Request):
        body = await request.json()
        args = ["--count", str(int(body.get("count") or 1))]
        if body.get("label"):
            args += ["--label", str(body["label"])]
        if body.get("proxy"):
            args += ["--proxy", str(body["proxy"])]
        if body.get("provider") == "domain-forward":
            args += ["--email-provider", "domain-forward"]
            for flag, field in (("--domain", "domain"), ("--forward-to", "forward_to"), ("--pin", "pin")):
                if body.get(field):
                    args += [flag, str(body[field])]
        job = gw().jobs.start(args)
        return job.public()

    @app.get("/api/jobs", dependencies=[Depends(auth_admin)])
    async def jobs():
        return {"jobs": gw().jobs.list()}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(auth_admin)])
    async def job(job_id: str):
        found = gw().jobs.get(job_id)
        if not found:
            raise HTTPException(404, "job not found")
        gw().pool.accounts()
        return found.public()

    # ------------------------------------------------------------------
    # panel
    # ------------------------------------------------------------------

    @app.get("/")
    async def index():
        return FileResponse(resolved.static_dir / "index.html")

    @app.get("/assets/{name}")
    async def asset(name: str):
        path = (resolved.static_dir / name).resolve()
        if not path.is_file() or resolved.static_dir not in path.parents:
            raise HTTPException(404, "not found")
        media = "text/css" if name.endswith(".css") else "application/javascript"
        return FileResponse(path, media_type=media)

    @app.get("/health")
    async def health():
        gateway = gw()
        return {
            "ok": True,
            "accounts": len(gateway.pool.accounts()),
            "upstream": resolved.upstream,
        }

    return app


def _safe_json(raw: bytes) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None
