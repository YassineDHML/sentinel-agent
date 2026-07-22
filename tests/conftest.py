"""Shared test fixtures.

The autouse fixture below guarantees the suite is **hermetic**: every test runs
with all secret environment variables removed, so a test can never accidentally
depend on a real credential (from the shell or a local .env). Tests that need a
secret set it explicitly via ``monkeypatch.setenv`` — which runs after this
fixture, so it still works.
"""

from __future__ import annotations

import pytest

from sentinel.config import SECRET_NAMES


@pytest.fixture(autouse=True)
def _strip_secrets(monkeypatch):
    for name in SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
