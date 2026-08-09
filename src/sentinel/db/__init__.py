"""Persistence layer: Supabase client + repositories (articles, trends, reports).

The ``supabase`` SDK is imported lazily (in :func:`~sentinel.db.client.create_supabase_client`),
so importing this package requires neither the SDK nor any credentials.
"""

from .client import SupabaseDB, create_supabase_client
from .errors import is_unique_violation, retry_unless_unique
from .repositories import (
    ArticleRepository,
    ReportRepository,
    ReportSourceRepository,
    TrendRepository,
)

__all__ = [
    "SupabaseDB",
    "create_supabase_client",
    "ArticleRepository",
    "TrendRepository",
    "ReportRepository",
    "ReportSourceRepository",
    "is_unique_violation",
    "retry_unless_unique",
]
