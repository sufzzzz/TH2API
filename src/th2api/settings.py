"""Runtime settings for the gateway.

Everything is environment-driven so a container only needs ``TH2API_HOME``
pointed at a volume.  Defaults keep the process self-contained under ``var/``,
matching how the registration pipeline already stores ``accounts.jsonl``.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

DEFAULT_UPSTREAM = "https://tokenharbor.ai"


@dataclass(frozen=True)
class Settings:
    home: Path
    upstream: str
    host: str
    port: int
    admin_token: str
    gateway_key_prefix: str = "sk-th2api"

    @property
    def var(self) -> Path:
        return self.home / "var"

    @property
    def accounts_path(self) -> Path:
        return Path(os.environ.get("TH2API_ACCOUNTS", self.var / "accounts.jsonl"))

    @property
    def db_path(self) -> Path:
        return Path(os.environ.get("TH2API_DB", self.var / "th2api.db"))

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "gateway" / "static"


def load_settings() -> Settings:
    home = Path(os.environ.get("TH2API_HOME", Path.cwd())).expanduser().resolve()
    var = home / "var"
    var.mkdir(parents=True, exist_ok=True)

    # The admin token gates every mutating panel endpoint.  Generate one on
    # first run and keep it on disk (0600) so restarts do not log the operator
    # out mid-session; an explicit env var always wins.
    token = os.environ.get("TH2API_ADMIN_TOKEN", "").strip()
    token_file = var / "admin_token"
    if not token:
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(24)
            token_file.write_text(token + "\n", encoding="utf-8")
            os.chmod(token_file, 0o600)

    return Settings(
        home=home,
        upstream=os.environ.get("TH2API_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/"),
        host=os.environ.get("TH2API_HOST", "127.0.0.1"),
        port=int(os.environ.get("TH2API_PORT", "8787")),
        admin_token=token,
    )