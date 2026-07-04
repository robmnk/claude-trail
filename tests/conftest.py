import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_trail as feed
import pytest


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Clear every module-level cache global before each test so cache state
    never leaks between tests.

    Any new cache global added to claude_trail must also be reset here.
    """
    feed._session_name_cache.clear()
    feed._session_name_cache_ts = 0.0
    feed._session_color_cache.clear()
    feed._session_color_cache_ts = 0.0
    feed._transcript_path_cache.clear()
    feed._transcript_positions.clear()
