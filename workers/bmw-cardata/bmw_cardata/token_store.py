"""Persist the rotating refresh token to Neon (ADR 0006 follow-up).

BMW rotates the refresh token on every refresh (~hourly). Without persistence, a pod
restart re-reads the (now-invalid) bootstrap token from the Secret and CrashLoops on
auth. We persist the latest token to Neon — external managed state, consistent with the
no-in-cluster-persistence rule — so restarts resume with a valid token.

Best-effort: read failures fall back to the bootstrap token; write failures log loudly
(the next restart would then need a re-auth) but never crash the running pod.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class NeonTokenStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def load(self, client_id: str) -> str | None:
        try:
            import psycopg

            with psycopg.connect(self._dsn, connect_timeout=10) as conn:
                row = conn.execute(
                    "SELECT refresh_token FROM bmw_cardata_tokens WHERE client_id = %s",
                    (client_id,),
                ).fetchone()
                return row[0] if row else None
        except Exception as exc:  # noqa: BLE001 — best-effort; fall back to bootstrap
            log.warning("token store load failed (falling back to bootstrap token): %s", exc)
            return None

    def save(self, client_id: str, refresh_token: str, gcid: str | None = None) -> None:
        try:
            import psycopg

            with psycopg.connect(self._dsn, connect_timeout=10) as conn:
                conn.execute(
                    "INSERT INTO bmw_cardata_tokens (client_id, refresh_token, gcid, updated_at) "
                    "VALUES (%s, %s, %s, now()) "
                    "ON CONFLICT (client_id) DO UPDATE SET "
                    "refresh_token = EXCLUDED.refresh_token, gcid = EXCLUDED.gcid, updated_at = now()",
                    (client_id, refresh_token, gcid),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 — don't crash the pod on a write failure
            log.error("token store save FAILED — rotated token not persisted (restart will need re-auth): %s", exc)
