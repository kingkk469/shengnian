from __future__ import annotations

import os
import tempfile
from pathlib import Path


os.environ["VOICE_JOURNAL_DATA_ROOT"] = str(
    Path(tempfile.gettempdir()) / "shengnian-open-source-tests"
)
