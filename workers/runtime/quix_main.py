"""Deployed entrypoint — generic Quix Streams runtime (ADR 0004).

Replaces `workers/runtime/main.py` (the threaded `RuntimeSupervisor`) as the image
CMD. Pure launcher; all logic lives in the installed `inference.runtime.quix` module.
"""

import logging

from inference.runtime.quix import run

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    run()
