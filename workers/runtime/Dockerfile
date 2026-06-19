FROM python:3.13-slim

WORKDIR /app

# Install the inference package and its dependencies
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Generic runtime entrypoint + the event definitions it loads at startup.
# Definitions are baked into the image for now (ADR 0003 Phase 1); Phase 2 moves
# them to a mounted ConfigMap so a YAML edit rolls the pod without a rebuild.
COPY workers/runtime/main.py ./workers/runtime/main.py
COPY events/ ./events/

RUN useradd -u 1000 -m worker
USER 1000

CMD ["python", "workers/runtime/main.py"]
