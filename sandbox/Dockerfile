# Disposable execution image for target repositories (Python and JavaScript/TypeScript).
# Node.js comes from the official Node image: Debian's packaged Node 18 is
# end-of-life and lacks corepack, which ReForge uses for pnpm/yarn projects.
FROM node:22-bookworm-slim AS node

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && node --version && npm --version && corepack --version

RUN pip install --no-cache-dir \
    pytest \
    pytest-json-report \
    ruff \
    mypy \
    pyupgrade \
    bump-pydantic

# The root filesystem is read-only at run time; keep package-manager state in /tmp.
ENV COREPACK_HOME=/tmp/corepack \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    NPM_CONFIG_UPDATE_NOTIFIER=false

RUN useradd --create-home --uid 10001 reforge
WORKDIR /work
USER reforge
