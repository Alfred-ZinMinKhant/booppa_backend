"""
pdpa_declaration_models.py — import shim for PdpaSelfDeclaration.

The model is defined in `models_v12.py` (picked up by metadata via the single
`from .models_v12 import *` in models.py). Sprint code imports it from
`app.core.pdpa_declaration_models`, so this re-exports it from its canonical home.
"""
from app.core.models import PdpaSelfDeclaration

__all__ = ["PdpaSelfDeclaration"]
