#!/usr/bin/env python3
"""Install the audited ACT shark persona as a Weixin-only platform hint."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from hermes_cli.config import load_config, save_config


def _read_prompt(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("persona prompt must be a regular file")
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt or "\x00" in prompt or len(prompt) > 8_000:
        raise RuntimeError("persona prompt is empty or invalid")
    return prompt


def desired_state(prompt: str) -> tuple[dict, str]:
    config = load_config()
    agent = config.setdefault("agent", {})
    hints = agent.setdefault("platform_hints", {})
    current = hints.get("weixin")
    if current is not None:
        current_append = current.get("append") if isinstance(current, dict) else current
        if current_append != prompt:
            raise RuntimeError(
                "weixin platform hint already contains different content; "
                "restore or merge it explicitly instead of overwriting"
            )
    hints["weixin"] = {"append": prompt}
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return config, digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    prompt = _read_prompt(args.prompt_file)
    config, digest = desired_state(prompt)
    if not args.check:
        save_config(config)
    print(
        f"mode={'check' if args.check else 'applied'} platform=weixin "
        f"prompt_sha256={digest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
