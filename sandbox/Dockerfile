FROM python:3.11-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git build-essential nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    pytest \
    pytest-json-report \
    ruff \
    mypy \
    pyupgrade \
    bump-pydantic

RUN useradd --create-home --uid 10001 reforge
WORKDIR /work
USER reforge

