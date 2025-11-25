FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY unison-orchestrator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r ./requirements.txt \
    && pip install --no-cache-dir redis python-jose[cryptography] bleach httpx[http2]

# Copy orchestrator source (from monorepo root context)
COPY unison-orchestrator/src ./src
COPY unison-orchestrator/tests ./tests
# Copy shared unison_common into src so it's on sys.path
COPY unison-common/src/unison_common ./src/unison_common

# Ensure Python can import from /app/src
ENV PYTHONPATH=/app/src

EXPOSE 8080
CMD ["python", "src/server.py"]
