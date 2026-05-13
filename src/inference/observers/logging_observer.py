import logging


class InferenceObserver:
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)

    def on_start(self, topics: list[str]) -> None:
        self.logger.info(f"Starting, subscribed to topics: {topics}")

    def on_received(self, payload: dict) -> None:
        self.logger.debug(f"Received event: {payload}")

    def on_inference(self, result: dict) -> None:
        self.logger.info(
            f"SUCCESS: Triggered {result['inference_type']} with score {result['message']['confidence_score']}"
        )

    def on_error(self, error: Exception, context: str | None = None) -> None:
        self.logger.error(f"ERROR: {str(error)} | Context: {context}", exc_info=True)

    def on_shutdown(self) -> None:
        self.logger.info("Shutdown signal received, stopping")
