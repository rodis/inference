"""Deployed entrypoint — BMW CarData subscriber (ADR 0006).

Wires config → token refresh → MQTT subscribe → Vector ingest, then runs a refresh
loop: the ID token (the MQTT password) expires ~hourly, so we refresh a few minutes
early and reconnect with the new password.

Locally, env/secrets come from workers/.env (run from inside the workers/ tree so
find_dotenv finds it). In K8s the same vars come from the ConfigMap/Secret.
"""

from __future__ import annotations

import logging
import signal
import threading

from dotenv import find_dotenv, load_dotenv

if dotenv_path := find_dotenv(usecwd=True, raise_error_if_not_found=False):
    load_dotenv(dotenv_path)

from bmw_cardata.auth import TokenManager  # noqa: E402
from bmw_cardata.config import Config  # noqa: E402
from bmw_cardata.ingest import Ingest  # noqa: E402
from bmw_cardata.mapper import Mapper  # noqa: E402
from bmw_cardata.mqtt_client import MqttSubscriber  # noqa: E402

log = logging.getLogger("bmw_cardata")


def _install_shutdown(stop: threading.Event) -> None:
    """Turn SIGTERM/SIGINT into a flag the run loop watches.

    Without this, SIGTERM took Python's DEFAULT action — terminate immediately — so the
    `finally: subscriber.stop()` below never ran and the broker never received a DISCONNECT.
    It had to notice the socket had died on its own, and BMW permits only one connection per
    GCID, so the replacement pod was refused ("Not authorized") until the old session was
    reaped: ~56s on one deploy, ~17s on another (ADR 0006).

    The flag is an Event, not a bool, because the loop waits on it: a bare flag plus
    `time.sleep()` would not help. Per PEP 475 a signal handler that doesn't raise leaves
    `sleep` to RESUME for its remaining time — and that sleep runs until the ID token is
    nearly expired, so shutdown could sit idle for the best part of an hour before the
    grace period killed it anyway. Setting the Event wakes the waiter at once.
    """
    def _handle(signum, _frame):
        log.info("received %s — shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def run() -> None:
    stop = threading.Event()
    _install_shutdown(stop)
    cfg = Config.from_env()
    store = None
    if cfg.neon_database_url:
        from bmw_cardata.token_store import NeonTokenStore
        store = NeonTokenStore(cfg.neon_database_url)
    token = TokenManager(cfg.client_id, cfg.refresh_token, cfg.token_url, store=store)
    token.refresh()  # initial — fails fast if creds are bad / account pending activation

    ingest = Ingest(cfg.vector_base_url, cfg.ingest_path, cfg.user_id)
    subscriber = MqttSubscriber(cfg, token, Mapper(), ingest)
    subscriber.start()

    try:
        while not stop.is_set():
            # Wait until just before the id_token expires, then refresh + reconnect. `wait`
            # returns True the moment a signal arrives, so a rollout doesn't have to sit
            # through the remainder of an hour-long wait.
            wait = max(60, token.seconds_until_expiry() - cfg.refresh_margin_seconds)
            if stop.wait(wait):
                break
            token.refresh()
            subscriber.reconnect_with_fresh_token()
    finally:
        subscriber.stop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    run()
