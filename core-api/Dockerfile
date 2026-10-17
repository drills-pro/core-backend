FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml .
RUN uv sync --no-install-project

COPY app ./app
COPY config ./config
# Read by the cluster's backend-migrate Job: an init container runs `cp -R /migrations/. /work/` and
# dbmate (its own image) applies them. This image ships no migration runner.
COPY migrations /migrations
RUN uv sync

# The image is the production one; compose and local runs override this (config/*.yaml).
ENV APP_ENV=prod

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
