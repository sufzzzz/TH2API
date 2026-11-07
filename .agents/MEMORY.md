# Project memory

- [Token Harbor registration](memory/tokenharbor-registration.md) -- server action wire format, auto-confirmed signup, verify-before-key rule
- [Token Harbor IP reputation](memory/tokenharbor-ip-reputation.md) -- `account_cap` and Turnstile mean the egress IP is used up
- [DuckMail API](memory/duckmail-api.md) -- mail.tm-compatible temp inbox used by the registration pipeline
- [Token Harbor free tier](memory/tokenharbor-free-tier.md) -- free models need consent, ~$1 allowance on a personal 7-day window
- [Registration proxy](memory/registration-proxy.md) -- one proxy for the whole pipeline; SOCKS5 needs socks5h for remote DNS
- [Email providers](memory/email-providers.md) -- provider interface, domain-forward PIN, queue-safe concurrent mail routing
- [Gateway routing](memory/gateway-routing.md) -- account selection order, cooldown policy per status, streaming commit point, state split between JSONL and SQLite
