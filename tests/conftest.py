import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Isolate the suite from the developer's real ~/.claude/.env BEFORE claude_trail
# is imported. The module resolves its column widths at import time from
# $CLAUDE_TRAIL_ENV (else CONFIG_DIR/.env) layered under any real
# CLAUDE_TRAIL_COL_* process env vars, so a developer who has adopted this
# feature with non-default widths would otherwise see them bleed into
# layout-sensitive rendering tests. Point CLAUDE_TRAIL_ENV at a
# guaranteed-nonexistent path and drop any inherited CLAUDE_TRAIL_COL_*
# overrides so the module always starts from the built-in defaults regardless
# of the machine running the tests. The reload-based wiring test
# (TestColumnWidthWiring) sets its own CLAUDE_TRAIL_ENV via patch.dict +
# importlib.reload, which overrides this default for its duration.
os.environ["CLAUDE_TRAIL_ENV"] = str(
    Path(tempfile.gettempdir()) / "claude-trail-no-such-dir" / ".env"
)
for _leaked_key in [k for k in os.environ if k.startswith("CLAUDE_TRAIL_COL_")]:
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
