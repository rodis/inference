import os

import redis

from inference.events import Envelope
from inference.pipeline.draft import DerivedDraft


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
        # Full contributor bodies, so the enrichment pipeline can shape the derived
        # event from its contributors (e.g. geolocation). The ZSET only holds
        # member strings; this HASH holds the message payloads, pruned in lockstep.
        # An implementation detail of this engine — not part of the engine contract.
        self.hash_key = f"inference:{name}:contributors"

        cfg = redis_config if redis_config is not None else _redis_config_from_env()
        # decode_responses=True so Redis returns str instead of bytes
        self.redis = redis.Redis(**cfg, decode_responses=True, ssl=True)

    def decide(self, payload: Envelope) -> DerivedDraft | None:
        message = payload.message
        event_name = message.event_name

        # gatekeeper: drop immediately if this event type isn't tracked by this engine
        if event_name not in self.weights:
            return None

        ts = int(message.timestamp)
        member = f"{event_name}:{ts}"

        # add (ZSET member + full envelope), prune the window, and fetch in one round-trip.
        # The HASH stores the whole contributing Envelope so enrichers get full context.
        with self.redis.pipeline() as pipe:
            pipe.zadd(self.zset_key, {member: ts})
            pipe.hset(self.hash_key, member, payload.model_dump_json())
            pipe.zremrangebyscore(self.zset_key, 0, ts - self.window)
            pipe.zrange(self.zset_key, 0, -1, withscores=True)
            _, _, _, active_raw = pipe.execute()

        # prune the HASH to match the surviving ZSET members (HASH has no score prune)
        survivors = {m for m, _ in active_raw}
        stale = set(self.redis.hkeys(self.hash_key)) - survivors
        if stale:
            self.redis.hdel(self.hash_key, *stale)

        # deduplicate by event type, keeping the earliest occurrence of each
        unique: dict[str, tuple[float, str]] = {}
        for m, score_ts in active_raw:
            etype = m.split(":")[0]
            if etype not in unique:
                unique[etype] = (score_ts, m)

        current_score = sum(self.weights.get(e, 0) for e in unique)

        if current_score < self.threshold:
            return None

        # SET NX EX is atomic — avoids the race condition of a separate exists() + setex()
        if not self.redis.set(self.lock_key, "active", nx=True, ex=self.cooldown):
            return None

        # fetch the contributing envelopes and assemble the draft (core + contributors).
        # Shaping (lineage, geo, ...) is the enrichment pipeline's job, not the engine's.
        bodies = self.redis.hmget(self.hash_key, [m for _, m in unique.values()])
        contributors = tuple(
            Envelope.model_validate_json(body) for body in bodies if body is not None
        )
        avg_ts = sum(score_ts for score_ts, _ in unique.values()) / len(unique)

        return DerivedDraft(
            inference_type=self.inference_type,
            event_name=self.inference_type,
            confidence_score=current_score,
            occurred_at=avg_ts,
            contributors=contributors,
        )
