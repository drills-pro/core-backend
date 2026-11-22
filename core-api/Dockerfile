FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
# The environment is built from uv.lock (a stale lock fails the build) without the dev group, and `uv run`/`uv sync` never
# touch it again at start. Bytecode is compiled at build time so a pod does not pay for it on every start.
ENV UV_FROZEN=1 UV_NO_DEV=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY core-api/pyproject.toml core-api/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-install-project

COPY core-api/app ./app
COPY core-api/config ./config
# The Ray job package: app/integrations/data_plane/job_package.py zips it, stages it and the ingest job downloads it.
# job_package.py finds it four levels up from /app/app/integrations/data_plane, the same relative path as in the checkout. Not installed here.
COPY ray-jobs/ray_jobs /ray-jobs/ray_jobs
# ray_core, the train driver and the contract task bundles run on (the ray_training package), staged the same way.
COPY ray-training/ray_core /ray-training/ray_core
# The shared Serve apps (ray_serving), staged the same way.
COPY ray-serving/ray_serving /ray-serving/ray_serving
# The shared kernel each staged zip carries beside its own package; build() reads it from here outside prod.
COPY ray-common/ray_common /ray-common/ray_common
# Read by the cluster's backend-migrate Job: an init container runs `cp -R /migrations/. /work/` and
# dbmate (its own image) applies them. This image ships no migration runner.
COPY core-api/migrations /migrations

# The image is the production one; compose and local runs override this (config/*.yaml).
ENV APP_ENV=prod

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
