"""ASGI config for the EMTL Tagger Workbench prototype."""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "emtl_site.settings")

application = get_asgi_application()
