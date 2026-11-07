"""``th2api-serve``: start the gateway and panel on one port."""

from __future__ import annotations

import uvicorn

from ..settings import load_settings
from .app import create_app


def main() -> int:
    settings = load_settings()
    app = create_app(settings)
    print(f"TH2API gateway on http://{settings.host}:{settings.port}")
    print(f"  upstream : {settings.upstream}")
    print(f"  accounts : {settings.accounts_path}")
    print(f"  database : {settings.db_path}")
    print(f"  admin    : {settings.admin_token}")
    print("Open the panel at / and paste the admin token.")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())