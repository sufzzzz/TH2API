# TH2API

2API gateway for [tokenharbor.ai](https://tokenharbor.ai) — an
OpenAI-compatible endpoint in front of a pool of Token Harbor accounts, plus a
web panel to run and watch that pool.

Two halves ship in one process:

* **Gateway** (`/v1/*`) — accepts `sk-th2api-…` client keys, picks a healthy
  account, streams the answer straight through, fails over on dead keys and
  rate limits, and records usage per request.
* **Panel** (`/`) — account table with free-tier gauges, gateway key management,
  live request log, a registration runner, and an on-demand model test.

The account registration pipeline (`th2api-register`) is what fills the pool;
the gateway is what spends it.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
```

## Run the gateway and panel

```bash
uv run th2api-serve
```

It prints the admin token (also written to `var/admin_token`, `0600`) and
listens on `http://127.0.0.1:8787`. Open that URL, paste the token, and the
panel loads.

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TH2API_HOME` | current directory | where `var/` lives (accounts, database, token) |
| `TH2API_HOST` | `127.0.0.1` | bind address; use `0.0.0.0` behind a reverse proxy |
| `TH2API_PORT` | `8787` | listen port |
| `TH2API_ADMIN_TOKEN` | generated | panel token; also accepted as a gateway key |
| `TH2API_UPSTREAM` | `https://tokenharbor.ai` | upstream base URL |
| `TH2API_ACCOUNTS` | `var/accounts.jsonl` | credential file |
| `TH2API_DB` | `var/th2api.db` | state database |

### Point a client at it

Create a key in the panel (密钥 tab), then:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=sk-th2api-…
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-th2api-…")
stream = client.chat.completions.create(
    model="deepseek-v4.1-flash:free",
    messages=[{"role": "user", "content": "hi"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

Routing rules, failure handling and the panel's API are documented in
[docs/gateway.md](docs/gateway.md).

## Register an account

```bash
uv run th2api-register
```

The command prints the API key once and appends the full record (email,
password, random device fingerprint, API key, and Supabase session tokens) to
`var/accounts.jsonl` (git-ignored). Useful flags:

| Flag | Purpose |
| --- | --- |
| `--count N` | register `N` accounts in one run |
| `--label NAME` | label for the minted key |
| `--email-provider KIND` | `duckmail` (default) or `domain-forward` |
| `--domain DOMAIN` | DuckMail domain, or the custom forwarding domain |
| `--forward-to ADDRESS` | DuckMail inbox receiving the custom domain's mail |
| `--pin SECRET` | DuckMail password or access token for `--forward-to` |
| `--proxy URL` | route both services through an HTTP proxy |
| `--timeout SECONDS` | wait for each verification email |

Programmatic use:

```python
from th2api.registration import register_account, save_account

account = register_account(label="my-agent")
save_account(account)
print(account.api_key)
```

### Registration through a proxy

`proxy` is a first-class argument, not a CLI-only afterthought. The same proxy
covers every request in the pipeline: DuckMail inbox creation and polling, the
Token Harbor sign-up action, the verification link, the free-model consent, and
key creation.

```python
account = register_account(
    label="my-agent",
    proxy="http://user:pass@proxy.example.com:3129",
)
```

```bash
uv run th2api-register --proxy http://user:pass@proxy.example.com:3129
```

The URL must include its scheme (`http://`, `https://`, or `socks5h://`);
percent-encode reserved characters in the credentials. For SOCKS5 use
`socks5h://` (remote DNS) — plain `socks5://` resolves hostnames locally, and
behind a fake-IP/tunnel DNS that hands the proxy an unroutable address:

```bash
uv run th2api-register --proxy "socks5h://user:pass@proxy.example.com:3000"
```

There is no automatic pool rotation — `account_cap` and Turnstile failures
surface as `AccountCapError` / `CaptchaRequiredError` inside
`RegistrationError.__cause__`, so callers can retry with another proxy. See
[docs/registration.md](docs/registration.md) for the retry example and the full
library reference.

### Email providers

The pipeline talks to an `EmailProvider`, so the mail source is swappable. The
default `duckmail` provider creates one disposable inbox per registration.

`domain-forward` generates a fresh `<random>@yourdomain` address and reads mail
from a single DuckMail inbox that your domain's catch-all forwards into:

```bash
uv run th2api-register --email-provider domain-forward   --domain example.com   --forward-to inbox@niceground.shop   --pin '<duckmail password or access token>'
```

```python
from th2api.registration import DomainForwardProvider, register_account

provider = DomainForwardProvider(
    domain="example.com",
    forward_to="inbox@niceground.shop",
    pin="<duckmail password or access token>",
)
account = register_account(mail_provider=provider)
```

`pin` is the DuckMail credential for the forward inbox, not the new account's
password — the pipeline generates a separate password for Token Harbor.

A shared forward inbox is safe for concurrent registrations: messages are never
deleted or consumed, each run filters by recipient and by the user id inside the
verification link, and a burst is read across several inbox pages. Two
simultaneous sign-ups therefore cannot take each other's email. To add Outlook,
Gmail, or another source, subclass `EmailProvider` (implement `create_mailbox`,
`list_messages`, `get_message`) and pass an instance as `mail_provider`; see
[docs/registration.md](docs/registration.md) for the interface.

## How registration works

1. The configured `EmailProvider` supplies an address. DuckMail
   (`api.duckmail.sbs`, mail.tm-compatible) is the default; a custom domain can
   forward into one shared DuckMail inbox instead.
2. `POST https://tokenharbor.ai/login?mode=signup` submits the login page's
   React Server Action with a browser-shaped multipart body and a freshly
   random device fingerprint for every run. Supabase creates the user already
   confirmed and returns session cookies.
3. Token Harbor still sends its own verification email. The pipeline polls the
   inbox, opens the `/verify-email?token=…` link with the same session, and
   confirms the redirect lands on `?verify=success`.
4. `POST /api/me/privacy` grants the free-model data-sharing consent, because
   `:free` routes answer `free_models_disabled` until it is accepted.
5. `POST /api/keys` mints the key, which is then checked against
   `GET /v1/models` before being recorded.

The JSONL record keeps the email, password, fingerprint, API key, free-model
consent state, email provider, forward target, raw session token,
access/refresh tokens, and the cookies needed to resume the session.

Keys created before verification are accepted by the API but rejected by
`/v1/models`, so the pipeline always verifies first.

## Qualification test (on demand)

Registration does not prove the routed model is real. This separate test asks a
thinking model to produce an SVG animation of a pelican riding a bicycle, with
`reasoning_effort: max`, and writes an HTML report for human review:

```bash
uv run th2api-qualify --model deepseek-v4.1-flash:free --effort max
```

The report lands in `var/tests/pelican-*.html` and embeds the model's page in a
sandboxed iframe, the raw output, the reasoning trace, token usage, elapsed
time, and the request id. A normal run thinks for tens of thousands of tokens,
so this is deliberately not part of `th2api-register`.

## Known failures

Both failures below are network-reputation signals from Token Harbor, not
invalid input. Retry from a cleaner network or through a residential proxy.

`account_cap` — the egress IP has already created its allowance of accounts:

```json
{"error": "We can't create another account from this network or device right now.", "code": "account_cap"}
```

Turnstile — after a couple of sign-ups the same IP is asked for a human check:

```text
Please complete the human check to continue.
```

The pipeline raises `AccountCapError` and `CaptchaRequiredError` respectively
so callers can tell these apart from real validation errors.

Region block — some exit countries are not supported:

```text
Your IP or email service is not supported for Token Harbor accounts.
```

Raised as `RegionBlockedError`; switch the egress country.

## Layout

```
src/th2api/registration/
  duckmail.py      temporary inbox client
  tokenharbor.py   sign-up server action, verification, API keys
  flow.py          end-to-end pipeline
  providers.py     email provider interface and presets
  routing.py       queue-safe verification mail routing
  store.py         local account records
  cli.py           th2api-register entry point
tests/
  test_routing.py  concurrent-mail routing checks
src/th2api/qualification/
  pelican.py       on-demand pelican SVG test and HTML report
  cli.py           th2api-qualify entry point
```

---

学AI，上L站
感谢LinuxDo社区
