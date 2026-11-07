"""HTTP plumbing to the upstream Token Harbor API.

Token Harbor speaks plain OpenAI over ``/v1`` with ``Authorization: Bearer
thk_live_…``; there is no gateway-specific protocol to translate.  What this
module adds is uniform error classification, so the pool can tell "this key is
dead" from "this request is malformed" and decide whether retrying helps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

# Status codes worth another account.  400/404 mean the request itself is
# wrong, and retrying them just multiplies the failure across the pool.
RETRYABLE_STATUS = {401, 402, 403, 408, 409, 425, 429, 500, 502, 503, 504, 529}

# Upstream answers 403 with these codes; they are account-state problems, not
# a rejection of the request.
ACCOUNT_ERROR_CODES = {
    "free_models_disabled",
    "balance_zero",
    "insufficient_balance",
    "invalid_api_key",
    "account_disabled",
    "account_suspended",
}


@dataclass
class UpstreamResult:
    status: int
    body: bytes
    headers: dict[str, str]
    error_code: str | None = None
    error_message: str | None = None

    @property
    def retryable(self) -> bool:
        if self.error_code in ACCOUNT_ERROR_CODES:
            return True
        return self.status in RETRYABLE_STATUS

    def cooldown(self) -> float:
        """Seconds this account should sit out after producing this result."""
        if self.error_code in {"balance_zero", "insufficient_balance", "account_disabled"}:
            return 24 * 3600
        if self.status == 401:
            return 3600
        if self.status == 429:
            retry_after = self.headers.get("retry-after")
            try:
                return max(5.0, float(retry_after)) if retry_after else 60.0
            except ValueError:
                return 60.0
        if self.status in {402, 403}:
            return 900.0
        if self.status >= 500:
            return 30.0
        return 0.0


def parse_error(body: bytes) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, body[:200].decode("utf-8", "replace") or None

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return error.get("code") or error.get("type"), error.get("message")
    if isinstance(error, str):
        return payload.get("code"), error
    if isinstance(payload, dict):
        return payload.get("code") or payload.get("type"), payload.get("message")
    return None, None


class Upstream:
    """One shared ``httpx.AsyncClient`` with per-attempt request builders."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            follow_redirects=False,
            # Tests inject a mock transport; production leaves it unset.
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    def _headers(self, api_key: str, incoming: dict[str, str]) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": incoming.get("content-type", "application/json"),
            "Accept": incoming.get("accept", "application/json"),
            "User-Agent": incoming.get("user-agent", "th2api/0.2"),
        }
        if trace := incoming.get("x-request-id"):
            headers["X-Request-Id"] = trace
        return headers

    async def send(
        self,
        method: str,
        path: str,
        api_key: str,
        *,
        json_body: Any | None = None,
        raw_body: bytes | None = None,
        incoming: dict[str, str] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        """Issue one attempt and hand back the open response.

        The caller owns the response and must close it; that is what makes
        peek-before-commit streaming possible.
        """
        headers = self._headers(api_key, incoming or {})
        request = self.client.build_request(
            method,
            path,
            headers=headers,
            json=json_body if raw_body is None else None,
            content=raw_body,
        )
        return await self.client.send(request, stream=stream)

    async def read_error(self, response: httpx.Response) -> UpstreamResult:
        body = await response.aread()
        code, message = parse_error(body)
        return UpstreamResult(
            status=response.status_code,
            body=body,
            headers={k.lower(): v for k, v in response.headers.items()},
            error_code=code,
            error_message=message,
        )
