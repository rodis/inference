import json
import signal

from confluent_kafka import Consumer, KafkaException
from pydantic import ValidationError

from inference.engines.protocol import InferenceEngine
from inference.events import Envelope
from inference.observers.protocol import Observer
from inference.pipeline.runner import EnrichmentPipeline
from inference.transport.protocol import Emitter


class KafkaStreamHandler:
    def __init__(
        self,
        kafka_consumer: Consumer,
        engine: InferenceEngine,
        observer: Observer,
        emitter: Emitter,
        pipeline: EnrichmentPipeline,
    ):
        self.consumer = kafka_consumer
        self.engine = engine
        self.observer = observer
        self.emitter = emitter
        self.pipeline = pipeline
        self._running = False

    def start(self, source_topics: list[str]) -> None:
        self.consumer.subscribe(source_topics)
        self._running = True
        self.observer.on_start(source_topics)

        # SIGTERM is the standard K8s pod shutdown signal; SIGINT handles local Ctrl+C
        signal.signal(signal.SIGTERM, lambda *_: self._shutdown())
        signal.signal(signal.SIGINT, lambda *_: self._shutdown())

        try:
            while self._running:
                # guard against poll() raising before assignment
                msg = None
                try:
                    msg = self.consumer.poll(1.0)
                    if msg is None:
                        # poll() returns None on timeout — no message available, keep looping
                        continue

                    if msg.error():
                        # KafkaError is not an Exception subclass; wrap it so the Observer type contract holds
                        self.observer.on_error(KafkaException(msg.error()))
                        continue

                    payload = Envelope.model_validate_json(msg.value())
                    self.observer.on_received(payload)

                    draft = self.engine.decide(payload)

                    if draft is not None:
                        # engine decided + assembled the core; the pipeline shapes the
                        # message (lineage, geo, ...) and finalizes it to a transport dict
                        result = self.pipeline.run(draft)
                        self.observer.on_inference(result)
                        try:
                            self.emitter.emit(result)
                        except Exception as e:
                            self.observer.on_error(e, "Emit failed")
                            # skip-and-move-on: commit even when emit fails so the offset advances
                            self.consumer.commit(message=msg)
                            continue

                    # manual commit after successful processing (enable.auto.commit=False)
                    self.consumer.commit(message=msg)

                except (json.JSONDecodeError, ValidationError) as e:
                    self.observer.on_error(e, "Invalid envelope received")
                    # skip-and-move-on: commit the malformed message to avoid an infinite retry loop
                    if msg:
                        self.consumer.commit(message=msg)
                except Exception as e:
                    self.observer.on_error(e, "Engine processing failed")
                    # skip-and-move-on: same strategy for unexpected engine errors
                    if msg:
                        self.consumer.commit(message=msg)
        finally:
            self.consumer.close()

    def _shutdown(self):
        self._running = False
        self.observer.on_shutdown()
