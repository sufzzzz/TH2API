"""Gateway behaviour against a mocked Token Harbor.

The mock is an ``httpx.MockTransport``, so no sockets are involved: each test
declares what the upstream answers per API key and then asserts on what the
gateway does with that -- retry, failover, logging, and the shapes clients see.
"""

from __future__ import annotations

import contextlib
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from th2api.gateway.app import create_app
from th2api.gateway.upstream import Upstream, UpstreamResult
from th2api.settings import Settings

ADMIN = "test-admin-token"


def sse(frames: list[dict]) -> bytes:
    out = b""
    for frame in frames:
        out += b"data: " + json.dumps(frame).encode() + b"\n\n"
    return out + b"data: [DONE]\n\n"


def chat_payload(model: str = "deepseek-v4.1-flash:free") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def account_record(email: str, api_key: str, *, free: bool = True) -> dict:
    return {
        "email": email,
        "api_key": api_key,
        "label": "test",
        "free_models_enabled": free,
        "cookies": {"sb": email},
        "created_at": "2026-11-01T00:00:00Z",
    }


def make_settings(tmp_path, records: list[dict] | None = None) -> Settings:
    (tmp_path / "var").mkdir(exist_ok=True)
    accounts = tmp_path / "var" / "accounts.jsonl"
    rows = records if records is not None else [
        account_record("dead@example.com", "thk_dead"),
        account_record("good@example.com", "thk_good"),
    ]
    accounts.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return Settings(
        home=tmp_path,
        upstream="https://tokenharbor.ai",
        host="127.0.0.1",
        port=0,
        admin_token=ADMIN,
    )


def route_by_key(handler):
    """``thk_dead`` always 401s; anything else goes to ``handler``."""

    def route(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("authorization", "").replace("Bearer ", "")
        if key == "thk_dead":
            return httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "bad key"}})
        return handler(request)

    return route


def seed_key(app) -> str:
    return app.state.gateway.db.create_gateway_key("test", "sk-th2api")["key"]


@contextlib.contextmanager
def gateway_client(tmp_path, handler, *, records=None, auth=True, admin=True):
    """Enter the app so the lifespan runs, then swap in the mock transport."""
    settings = make_settings(tmp_path, records)
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.gateway.upstream = Upstream(
            settings.upstream, transport=httpx.MockTransport(handler)
        )
        if auth:
            client.headers.update({"Authorization": "Bearer " + seed_key(app)})
        if admin:
            client.headers.update({"X-Admin-Token": ADMIN})
        yield client


def test_models_failover_on_dead_key(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"object": "list", "data": [{"id": "deepseek-v4.1-flash:free"}]}
        )

    with gateway_client(tmp_path, route_by_key(handler)) as client:
        response = client.get("/v1/models")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["data"]] == ["deepseek-v4.1-flash:free"]
        # The dead key must be retired from the pool after its 401.
        assert client.app.state.gateway.db.state("dead@example.com")["enabled"] == 0


def test_non_stream_usage_is_logged(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_payload())

    with gateway_client(tmp_path, route_by_key(handler)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-v4.1-flash:free", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hi"
        assert response.headers["x-th2api-account"].startswith("go***")

        logs = client.app.state.gateway.db.recent_requests(5)
        assert logs and logs[0]["completion_tokens"] == 7
        assert logs[0]["status"] == 200


def test_streaming_passes_frames_and_records_usage(tmp_path):
    frames = [
        {"id": "c1", "model": "deepseek-v4.1-flash:free", "choices": [{"delta": {"content": "he"}}]},
        {"id": "c1", "model": "deepseek-v4.1-flash:free", "choices": [{"delta": {"content": "llo"}}]},
        {"id": "c1", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(frames), headers={"content-type": "text/event-stream"})

    with gateway_client(tmp_path, route_by_key(handler)) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "deepseek-v4.1-flash:free", "stream": True, "messages": []},
        ) as response:
            assert response.status_code == 200
            body = b"".join(response.iter_bytes())
        assert b"[DONE]" in body

        deadline = time.time() + 2
        logs: list[dict] = []
        while time.time() < deadline:
            logs = client.app.state.gateway.db.recent_requests(5)
            if logs:
                break
            time.sleep(0.05)
        assert logs[0]["stream"] == 1
        assert logs[0]["completion_tokens"] == 2
        assert logs[0]["ttft_ms"] is not None


def test_client_error_is_not_retried(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"code": "invalid_request", "message": "bad"}})

    with gateway_client(tmp_path, route_by_key(handler)) as client:
        response = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
        assert response.status_code == 400
        assert calls["n"] == 1


def test_free_model_needs_consent(tmp_path):
    records = [account_record("nofree@example.com", "thk_good", free=False)]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must not be called without an eligible account")

    with gateway_client(tmp_path, handler, records=records) as client:
        response = client.post(
            "/v1/chat/completions", json={"model": "deepseek-v4.1-flash:free", "messages": []}
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "free_models_disabled"


def test_gateway_requires_key(tmp_path):
    with gateway_client(tmp_path, route_by_key(lambda r: httpx.Response(200, json={})), auth=False) as client:
        assert client.post("/v1/chat/completions", json={"model": "x"}).status_code == 401


def test_admin_requires_token(tmp_path):
    with gateway_client(
        tmp_path, route_by_key(lambda r: httpx.Response(200, json={})), admin=False
    ) as client:
        assert client.get("/api/overview").status_code == 401
        response = client.get("/api/overview", headers={"X-Admin-Token": ADMIN})
        assert response.status_code == 200
        assert response.json()["accounts"]["total"] == 2


def test_deleted_account_is_hidden(tmp_path):
    with gateway_client(tmp_path, route_by_key(lambda r: httpx.Response(200, json={}))) as client:
        client.delete("/api/accounts/good@example.com")
        listing = client.get("/api/accounts").json()["accounts"]
        assert [item["email"] for item in listing] == ["dead@example.com"]


def test_pool_orders_by_remaining_quota(tmp_path):
    with gateway_client(tmp_path, route_by_key(lambda r: httpx.Response(200, json={}))) as client:
        gateway = client.app.state.gateway
        gateway.db.set_free_tier("dead@example.com", pct=80, reset_at=None, exhausted=False)
        gateway.db.set_free_tier("good@example.com", pct=5, reset_at=None, exhausted=False)
        order = [account.email for account in gateway.pool.candidates("deepseek-v4.1-flash:free")]
        assert order == ["good@example.com", "dead@example.com"]


@pytest.mark.parametrize(
    "status,expected",
    [(401, 3600.0), (429, 60.0), (402, 900.0), (500, 30.0), (400, 0.0)],
)
def test_cooldown_by_status(status, expected):
    result = UpstreamResult(status=status, body=b"", headers={})
    assert result.cooldown() == expected