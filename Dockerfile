FROM ghcr.io/astral-sh/uv:0.8.13@sha256:4de5495181a281bc744845b9579acf7b221d6791f99bcc211b9ec13f417c2853 AS uv
FROM node:24-bookworm-slim@sha256:cd84903a12dbd26b46f1f3b8144a2568c41c5d37ddd0c7a80a34c7a19786b35f AS web-builder

WORKDIR /web
RUN corepack enable && corepack install --global pnpm@11.17.0
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY web/index.html web/tsconfig.json web/vite.config.ts web/prettier.config.js ./
COPY web/src ./src
RUN pnpm build

FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_PROGRESS=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TWOXBRAINZ_LOG_FILE=/tmp/2xbrainz.log

ARG PIPEWIRE_BIN_VERSION=0.3.65-3+deb12u1

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        pipewire-bin=${PIPEWIRE_BIN_VERSION} \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=web-builder --chown=app:app /web/dist /app/web/dist
COPY --chown=app:app LICENSE THIRD_PARTY.md /app/

USER app:app
ENTRYPOINT ["2xbrainz"]
CMD ["doctor"]
