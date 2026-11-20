"""Test-wide setup."""
import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _no_langsmith_tracing():
    """Never send test runs to the owner's LangSmith. app.extractor loads .env with override=True at import,
    which can switch tracing on; import it first, then force tracing off for the whole session."""
    from app import extractor, tracing  # noqa: F401

    os.environ["LANGSMITH_TRACING"] = "false"
    tracing.setup()
    yield
