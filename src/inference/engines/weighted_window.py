import os
import time

import redis


def _redis_config_from_env() -> dict:
    return {
        "host":     os.environ["REDIS_HOST"],
        "port":     int(os.environ["REDIS_PORT"]),
        "db":       int(os.environ.get("REDIS_DB", "0")),
        "username": os.environ.get("REDIS_USERNAME", "default"),
        "password": os.environ["REDIS_PASSWORD"],
    }


class WeightedWindowEngine:
    def __init__(self, rules: dict, redis_config: dict | None = None):
        self.weights = rules.get("weights", {})
        self.threshold = rules["threshold"]
        self.window = rules["window_seconds"]
        self.cooldown = rules.get("cooldown_seconds", 1800)

        name = rules["name"]
        self.inference_type = name
        self.zset_key = f"inference:{name}:buffer"
        self.lock_key = f"inference:{name}:lock"

        cfg = redis_config if redis_config is not None else _redis_config_from_env()
        # decode_responses=True so Redis returns str instead of bytes
        self.redis = redis.Redis(**cfg, decode_responses=True, ssl=True)

    def process(self, payload: dict) -> dict | None:
        message = payload.get("message", {})
        event_name = message.get("event_name")

        # gatekeeper: drop immediately if this event type isn't tracked by this engine
        if event_name not in self.weights:
            return None

        ts = int(message.get("timestamp", time.time()))

        # add, prune, and fetch in one round-trip
        with self.redis.pipeline() as pipe:
            pipe.zadd(self.zset_key, {f"{event_name}:{ts}": ts})
            pipe.zremrangebyscore(self.zset_key, 0, ts - self.window)
            pipe.zrange(self.zset_key, 0, -1, withscores=True)
            _, _, active_raw = pipe.execute()

        # deduplicate by event type, keeping the earliest occurrence of each
        unique_contributors: dict[str, float] = {}
        for member, score_ts in active_raw:
            etype = member.split(":")[0]
            if etype not in unique_contributors:
                unique_contributors[etype] = score_ts

        current_score = sum(self.weights.get(e, 0) for e in unique_contributors)

        if current_score < self.threshold:
            return None

        # SET NX EX is atomic — avoids the race condition of a separate exists() + setex()
        if not self.redis.set(self.lock_key, "active", nx=True, ex=self.cooldown):
            return None

        avg_ts = sum(unique_contributors.values()) / len(unique_contributors)

        return {
            "inference_type": self.inference_type,
            "processed_at": time.time(),
            "message": {
                "event_name": self.inference_type,
                "confidence_score": current_score,
                "occurred_at": avg_ts,
                "sources": list(unique_contributors.keys()),
                "evidence": unique_contributors,
            },
        }
