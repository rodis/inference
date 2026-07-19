"""Environment-driven config for the BMW CarData subscriber (ADR 0006).

All values come from env (workers/.env locally; ConfigMap/Secret in K8s). The
secrets — client id, refresh token — are the output of the one-time OAuth2 device
code flow (see the repo-root onboarding notes); this worker only ever *refreshes*.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """A required env var is missing/blank."""


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"missing required env var: {name}")
    return val


def _default(name: str, fallback: str) -> str:
    return os.environ.get(name, "").strip() or fallback


@dataclass(frozen=True)
class Config:
    # --- OAuth (device-code-flow output; we only refresh) ---
    client_id: str
    refresh_token: str
    token_url: str

    # --- MQTT streaming (CONFIRM host/port/topic against Integration Guide ch. 3.3.2) ---
    mqtt_host: str
    mqtt_port: int
    # {gcid} is filled from the token response at runtime; {vin} from vin below.
    topic_template: str

    # --- Vehicle + identity ---
    vin: str
    # The pipeline keys per-entity state on message.user_id (ADR 0004). One car → one
    # user_id here; multi-vehicle would become a VIN→user_id JSON map (see README).
    user_id: str

    # --- Vector ingest (standard sensors lane; no new Vector transform — see README) ---
    # Subscriber POSTs canonical events to {vector_base_url}{ingest_path}.
    vector_base_url: str
    ingest_path: str

    # Seconds before id_token expiry to proactively refresh + reconnect (id_token is the
    # MQTT password and lives ~3600s; refresh a few minutes early).
    refresh_margin_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            client_id=_require("BMW_CLIENT_ID"),
            refresh_token=_require("BMW_REFRESH_TOKEN"),
            token_url=_default("BMW_TOKEN_URL", "https://customer.bmwgroup.com/gcdm/oauth/token"),
            mqtt_host=_default("BMW_MQTT_HOST", "customer.streaming-cardata.bmwgroup.com"),
            mqtt_port=int(_default("BMW_MQTT_PORT", "9000")),
            # Confirmed: BMW publishes under {gcid}/… ; kvanbiesen subscribes {gcid}/+ (all
            # VINs on the account). Single car → the wildcard is fine; the payload carries the vin.
            topic_template=_default("BMW_TOPIC_TEMPLATE", "{gcid}/+"),
            vin=_require("BMW_VIN"),
            user_id=_require("BMW_USER_ID"),
            vector_base_url=_require("VECTOR_BASE_URL").rstrip("/"),
            ingest_path=_default("BMW_INGEST_PATH", "/sensors/bmw"),
            refresh_margin_seconds=int(_default("BMW_REFRESH_MARGIN_SECONDS", "300")),
        )
