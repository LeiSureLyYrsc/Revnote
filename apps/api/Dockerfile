# syntax=docker/dockerfile:1.7
#
# Revnote API image.
#
#   Build context = REPOSITORY ROOT (not apps/api). apps/api is a member of the
#   root uv workspace, so the build needs the root `pyproject.toml`, the single
#   root `uv.lock` and `.python-version` next to the member — exactly as on a
#   developer machine. CI (.github/workflows/ci.yml) is the only place that
#   builds this image; local `docker build` is prohibited (docs/build-conventions.md).
#
# Runtime contract:
#   * listens on 0.0.0.0:8000 (override with HOST / PORT);
#   * started via the app factory: `uvicorn revnote_api.main:create_app --factory`;
#   * runs as an unprivileged numeric user (uid/gid 10001);
#   * configured from process environment only — no `.env` is baked in.

# --------------------------------------------------------------------------- #
# Stage 1 — builder: resolve the uv workspace, install runtime deps only.
# --------------------------------------------------------------------------- #
# Both bases are pinned by tag AND multi-arch digest (resolved from the live
# registries). `python:3.12.16-slim-bookworm` matches the `requires-python
# >=3.12` floor; the uv image supplies a pinned `uv` binary.
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /uvx /bin/

# Builder environment:
#   * PYTHON*       — no .pyc in the image, unbuffered logs.
#   * UV_COMPILE_BYTECODE — precompile .pyc at sync time (fast, read-only start).
#   * UV_LINK_MODE=copy   — hardlinks do not survive a multi-stage COPY.
#   * UV_PYTHON_DOWNLOADS=never — never fetch another interpreter; use the base
#     image's Python (a silent download would defeat the digest pin).
# The package index is declared in the root pyproject.toml ([[tool.uv.index]],
# public PyPI as default), so no index override is needed here.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Manifests only, so the dependency layer stays cached when app code changes.
# `apps/api` must exist because it is a declared workspace member.
COPY pyproject.toml uv.lock .python-version /app/
COPY apps/api/pyproject.toml /app/apps/api/pyproject.toml

# Third-party runtime dependencies only:
#   --locked                fail if uv.lock disagrees with pyproject.toml
#   --no-dev                skip pytest/ruff/httpx from the root dependency-group
#   --no-editable           install revnote-api as real files, not a path link
#   --no-install-workspace  skip EVERY workspace member, including the root
#                           virtual project *and* this `apps/api` member. The
#                           member's sources are not copied yet, so installing it
#                           here would fail; `--no-install-project` would only
#                           cover the root project.
RUN uv sync --locked --no-dev --no-editable --no-install-workspace

# Member sources + Alembic config, then install the member into the venv.
COPY apps/api/src /app/apps/api/src
COPY apps/api/alembic.ini /app/apps/api/alembic.ini
COPY apps/api/migrations /app/apps/api/migrations
COPY apps/api/README.md /app/apps/api/README.md

RUN uv sync --locked --no-dev --no-editable

# Build-time sanity checks — a broken import must fail the build, not the deploy:
#   1. the factory is importable and callable;
#   2. no dev-only tooling leaked into the runtime environment.
RUN /app/.venv/bin/python -c "from revnote_api.main import create_app; assert callable(create_app)" \
    && /app/.venv/bin/python -c "import importlib.util as u; assert u.find_spec('pytest') is None, 'dev dependencies leaked into the runtime image'"

# --------------------------------------------------------------------------- #
# Stage 2 — runtime: same base, no uv, no compiler, no lock file.
# --------------------------------------------------------------------------- #
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS runtime

LABEL org.opencontainers.image.title="revnote-api" \
      org.opencontainers.image.description="Revnote API — FastAPI scaffold (liveness endpoints only)"
# NOTE: no `org.opencontainers.image.licenses` label on purpose. The repository
# ships no LICENSE file and no license field in either pyproject.toml, so any
# value here would be an unverified claim. Add the label together with a real
# LICENSE file, and keep it in sync with .github/workflows/ci.yml.

# Runtime environment:
#   * NO_COLOR — keep `docker logs` / CI output free of ANSI escapes.
#   * APP_ENV=production — the app's accepted mode literal; keeps dev-only
#     behaviour (e.g. reload) off. Configuration still comes from the process
#     environment only: no `.env` is present in this image.
#   * HOST/PORT — bound by the CMD below. They are deliberately NOT the app's
#     SERVER_HOST/SERVER_PORT settings: those configure the `revnote-api`
#     console script (`python -m revnote_api`), whose default is 127.0.0.1 and
#     therefore unusable inside a container. The image starts uvicorn directly so
#     it can listen on 0.0.0.0 while the app's own defaults stay dev-friendly.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NO_COLOR=1 \
    PATH=/app/.venv/bin:$PATH \
    APP_ENV=production \
    HOST=0.0.0.0 \
    PORT=8000

# Unprivileged numeric user: predictable for volume permissions and for
# `runAsNonRoot` checks.
RUN groupadd --system --gid 10001 revnote \
    && useradd --system --uid 10001 --gid 10001 \
       --home-dir /app --shell /usr/sbin/nologin revnote

WORKDIR /app

# Only the prepared virtualenv plus the Alembic config/migrations are needed.
# `--chown` keeps everything owned by the runtime user.
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/apps/api/alembic.ini /app/apps/api/alembic.ini
COPY --from=builder --chown=10001:10001 /app/apps/api/migrations /app/apps/api/migrations

USER 10001:10001

EXPOSE 8000

# Probe the real liveness endpoint with the stdlib (the slim base has no curl).
# `/health` is a pure process check that never touches PostgreSQL or MinIO, so a
# database outage can never turn into a container restart loop. The port comes
# from PORT, with the image default as a fallback.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request;port=os.environ.get('PORT') or '8000';sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+port+'/health',timeout=4).status==200 else 1)"]

# App factory, not a module-level app object, so settings can be injected and it
# matches the documented local command. Shell form so HOST/PORT can be overridden
# at run time; `exec` makes uvicorn PID 1 so SIGTERM reaches it directly.
CMD ["/bin/sh", "-c", "exec uvicorn revnote_api.main:create_app --factory --host \"${HOST}\" --port \"${PORT}\""]
