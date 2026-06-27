"""Deployed entrypoint — generic Quix Streams runtime (ADR 0004).

Pure launcher; all logic lives in the installed `inference.runtime.quix` module.

Locally, env/secrets come from `workers/.env` (run from inside the `workers/` tree
so `find_dotenv` finds it). In K8s the same vars come from the ConfigMap/Secret and
`find_dotenv` returns "" (skipped).
"""

import logging

from dotenv import find_dotenv, load_dotenv

if dotenv_path := find_dotenv(usecwd=True, raise_error_if_not_found=False):
    load_dotenv(dotenv_path)

from inference.runtime.quix import run  # noqa: E402  (after dotenv)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    run()
