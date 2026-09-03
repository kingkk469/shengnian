"""Frozen app entry: dispatch multiprocessing before importing Qt or models."""
import multiprocessing
multiprocessing.freeze_support()

import os
from pathlib import Path
import sys
import traceback

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

try:
    if sys.argv[1:3] == ["--role", "mac-self-test"]:
        from mac_frozen_check import main
        main(sys.argv[3:])
    else:
        from launcher import main
        main()
except Exception:
    from platform_support import default_data_root
    log = default_data_root() / "logs" / "mac-startup-error.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(traceback.format_exc(), encoding="utf-8")
    raise
