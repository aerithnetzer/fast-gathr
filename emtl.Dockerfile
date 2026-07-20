# EMTL Historian Workflow — Django production image.
#
# Serves the historian-facing Django app via gunicorn, with static assets
# collected at build time and served by WhiteNoise. Uses the slim
# production requirements (no torch/sentence-transformers) since the AWS
# chatbot / dense-lookup provider is not wired in this deployment.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=emtl_site.settings_prod

WORKDIR /code

# psycopg[binary] ships its own libpq, so no system postgres dev headers
# are needed. curl is included for the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY emtl-historian-workflow/emtl_django/requirements-prod.txt /code/requirements-prod.txt
RUN pip install -r /code/requirements-prod.txt

COPY emtl-historian-workflow/emtl_django /code/

# Collect static assets into STATIC_ROOT (WhiteNoise serves them). This runs
# without a database connection; DJANGO_SECRET_KEY is not required for
# collectstatic but we pass a throwaway to satisfy settings import.
RUN DJANGO_SECRET_KEY=build-time-only \
    DATABASE_URL="" \
    python manage.py collectstatic --noinput

EXPOSE 8000

# Run migrations (Django only touches its own tagger_/auth_/django_ tables),
# then serve. gunicorn with a modest worker count for a research-tool load.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn emtl_site.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120 --access-logfile - --error-logfile -"]
