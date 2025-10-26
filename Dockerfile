FROM python:3.12-slim

WORKDIR /app

COPY unison-orchestrator/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r ./requirements.txt

# Install unison-common from the monorepo
COPY unison-common /tmp/unison-common
RUN pip install --no-cache-dir -e /tmp/unison-common

# Copy orchestrator source
COPY unison-orchestrator/src ./src

EXPOSE 8080
CMD ["python", "src/server.py"]
