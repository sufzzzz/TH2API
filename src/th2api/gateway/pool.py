"""Account selection and health bookkeeping.

The pool answers one question per request: *which key do we try first, and
what do we learn when it fails?*  Three signals drive it:

* eligibility -- enabled, not cooling down, free routes only from accounts that
  accepted the data-sharing consent;
* freshness -- least-recently-used first, so a burst of parallel requests
  spreads over the pool instead of hammering one key into a 429;
* remaining allowance -- for ``:free`` models, accounts with a lower
  ``used_pct`` go first, tying request placement to the roughly one-dollar
  rolling window Token Harbor meters.
"""

from __future__ import annotations

import time
from typing import Iterable

from .accounts import Account, AccountStore
from .db import Database
from .upstream import UpstreamResult


def is_free_model(model: str) -> bool:
    return model.endswith(":free") or model.endswith("-free")


class Pool:
    def __init__(self, db: Database, store: AccountStore) -> None:
        self.db = db
        self.store = store
        self._last_order: dict[str, float] = {}

    def accounts(self) -> list[Account]:
        accounts = self.store.reload()
        self.db.ensure_accounts([account.email for account in accounts])
        return accounts

    def by_email(self) -> dict[str, Account]:
        return {account.email: account for account in self.accounts()}

    def candidates(self, model: str) -> list[Account]:
        states = self.db.all_states()
        now = time.time()
        free = is_free_model(model)
        eligible: list[tuple[tuple, Account]] = []
        for account in self.accounts():
            state = states.get(account.email) or {}
            if not state.get("enabled", 1):
                continue
            if not account.api_key:
                continue
            if (state.get("cooldown_until") or 0) > now:
                continue
            if free:
                if not account.free_models_enabled:
                    continue
                if state.get("free_exhausted"):
                    continue
            pct = state.get("free_pct")
            sort_key = (
                0 if free and pct is None else 1,  # unmeasured accounts first
                pct if free and pct is not None else 0,
                state.get("last_used_at") or 0,
                -(state.get("err_count") or 0),
            )
            eligible.append((sort_key, account))
        eligible.sort(key=lambda item: item[0])
        return [account for _, account in eligible]

    def model_union(self, accounts: Iterable[Account]) -> list[str]:
        """Models are per-account; the panel shows the cached union."""
        models: set[str] = set()
        for account in accounts:
            models.update(account_models(self.db, account.email))
        return sorted(models)

    # ---- outcomes ------------------------------------------------------

    def record_success(self, account: Account, usage: dict | None = None) -> None:
        self.db.mark_used(account.email, True)
        self.db.clear_error(account.email)
        self._last_order[account.email] = time.time()

    def record_failure(self, account: Account, result: UpstreamResult, model: str) -> None:
        message = result.error_message or f"HTTP {result.status}"
        if result.error_code:
            message = f"{result.error_code}: {message}"
        self.db.mark_error(account.email, message[:500], cooldown=result.cooldown())

        free = is_free_model(model)
        exhausted = result.error_code in {"free_models_disabled"} or (
            free and result.error_code in {"balance_zero", "insufficient_balance"}
        )
        if free and (exhausted or result.error_code == "free_tier_exhausted"):
            state = self.db.state(account.email)
            self.db.set_free_tier(
                account.email,
                pct=100 if exhausted else state.get("free_pct"),
                reset_at=state.get("free_reset_at"),
                exhausted=True,
            )
        if result.status == 401:
            # A key that no longer authenticates will never recover on its own;
            # disabling it surfaces the problem in the panel instead of burning
            # every retry on it.
            self.db.set_enabled(account.email, False)


_MODEL_CACHE: dict[str, tuple[float, list[str]]] = {}
_MODEL_TTL = 900.0


def account_models(db: Database, email: str) -> list[str]:  # pragma: no cover - thin shim
    cached = _MODEL_CACHE.get(email)
    if cached and time.time() - cached[0] < _MODEL_TTL:
        return cached[1]
    return []


def remember_models(email: str, models: list[str]) -> None:
    _MODEL_CACHE[email] = (time.time(), models)