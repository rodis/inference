"""Run many handlers in one process, one thread each.

Each `KafkaStreamHandler` owns a blocking poll loop and its own consumer group,
so handlers stay isolated (one event's lag/rebalance doesn't couple to another's)
— the smallest semantic change from one-consumer-per-pod. The work is IO-bound
(Kafka poll + Redis), so the GIL is not the bottleneck.

Signals are handled here, in the main thread (signal.signal() can't run in a
worker thread): SIGTERM/SIGINT asks every handler to stop, then we join.
"""

import logging
import signal
import threading

from inference.runtime.builder import Handler

logger = logging.getLogger(__name__)


class RuntimeSupervisor:
    def __init__(self, handlers: list[Handler]):
        self._handlers = handlers
        self._threads: list[threading.Thread] = []

    def run(self) -> None:
        if not self._handlers:
            logger.warning("No handlers to run — no enabled event definitions loaded.")
            return

        signal.signal(signal.SIGTERM, lambda *_: self._stop_all())
        signal.signal(signal.SIGINT, lambda *_: self._stop_all())

        for handler in self._handlers:
            thread = threading.Thread(
                target=handler.stream_handler.start,
                args=(handler.source_topics,),
                kwargs={"handle_signals": False},
                name=handler.name,
                daemon=False,
            )
            self._threads.append(thread)
            thread.start()
            logger.info("Started handler '%s' on %s", handler.name, handler.source_topics)

        # Block the main thread on the handler threads; signals interrupt the joins.
        for thread in self._threads:
            thread.join()

    def _stop_all(self) -> None:
        logger.info("Stopping %d handler(s)", len(self._handlers))
        for handler in self._handlers:
            handler.stream_handler.stop()
