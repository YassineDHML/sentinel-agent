"""Reporting layer: assembles + renders the weekly HTML report (spec BF-05).

Five sections: executive summary, competitive watch, trends detected (new +
accelerating, with historical context), opportunities (cited), sources
consulted. Narrative sections come from the Gemini-only deep analysis; trends
and sources are computed deterministically from stored data.
"""

from .builder import build_report_context, generate_report, render_report_html

__all__ = ["build_report_context", "render_report_html", "generate_report"]
