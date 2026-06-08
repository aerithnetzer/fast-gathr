
FROM python:3.14


WORKDIR /code

# The installer requires curl (and certificates) to download the release archive
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates

# Download the latest installer
ADD https://astral.sh/uv/install.sh /uv-installer.sh

# Run the installer then remove it
RUN sh /uv-installer.sh && rm /uv-installer.sh

COPY ./pyproject.toml /code/pyproject.toml

ENV PATH="/root/.local/bin/:$PATH"

RUN uv sync --directory /code

COPY ./app /code/app
COPY ./alembic.ini /code/alembic.ini

# Run migrations on startup, then launch the API. Migrations are
# idempotent (alembic short-circuits when already at head), and a
# bootstrap ``alembic stamp 0001_baseline`` should be performed once
# against any pre-existing database that already has ``user`` and
# ``apitoken`` from the legacy ``create_all`` schema.
CMD ["sh", "-c", "uv run --directory /code alembic upgrade head && uv run --directory /code fastapi run app/api.py --port 8000"]
