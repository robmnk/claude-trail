import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Isolate the suite from the developer's real ~/.claude/.env BEFORE claude_trail
# is imported. The module resolves its column widths at import time from
# $CLAUDE_TRAIL_ENV (else CONFIG_DIR/.env) layered under any real
# CLAUDE_TRAIL_COL_* / CLAUDE_TRAIL_PROMPT_COL_* process env vars, so a developer
# who has adopted this feature with non-default widths would otherwise see them
# bleed into layout-sensitive rendering tests. Point CLAUDE_TRAIL_ENV at a
# guaranteed-nonexistent path and drop every inherited width override so the
# module always starts from the built-in defaults regardless of the machine
# running the tests. The reload-based wiring tests (TestColumnWidthWiring,
# TestPromptColumnWidthWiring) set their own CLAUDE_TRAIL_ENV via patch.dict +
# importlib.reload, which overrides this default for their duration.
#
# The filter is "any CLAUDE_TRAIL_* width key" rather than one hard-coded prefix:
# the Prompts tab added six CLAUDE_TRAIL_PROMPT_COL_* keys that a
# CLAUDE_TRAIL_COL_-only strip missed, and a developer with one exported failed
# every geometry test (they count body rows by matching the 8-char session label,
# which a narrower Session column ellipsizes). Matching on the `_WIDTH` suffix
# covers a key a later tab adds too, and leaves the non-geometry knobs
# (CLAUDE_TRAIL_ENV set just above, CLAUDE_TRAIL_DB, which the store tests
# monkeypatch) alone.
os.environ["CLAUDE_TRAIL_ENV"] = str(
    Path(tempfile.gettempdir()) / "claude-trail-no-such-dir" / ".env"
)
for _leaked_key in [
    k for k in os.environ
    if k.startswith("CLAUDE_TRAIL_") and k.endswith("_WIDTH")
]:
    del os.environ[_leaked_key]

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
    feed._agent_label_cache.clear()
    feed._agent_label_cache_ts = 0.0
    feed._session_model_cache.clear()
