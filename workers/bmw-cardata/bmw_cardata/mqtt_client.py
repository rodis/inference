"""MQTT subscriber for the BMW CarData stream (ADR 0006).

BMW hosts the broker; we subscribe (one connection per GCID — this worker is the SOLE
subscriber, HA's direct subscription retired). Auth is MQTT v5 over TLS with
username=GCID, password=the current ID token (which rotates hourly — on refresh we
re-set the password and reconnect).

CONFIRM against Integration Guide ch. 3.3.2: host/port (default
customer.streaming-cardata.bmwgroup.com:9000), the topic (`{gcid}/{vin}`), and that the
payload is JSON.
"""

from __future__ import annotations

import json
import logging
import ssl

import paho.mqtt.client as mqtt

from .auth import TokenManager
from .config import Config
from .ingest import Ingest
from .mapper import Mapper

log = logging.getLogger(__name__)


class MqttSubscriber:
    def __init__(self, cfg: Config, token: TokenManager, mapper: Mapper, ingest: Ingest):
        self._cfg = cfg
        self._token = token
        self._mapper = mapper
        self._ingest = ingest
        self._topic = cfg.topic_template.format(gcid=token.gcid, vin=cfg.vin)

        # Connection params CONFIRMED against BMW's broker (2026-07-20) + the kvanbiesen
        # integration: MQTT v3.1.1 (NOT v5) and TLS 1.3 *minimum* (the broker rejects lower
        # with a TLSV1_ALERT_PROTOCOL_VERSION — needs OpenSSL 3 / Python 3.13, not macOS LibreSSL).
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv311,
            client_id=f"inference-cardata-{cfg.vin}",
        )
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        self._client.tls_set_context(ctx)
        self._client.tls_insecure_set(False)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._apply_auth()

    def _apply_auth(self) -> None:
        # Username = GCID, password = current ID token (the rotating secret).
        self._client.username_pw_set(self._token.gcid, self._token.id_token)

    # --- lifecycle ---
    def start(self) -> None:
        log.info("connecting to mqtt %s:%s topic=%s", self._cfg.mqtt_host, self._cfg.mqtt_port, self._topic)
        self._client.connect(self._cfg.mqtt_host, self._cfg.mqtt_port, keepalive=30)
        self._client.loop_start()  # background network thread

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def reconnect_with_fresh_token(self) -> None:
        """Called after TokenManager.refresh(): the MQTT password (id_token) changed."""
        log.info("re-authing mqtt with refreshed id_token")
        self._apply_auth()
        self._client.reconnect()

    # --- callbacks ---
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # paho v2 hands a ReasonCode; .is_failure is the reliable success test.
        if getattr(reason_code, "is_failure", reason_code != 0):
            log.error("mqtt connect failed: %s", reason_code)
            return
        client.subscribe(self._topic, qos=1)
        log.info("connected (%s); subscribed to %s", reason_code, self._topic)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        # paho's loop auto-reconnects; log so drops are visible (BMW stream is flaky — ADR 0006).
        log.warning("mqtt disconnected: %s (auto-reconnect)", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            raw = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            log.error("undecodable mqtt payload on %s: %s", msg.topic, exc)
            return
        for event_name, ts, extra in self._mapper.process(raw):
            self._ingest.post(event_name, ts, extra)
