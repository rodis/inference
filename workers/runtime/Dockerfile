FROM python:3.13-slim

WORKDIR /app

# Install the inference package and its dependencies. The runtime is a single
# Quix Streams Application (ADR 0004); quixstreams is a base dependency.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Generic runtime entrypoint + the event definitions it loads at startup.
# Definitions are baked into the image for now (ADR 0003 Phase 1); Phase 2 moves
# them to a mounted ConfigMap so a YAML edit rolls the pod without a rebuild.
# (workers/runtime/main.py — the old threaded entrypoint — stays in the repo for
#  reference/rollback but is no longer the CMD.)
COPY workers/runtime/quix_main.py ./workers/runtime/quix_main.py
COPY events/ ./events/

# Runtime config (overridable by the Deployment env). State is ephemeral local
# RocksDB recovered from the Kafka changelog — /tmp is writable by the non-root user.
ENV EVENTS_DIR=/app/events \
    QUIX_STATE_DIR=/tmp/quix-state \
    QUIX_CONSUMER_GROUP=inference-quix-runtime-v1

RUN useradd -u 1000 -m worker
USER 1000

CMD ["python", "workers/runtime/quix_main.py"]
