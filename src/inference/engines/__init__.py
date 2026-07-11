"""Pluggable inference engines. Importing this package registers the built-ins."""

from inference.engines import decaying_window  # noqa: F401  (side effect: registers the engine)
from inference.engines import geofence  # noqa: F401  (side effect: registers the engine)
from inference.engines import naive_bayes_window  # noqa: F401  (side effect: registers the engine)
from inference.engines import session_window  # noqa: F401  (side effect: registers the engine)
from inference.engines import weighted_window  # noqa: F401  (side effect: registers the engine)
from inference.engines.base import (
    Decision,
    Engine,
    ScopedState,
    build_engine,
    register_engine,
)

__all__ = [
    "Decision",
    "Engine",
    "ScopedState",
    "build_engine",
    "register_engine",
]
