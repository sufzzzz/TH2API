# Gateway and panel

The gateway is a routing layer, not a protocol translator: Token Harbor already
speaks OpenAI over `/v1`, so everything here is about *which* key serves a
request and *what happens* when that key fails.

```
client ──sk-th2api-…──▶ /v1/chat/completions
                          │
                          ├─ pick account (enabled, not cooling, quota)
                          ├─ POST upstream /v1 with thk_live_…
                          ├─ 4xx/5xx? classify → cooldown / disable / retry next
                          └─ stream frames back, log usage
```

## Files

```
src/th2api/gateway/
  app.py        FastAPI app: /v1 surface + panel API
  pool.py       account selection, cooldowns, quota-aware ordering
  upstream.py   httpx client, error classification, cooldown policy
  accounts.py   reads accounts.jsonl, tombstones, free-tier refresh
  db.py         SQLite: account state, gateway keys, request log
  jobs.py       registration subprocesses started from the panel
  server.py     th2api-serve entry point
  static/       panel (index.html, app.js, style.css)
var/
  accounts.jsonl   credentials, append-only, written by th2api-register
  th2api.db        runtime state
  admin_token      panel token (0600)
```

`accounts.jsonl` is never rewritten: deleting an account appends a tombstone
(`{"email": …, "_deleted": true}`), so a panel action cannot corrupt an
in-flight registration.

## Routing

A candidate list is built per request:

1. enabled, has an API key;
2. `cooldown_until` in the past;
3. for `:free` models, the account must have accepted the data-sharing consent
   and must not be marked exhausted;
4. ordering: unmeasured accounts first, then lowest `used_pct`, then
   least-recently-used, then fewest errors.

The first four candidates are tried in order. A request is retried on a
different account when the failure looks account-specific; errors caused by the
request itself (400, 404) return immediately so a bad body does not fan out
across the pool.

## Failure policy

| Upstream result | Action |
| --- | --- |
| `401` | disable the account (a dead key never recovers by itself) |
| `402` / `balance_zero` | 15-minute cooldown, or 24h for a zero-wallet account |
| `403 free_models_disabled` | mark free allowance exhausted, cooldown |
| `429` | honour `Retry-After`, default 60s |
| `5xx` / network | 30s cooldown, try the next account |
| `400` / `404` | no retry, return the upstream error verbatim |

Streaming is committed only after the first upstream frame arrives, so a key
that fails mid-handshake is replaced without the client seeing a half-answer.
A client hanging up is logged as `499 client_disconnected`.

## Panel API

All panel endpoints require `X-Admin-Token` (or `Authorization: Bearer` with the
admin token).

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/auth` | validate the token |
| GET | `/api/overview` | counts, quota average, 24h traffic |
| GET | `/api/accounts` | account rows with health and quota |
| POST | `/api/accounts/{email}/toggle` | enable / disable |
| DELETE | `/api/accounts/{email}` | tombstone the record |
| POST | `/api/accounts/{email}/refresh` | read `/api/me/free-tier` |
| POST | `/api/accounts/refresh` | refresh every account |
| GET/POST | `/api/keys`, DELETE `/api/keys/{id}` | gateway keys |
| GET | `/api/logs?limit=100` | recent requests |
| GET | `/api/models?refresh=true` | cached model list |
| POST | `/api/test` | one real completion through the pool |
| POST | `/api/register` | start a registration job |
| GET | `/api/jobs`, `/api/jobs/{id}` | job output |

## Deployment

### Docker

```bash
docker compose up -d --build
# token: docker compose logs gateway | grep admin
```

`var/` is mounted from the host, so accounts and the database survive rebuilds
and images never contain credentials.

### systemd

```ini
[Unit]
Description=TH2API gateway
After=network-online.target

[Service]
WorkingDirectory=/opt/TH2API
Environment=TH2API_HOST=127.0.0.1
Environment=TH2API_PORT=8787
ExecStart=/opt/TH2API/.venv/bin/th2api-serve
Restart=always
RestartSec=3
User=th2api

[Install]
WantedBy=multi-user.target
```

Put TLS in front of it (Caddy, nginx, Cloudflare Tunnel); the gateway itself
speaks plain HTTP and should stay on localhost.

## Limits worth remembering

* Free allowance is metered by list price, roughly one dollar per rolling
  seven-day window per account. The panel's `used_pct` bar is the real number;
  token counts are not.
* Registration is IP-sensitive: `account_cap`, Turnstile, and region blocks all
  come from the egress IP, not the email. Rotate clean residential exits.
* The gateway cannot make a paid model work on a zero balance; only `:free`
  routes answer there.