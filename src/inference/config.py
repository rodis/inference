import os

from dotenv import find_dotenv, load_dotenv

# find_dotenv(usecwd=True) walks upward from CWD — run from within the workers/
# tree so workers/.env is found. Returns '' when absent (K8s), so guard before loading.
if dotenv_path := find_dotenv(usecwd=True, raise_error_if_not_found=False):
    load_dotenv(dotenv_path)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable '{name}' is not set")
    return value


KAFKA_BOOTSTRAP_SERVERS = _require("KAFKA_BOOTSTRAP_SERVERS")

VECTOR_BASE_URL = _require("VECTOR_BASE_URL")

# Kafka SSL cert file paths — default to the K8s Secret volume mount location.
# Override via env vars when running locally (point to cert files on disk).
KAFKA_SSL_CA_PATH   = os.environ.get("KAFKA_SSL_CA_PATH",   "/etc/kafka/ssl/ca-cert.pem")
KAFKA_SSL_CERT_PATH = os.environ.get("KAFKA_SSL_CERT_PATH", "/etc/kafka/ssl/access-cert.pem")
KAFKA_SSL_KEY_PATH  = os.environ.get("KAFKA_SSL_KEY_PATH",  "/etc/kafka/ssl/access-key.pem")

REDIS_CONFIG = {
    "host":     _require("REDIS_HOST"),
    "port":     int(_require("REDIS_PORT")),
    "db":       int(os.environ.get("REDIS_DB", "0")),
    "username": os.environ.get("REDIS_USERNAME", "default"),
    "password": _require("REDIS_PASSWORD"),
}
