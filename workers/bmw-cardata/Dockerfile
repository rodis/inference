FROM python:3.13-slim

WORKDIR /app

# ca-certificates: the MQTT client verifies BMW's broker TLS against the system CA bundle.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Standalone worker (ADR 0006) — its own deps, NOT the inference package.
COPY workers/bmw-cardata/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY workers/bmw-cardata/main.py ./main.py
COPY workers/bmw-cardata/bmw_cardata/ ./bmw_cardata/

RUN useradd -u 1000 -m worker
USER 1000

CMD ["python", "main.py"]
