"""Production settings for the deployed EMTL Historian Workflow.

Layered on top of the base ``emtl_site.settings`` so local development is
untouched. Selected by setting ``DJANGO_SETTINGS_MODULE=emtl_site.settings_prod``
in the Fargate task environment.

Key differences from base:
- WhiteNoise serves collected static assets.
- Trusts the ALB's X-Forwarded-Proto so Django knows requests are HTTPS.
- CSRF trusts the public origin.
- STATIC_ROOT is set so ``collectstatic`` has a destination.
"""

from __future__ import annotations

import os

from .settings import *  # noqa: F401,F403  (inherit all base settings)
from .settings import BASE_DIR, MIDDLEWARE, TEMPLATES

# ── Static files via WhiteNoise ──────────────────────────────────────────────

STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Insert WhiteNoise directly after SecurityMiddleware, as the docs require.
if "whitenoise.middleware.WhiteNoiseMiddleware" not in MIDDLEWARE:
    _security_index = MIDDLEWARE.index(
        "django.middleware.security.SecurityMiddleware"
    )
    MIDDLEWARE.insert(
        _security_index + 1, "whitenoise.middleware.WhiteNoiseMiddleware"
    )

# Answer /healthz before Host validation so ALB health checks (which send the
# target's private IP as the Host header) are not rejected with 400. Must be
# the very first middleware.
if "emtl_site.health_middleware.HealthCheckMiddleware" not in MIDDLEWARE:
    MIDDLEWARE.insert(0, "emtl_site.health_middleware.HealthCheckMiddleware")

# Gate the whole site behind Django login. Inserted immediately after
# AuthenticationMiddleware so request.user is populated. Everything except the
# allowlist (/healthz, /accounts/login|logout|password_reset, /static/)
# redirects anonymous users to LOGIN_URL.
_auth_mw = "django.contrib.auth.middleware.AuthenticationMiddleware"
_login_mw = "emtl_site.login_required_middleware.LoginRequiredMiddleware"
if _login_mw not in MIDDLEWARE and _auth_mw in MIDDLEWARE:
    MIDDLEWARE.insert(MIDDLEWARE.index(_auth_mw) + 1, _login_mw)

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# Project-level templates dir so LoginView finds registration/login.html.
if str(BASE_DIR / "templates") not in TEMPLATES[0]["DIRS"]:
    TEMPLATES[0]["DIRS"].append(str(BASE_DIR / "templates"))

# ── Behind the ALB (TLS terminated upstream) ─────────────────────────────────

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# The ALB and Cloudflare terminate TLS; do not force HTTPS redirects at the
# Django layer (that is handled by the ALB HTTP->HTTPS listener), which would
# otherwise cause redirect loops.
SECURE_SSL_REDIRECT = False

_public_host = os.getenv("DJANGO_PUBLIC_HOST", "app.gathrlab.org").strip()
CSRF_TRUSTED_ORIGINS = [f"https://{_public_host}"]

# Session cookies remain signed-cookie based (see base settings); mark them
# secure now that we are always served over HTTPS at the edge.
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
