"""Docker HEALTHCHECK probe.

Reads the heartbeat file's mtime. The daemon touches
/data/state/heartbeat on every state.save() during a pass AND every
60s during the long inter-pass sleep. Threshold = 25h (INTERVAL_HOURS=24
default + 1h buffer). Missing file is treated as healthy during the
container's start_period (bootstrap before any save() has happened).

Exit code:
  0   healthy (or bootstrap window)
  1   unhealthy (heartbeat is stale → daemon is parked or wedged)
"""

import os
import sys
import time

HB = "/data/state/heartbeat"
MAX_AGE_S = 90000  # 25h

if not os.path.exists(HB):
    sys.exit(0)

age = time.time() - os.path.getmtime(HB)
sys.exit(0 if age < MAX_AGE_S else 1)
