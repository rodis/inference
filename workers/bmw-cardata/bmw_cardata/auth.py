"""OAuth token management — the "hard machinery" that stays on our side (ADR 0006).

The subscriber authenticates to BMW's MQTT broker with the **ID token as the password**,
and that token expires ~hourly. This module owns the refresh loop: it exchanges the
long-lived refresh token (2-week life, from the one-time device-code flow) for a fresh
access/ID token set and exposes the current ID token + GCID to the MQTT client.

Endpoint/params follow the CarData OAuth spec (swagger-device-code-flow.json); the
refresh_token grant itself is standard OAuth2. VERIFY the refresh grant shape against
BMW's auth swagger when available.
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class TokenError(RuntimeError):
    pass


class TokenManager:
    def __init__(self, client_id: str, refresh_token: str, token_url: str, store=None):
        self._client_id = client_id
        self._bootstrap_refresh_token = refresh_token  # from the Secret; may be stale
        self._token_url = token_url
        self._store = store  # optional NeonTokenStore — survives restarts
        self._refresh_token: str | None = None  # most-recent (in-memory), set on refresh
        self._id_token: str | None = None
        self._access_token: str | None = None
        self._gcid: str | None = None
        self._expires_at: float = 0.0

    def _resolve_refresh_token(self) -> str:
        """Most-recent in-memory token > persisted (store) > bootstrap (Secret)."""
        if self._refresh_token:
            return self._refresh_token
        if self._store:
            stored = self._store.load(self._client_id)
            if stored:
                log.info("using persisted refresh token from store")
                return stored
        log.info("using bootstrap refresh token (no persisted token yet)")
        return self._bootstrap_refresh_token

    @property
    def id_token(self) -> str:
        if not self._id_token:
            raise TokenError("no id_token yet — call refresh() first")
        return self._id_token

    @property
    def gcid(self) -> str:
        if not self._gcid:
            raise TokenError("no gcid yet — call refresh() first")
        return self._gcid

    @property
    def expires_at(self) -> float:
        return self._expires_at

    def seconds_until_expiry(self) -> float:
        return self._expires_at - time.time()

    def refresh(self) -> None:
        """Exchange the refresh token for a fresh token set. Idempotent; call on a timer."""
        resp = requests.post(
            self._token_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "client_id": self._client_id,
                "grant_type": "refresh_token",
                "refresh_token": self._resolve_refresh_token(),
            },
            timeout=30,
        )
        if resp.status_code != 200:
            # 401 here can mean the refresh token expired (2 weeks) OR the client was
            # de-subscribed — both require re-running the device-code flow (ADR 0006).
            raise TokenError(f"refresh failed [{resp.status_code}]: {resp.text[:300]}")

        body = resp.json()
        self._id_token = body.get("id_token")
        self._access_token = body.get("access_token")
        self._gcid = body.get("gcid") or self._gcid
        expires_in = int(body.get("expires_in", 3600))
        self._expires_at = time.time() + expires_in

        if not self._id_token:
            raise TokenError("refresh response missing id_token (need `openid` scope)")

        # BMW may rotate the refresh token on each refresh. We keep the newest in memory,
        # but on a pod restart we fall back to the ENV one — if BMW invalidates the old
        # token on rotation, that stale env value breaks re-auth. OPEN ITEM: persist the
        # rotated refresh token (write back to a Secret / mounted file). See README.
        new_refresh = body.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            if self._store:
                self._store.save(self._client_id, new_refresh, self._gcid)
                log.info("refresh_token rotated; persisted to store")
            else:
                log.warning("refresh_token rotated; in-memory only (no store — restart will need re-auth)")

        log.info("token refreshed; gcid=%s id_token expires_in=%ss", self._gcid, expires_in)
