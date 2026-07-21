#!/bin/sh
set -e

# One-off command mode: if any arguments are passed (e.g. via an ECS task
# `command` override or `docker run ... <args>`), run them as a Django
# management command and exit. Lets operators run smoke_bedrock,
# createsuperuser, migrations, etc. without a separate image.
if [ "$#" -gt 0 ]; then
  exec python manage.py "$@"
fi

# Default server mode: apply migrations (Django only touches its own
# tagger_/auth_/django_ tables), create the bootstrap admin if configured
# (idempotent), then serve.
python manage.py migrate --noinput
python -m emtl_site.bootstrap_admin

exec gunicorn emtl_site.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers 3 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
