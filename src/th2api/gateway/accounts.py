"""Credential loading on top of the append-only ``accounts.jsonl``.

The registration pipeline owns the file format; this module only reads it and
applies tombstones (``{"_deleted": true, "email": ...}``) so the panel can
remove an account without rewriting history.  Last record per email wins, which
also makes a re-registered address work without manual cleanup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from curl_cffi import requests

from ..settings import Settings


@dataclass
class Account:
    email: str
    api_key: str
    label: str
    free_models_enabled: bool
    cookies: dict[str, str]
    access_token: str
    refresh_token: str
    created_at: str
    password: str = ""
    key_id: str = ""
    email_provider: str = ""
    forward_to: str | None = None
    fingerprint: str = ""

    @property
    def masked_key(self) -> str:
        return f"{self.api_key[:12]}…{self.api_key[-4:]}" if self.api_key else ""

    def public(self, state: dict[str, Any]) -> dict[str, Any]:
        """Everything the panel needs except the secrets themselves."""
        return {
            "email": self.email,
            "api_key": self.masked_key,
            "label": self.label,
            "free_models_enabled": self.free_models_enabled,
            "provider": self.email_provider or "duckmail",
            "forward_to": self.forward_to,
            "created_at": self.created_at,
            "has_session": bool(self.cookies or self.access_token),
            "enabled": bool(state.get("enabled", 1)),
            "free_pct": state.get("free_pct"),
            "free_reset_at": state.get("free_reset_at"),
            "free_exhausted": bool(state.get("free_exhausted") or 0),
            "cooldown_until": state.get("cooldown_until") or 0,
            "last_error": state.get("last_error"),
            "last_error_at": state.get("last_error_at"),
            "last_used_at": state.get("last_used_at"),
            "ok_count": state.get("ok_count") or 0,
            "err_count": state.get("err_count") or 0,
            "checked_at": state.get("checked_at"),
        }


class AccountStore:
    """Read-through view of the JSONL file, refreshed on demand.

    Writes only ever append: deletion is a tombstone, because concurrent
    registration runs must never rewrite each other's records.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._accounts: dict[str, Account] = {}
        self._mtime: float = 0.0

    def reload(self) -> list[Account]:
        if not self.path.exists():
            self._accounts = {}
            return []
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime and self._accounts:
            return list(self._accounts.values())

        accounts: dict[str, Account] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A torn last line from an interrupted run; skip it rather
                # than lose the whole file.
                continue
            email = record.get("email")
            if not email:
                continue
            if record.get("_deleted"):
                accounts.pop(email, None)
                continue
            accounts[email] = Account(
                email=email,
                api_key=record.get("api_key") or "",
                label=record.get("label") or "",
                free_models_enabled=bool(record.get("free_models_enabled")),
                cookies={k: v for k, v in (record.get("cookies") or {}).items() if v},
                access_token=record.get("access_token") or "",
                refresh_token=record.get("refresh_token") or "",
                created_at=record.get("created_at") or "",
                password=record.get("password") or "",
                key_id=record.get("key_id") or "",
                email_provider=record.get("email_provider") or "",
                forward_to=record.get("forward_to"),
                fingerprint=record.get("fingerprint") or "",
            )
        self._accounts = accounts
        self._mtime = mtime
        return list(accounts.values())

    def get(self, email: str) -> Account | None:
        self.reload()
        return self._accounts.get(email)

    def delete(self, email: str) -> bool:
        if not self.path.exists():
            return False
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"email": email, "_deleted": True}) + "\n")
        self._mtime = 0.0
        self.reload()
        return True


def refresh_free_tier(account: Account, settings: Settings) -> dict[str, Any]:
    """Read ``/api/me/free-tier`` with the stored Supabase session cookies.

    The endpoint is the only way to see the allowance: it is measured as a
    percentage of a roughly one-dollar rolling window, not a token count.
    Runs in a worker thread because ``curl_cffi`` is synchronous.
    """
    if not account.cookies:
        raise RuntimeError("no session cookies recorded for this account")
    response = requests.get(
        f"{settings.upstream}/api/me/free-tier",
        cookies=account.cookies,
        headers={"Accept": "application/json"},
        impersonate="chrome",
        timeout=30,
    )
    if response.status_code == 401:
        raise RuntimeError("session expired (401); re-register or refresh the session")
    if response.status_code != 200:
        raise RuntimeError(f"free-tier endpoint returned {response.status_code}")
    payload = response.json()
    return {
        "pct": payload.get("used_pct"),
        "reset_at": payload.get("reset_at"),
        "exhausted": payload.get("exhausted"),
        "req_used": payload.get("req_used"),
    }