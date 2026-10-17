# MLflow tracking server for compose. psycopg 3, matching the postgresql+psycopg:// store URI in
# docker-compose.yml (current SQLAlchemy loads psycopg 3 for a bare postgresql://).
FROM python:3.12-slim

RUN pip install --no-cache-dir mlflow "psycopg[binary]" boto3

EXPOSE 5000
