import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


class VectorHttpEmitter:
    def __init__(self, url: str):
        self.url = url

    def emit(self, event: dict) -> None:
        body = json.dumps(event).encode("utf-8")
        logger.debug("Emitting to %s: %s", self.url, body.decode("utf-8"))

        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            logger.debug("Vector responded %s", resp.status)
