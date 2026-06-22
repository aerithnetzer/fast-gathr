FROM python:3.14

WORKDIR /code

# uv installer needs curl + ca-certificates
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin/:$PATH"

# Resolve dependencies first for better layer caching.
COPY ./pyproject.toml /code/pyproject.toml
COPY ./src /code/src
RUN uv sync --directory /code

# Default port; ECS overrides via $PORT if needed.
ENV PORT=8000

CMD ["uv", "run", "--directory", "/code", "python", "-m", "fast_gathr_mcp"]
