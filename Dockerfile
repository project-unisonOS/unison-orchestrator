FROM python:3.12-slim

WORKDIR /app

COPY unison-orchestrator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r ./requirements.txt

# Copy orchestrator source
COPY unison-orchestrator/src ./src
# Copy vendored minimal unison_common into src so it's on sys.path
COPY unison-orchestrator/unison_common ./src/unison_common

# Ensure Python can import from /app/src
ENV PYTHONPATH=/app/src

EXPOSE 8080
CMD ["python", "src/server.py"]
