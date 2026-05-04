# Cloud Run image for the Budget Coach API.
# Slim Python base + binary psycopg wheel = small image, no native build chain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Install dependencies first so docker layer caching survives source-only edits.
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

COPY app /app/app
COPY scripts /app/scripts

EXPOSE 8080

# Cloud Run sends SIGTERM for graceful shutdown; uvicorn handles it natively.
CMD ["python", "-m", "app.api.main"]
