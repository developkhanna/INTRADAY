"""Keep the suite off the owner's real ~/.intraday folder.

`intraday.config` resolves its paths at import time, so the override has to be
in place before anything imports it — a conftest at collection time is the
earliest hook that guarantees that.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "INTRADAY_DATA_DIR", tempfile.mkdtemp(prefix="intraday-tests-")
)
