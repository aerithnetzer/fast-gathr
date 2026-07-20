"""Idempotent Django superuser bootstrap.

Mirrors the FastAPI service's bootstrap_admin pattern. Reads
``EMTL_BOOTSTRAP_ADMIN_USERNAME`` and ``EMTL_BOOTSTRAP_ADMIN_PASSWORD`` from
the environment and creates a superuser on first startup. No-op if either var
is missing or if any superuser already exists, so it is safe to run on every
container start. Rotating the secret does NOT change an existing admin's
password.

Invoked from entrypoint.sh after ``migrate`` and before gunicorn starts.
"""

from __future__ import annotations

import os

import django


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "emtl_site.settings_prod")
    django.setup()

    from django.contrib.auth import get_user_model

    username = (os.getenv("EMTL_BOOTSTRAP_ADMIN_USERNAME") or "").strip()
    password = os.getenv("EMTL_BOOTSTRAP_ADMIN_PASSWORD") or ""
    if not username or not password:
        print("bootstrap_admin: no bootstrap credentials set; skipping.")
        return

    User = get_user_model()
    if User.objects.filter(is_superuser=True).exists():
        print("bootstrap_admin: a superuser already exists; skipping.")
        return

    User.objects.create_superuser(username=username, password=password)
    print(f"bootstrap_admin: created superuser {username!r}.")


if __name__ == "__main__":
    main()
