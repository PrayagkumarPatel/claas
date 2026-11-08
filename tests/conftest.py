"""Pytest config: make repo-root imports work + keep tests hermetic."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Tests must never see real credentials or bootstrap modes."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("CLAAS_DB_BOOTSTRAP", raising=False)
