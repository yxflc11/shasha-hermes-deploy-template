#!/usr/bin/env python3

import os

from act_ai_brief import main


if __name__ == "__main__":
    os.environ.setdefault("ACT_BRIEF_VISUAL_DELIVERY", "1")
    os.environ.setdefault("ACT_BRIEF_RENDER_MODE", "queue")
    raise SystemExit(main(["--slot", "evening"]))
