#!/usr/bin/env python3

import os
from datetime import datetime, timezone

from act_ai_brief import latest_due_slot, main


if __name__ == "__main__":
    os.environ.setdefault("ACT_BRIEF_VISUAL_DELIVERY", "1")
    os.environ.setdefault("ACT_BRIEF_RENDER_MODE", "queue")
    raise SystemExit(main(["--slot", latest_due_slot(datetime.now(timezone.utc))]))
