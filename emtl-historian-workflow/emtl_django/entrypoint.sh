#!/bin/sh
set -e

# Apply Django migrations (only touches auth_/django_/tagger_ tables), create
# the bootstrap admin if configured (idempotent), then serve.
python manage.py migrate --noinput
python -m emtl_site.bootstrap_admin

exec gunicorn emtl_site.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers 3 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
