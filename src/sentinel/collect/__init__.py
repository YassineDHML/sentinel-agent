"""Collection layer: one module per source, each returning normalized article dicts.

Sources: rss, hackernews (Algolia), producthunt (GraphQL), googlenews (RSS),
gnews (discovery), plus fulltext (main-text extraction for filtered articles).

Every ``collect_*`` entrypoint fails gracefully (logs + returns []), so one dead
source never breaks a run.

Import collectors from their own modules, e.g.::

    from sentinel.collect.rss import collect_rss
    from sentinel.collect.hackernews import collect_hackernews

(The submodules are intentionally not re-exported here so that
``python -m sentinel.collect.<source>`` runs without a double-import warning.)
"""

from .base import ARTICLE_KEYS, make_article

__all__ = ["ARTICLE_KEYS", "make_article"]
