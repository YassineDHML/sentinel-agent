"""Competitor comparison report (capability 2).

The engine lives in :mod:`sentinel.research.competitors`; this package provides the
``python -m sentinel.competitors`` entry point.
"""

from ..research.competitors import CompetitorDossier, CompetitorReport, run_competitor_report

__all__ = ["run_competitor_report", "CompetitorReport", "CompetitorDossier"]
