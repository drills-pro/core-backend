FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY core-api/pyproject.toml .
RUN uv sync --no-install-project

COPY core-api/app ./app
COPY core-api/config ./config
# The Ray job package: app/integrations/data_plane/job_package.py zips it, stages it and the ingest job downloads it.
# job_package.py finds it four levels up from /app/app/integrations/data_plane, the same relative path as in the checkout. Not installed here.
COPY ray-jobs/ray_jobs /ray-jobs/ray_jobs
# Read by the cluster's backend-migrate Job: an init container runs `cp -R /migrations/. /work/` and
# dbmate (its own image) applies them. This image ships no migration runner.
COPY core-api/migrations /migrations
RUN uv sync

# The image is the production one; compose and local runs override this (config/*.yaml).
ENV APP_ENV=prod

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
