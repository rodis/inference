"""Runtime configuration.

Pure constants live here; environment-backed settings are read from env — `workers/.env`
locally (loaded by `quix_main`), or the K8s ConfigMap/Secret + image ENV in the cluster.
The required broker address and the mTLS paths are read **lazily** (via functions) so
this module (and `inference.runtime.quix`) imports without a full environment, e.g. for tests.
"""

import os
from pathlib import Path

# Producing application — recorded as `source_app` on derived events. Raw events carry
# their own producer (e.g. "shortcut"); derived events all share this one value.
APP_NAME = "inference"

# Env-backed settings with safe defaults (the defaults are baked into the image / .env).
EVENTS_DIR = Path(os.environ.get("EVENTS_DIR", "events"))
# Bumped v1 -> v2 with the InferredEvent shaping change: engine state format changed
# ({ts,id} -> {ts,event}), so a fresh group = a fresh changelog = fresh, self-healing
# state. The old v1 changelog is orphaned (harmless; delete to reclaim the topic slot).
CONSUMER_GROUP = os.environ.get("QUIX_CONSUMER_GROUP", "inference-quix-runtime-v2")
STATE_DIR = os.environ.get("QUIX_STATE_DIR", "state")


def kafka_bootstrap() -> str:
    """Kafka bootstrap servers (required; read lazily so importing doesn't need it)."""
    return os.environ["KAFKA_BOOTSTRAP_SERVERS"]


def kafka_ssl() -> dict:
    """librdkafka mTLS config. Defaults match the kafka-ssl Secret volume mount in
    deploy/inference/kustomize/base/runtime/values.yml.
    """
    return {
        "security.protocol": "SSL",
        "ssl.ca.location": os.environ.get("KAFKA_SSL_CA_PATH", "/etc/kafka/ssl/ca-cert.pem"),
        "ssl.certificate.location": os.environ.get("KAFKA_SSL_CERT_PATH", "/etc/kafka/ssl/access-cert.pem"),
        "ssl.key.location": os.environ.get("KAFKA_SSL_KEY_PATH", "/etc/kafka/ssl/access-key.pem"),
    }
