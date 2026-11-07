---
name: gateway-routing
description: Recall when changing how requests are placed on accounts, why a key got disabled, or how the panel reads account health.
metadata:
  type: project
  scope: src/th2api/gateway and var/th2api.db
  status: active
  last_verified: 2026-11-08
---

The gateway is a router, not a translator: Token Harbor already speaks OpenAI,
so `app.py:dispatch` only chooses an account, forwards the body verbatim, and
classifies failures.

Selection order (`pool.py:candidates`): enabled → not cooling down → free-route
consent and quota → unmeasured accounts first, then lowest `used_pct`, then
least-recently-used, then fewest errors. An LRU tiebreak matters because a
parallel burst otherwise lands on one key and trips its per-minute 429.

Failure policy lives in `upstream.py:UpstreamResult.cooldown`: 401 disables the
account outright (a dead key never recovers), 402/403 cool down 15 minutes (24h
for a zero wallet), 429 honours `Retry-After`, 5xx cools 30s. 400 and 404 are
never retried — the request is wrong, and retrying only multiplies it.

Streaming commits after the first upstream frame, not before, so a key that
dies mid-handshake is swapped invisibly. Two edge cases cost real debugging
time: `httpx.StreamConsumed` (a transport that already buffered the body — treat
it as a normal response) and client disconnects (log as `499`, never as a
success).

State separation is deliberate: `var/accounts.jsonl` stays append-only and owned
by the registration pipeline, deletions are tombstones, and everything mutable
(health, keys, request log) lives in `var/th2api.db`.

**Why:** the free allowance is metered by list price on a roughly one-dollar
rolling week, so placement quality matters more than raw throughput.

**How to apply:** before "fixing" a 429 loop, check whether the pool is actually
spreading load; before disabling an account by hand, read `last_error`, because
the policy already disables on 401.

**Recheck when:** Token Harbor changes error codes or adds per-model routing.