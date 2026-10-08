FROM python:3.11-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
# Install third-party dependencies before application source.  The tiny package
# skeleton makes this Docker layer reusable when only ReForge code changes.
RUN mkdir -p reforge/backend/app && touch reforge/backend/app/__init__.py
RUN --mount=type=cache,target=/root/.cache/pip pip install .
COPY reforge ./reforge
# Controlled fixtures are part of the API's benchmark catalog.  Keep them in
# the runtime image so /benchmarks and /benchmarks/run work after deployment.
COPY benchmarks ./benchmarks

RUN mkdir -p /app/runs
EXPOSE 8000
CMD ["uvicorn", "reforge.backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
