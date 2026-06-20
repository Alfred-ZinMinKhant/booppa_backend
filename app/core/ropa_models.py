"""
ropa_models.py — import shim for RopaActivities.

The model itself is defined in `models_v12.py` (next to PendingRfpIntake, so
Alembic's metadata picks it up through the single `from .models_v12 import *`
in models.py). Sprint 5's API and fulfillment code import it from
`app.core.ropa_models`, so this module re-exports it from its canonical home.
"""
from app.core.models_v12 import RopaActivities

__all__ = ["RopaActivities"]
