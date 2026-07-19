"""Deployed entrypoint — BMW CarData subscriber (ADR 0006).

Wires config → token refresh → MQTT subscribe → Vector ingest, then runs a refresh
loop: the ID token (the MQTT password) expires ~hourly, so we refresh a few minutes
early and reconnect with the new password.

Locally, env/secrets come from workers/.env (run from inside the workers/ tree so
find_dotenv finds it). In K8s the same vars come from the ConfigMap/Secret.
"""

from __future__ import annotations

import logging
import time

from dotenv import find_dotenv, load_dotenv

if dotenv_path := find_dotenv(usecwd=True, raise_error_if_not_found=False):
    load_dotenv(dotenv_path)

from bmw_cardata.auth import TokenManager  # noqa: E402
from bmw_cardata.config import Config  # noqa: E402
from bmw_cardata.ingest import Ingest  # noqa: E402
from bmw_cardata.mapper import Mapper  # noqa: E402
from bmw_cardata.mqtt_client import MqttSubscriber  # noqa: E402

log = logging.getLogger("bmw_cardata")


def run() -> None:
    cfg = Config.from_env()
    token = TokenManager(cfg.client_id, cfg.refresh_token, cfg.token_url)
    token.refresh()  # initial — fails fast if creds are bad / account pending activation

    ingest = Ingest(cfg.vector_base_url, cfg.ingest_path, cfg.user_id)
    subscriber = MqttSubscriber(cfg, token, Mapper(), ingest)
    subscriber.start()

    try:
        while True:
            # Sleep until just before the id_token expires, then refresh + reconnect.
            wait = max(60, token.seconds_until_expiry() - cfg.refresh_margin_seconds)
            time.sleep(wait)
            token.refresh()
            subscriber.reconnect_with_fresh_token()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        subscriber.stop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    run()
