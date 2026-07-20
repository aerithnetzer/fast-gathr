from __future__ import annotations

from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, unquote, urlparse


def database_config_from_env(environ: Mapping[str, str], *, base_dir: Path) -> dict:
    url = str(environ.get("DATABASE_URL") or "").strip()
    if not url:
        sqlite_path = str(environ.get("EMTL_SQLITE_PATH") or "").strip()
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(sqlite_path) if sqlite_path else base_dir / "db.sqlite3",
        }
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use postgres:// or postgresql://")
    if not parsed.path.strip("/"):
        raise ValueError("DATABASE_URL must include a database name")
    query = parse_qs(parsed.query)
    options = {}
    if query.get("sslmode"):
        options["sslmode"] = query["sslmode"][-1]
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or "5432"),
        "CONN_MAX_AGE": int(environ.get("EMTL_DB_CONN_MAX_AGE") or 60),
        "OPTIONS": options,
    }
