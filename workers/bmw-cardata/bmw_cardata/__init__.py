"""BMW CarData subscriber — car-native trip signals into the pipeline (ADR 0006).

Thin transport + auth only: OAuth token refresh + MQTT subscribe + map descriptor
edges to canonical signals + POST to Vector ingest. NO trip logic here — that stays in
the inference engines (got_into_the_car / got_out_the_car / car_trip).
"""
