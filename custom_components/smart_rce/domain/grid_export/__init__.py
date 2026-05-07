"""Grid Export package — POSITIVE/NEGATIVE intervention orchestration.

Public API (re-exports for backward compat with `from .domain.grid_export
import GridExportManager, InterventionDirection`):
- GridExportManager — orchestrator
- InterventionDirection — enum (POSITIVE | NEGATIVE)

Internal modules (import directly when needed):
- intervention — Protocol + EntryResult/ContinueResult VOs
- positive — PositiveIntervention entity + module constants (BALANCE_GATE_KWH)
- negative — NegativeIntervention entity + module constants + entry_threshold()
- manager — GridExportManager
"""

from custom_components.smart_rce.domain.grid_export.intervention import (
    InterventionDirection,
)
from custom_components.smart_rce.domain.grid_export.manager import GridExportManager

__all__ = ["GridExportManager", "InterventionDirection"]
