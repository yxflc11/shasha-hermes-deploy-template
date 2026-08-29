#!/usr/bin/env python3
"""Invalidate false-positive image receipts in one incomplete brief manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile


MANIFEST_ROOT = Path("/opt/data/act-ai-brief/delivery-manifests")


def invalidate(manifest: dict, *, brief_id: str, reason: str) -> dict:
    if manifest.get("brief_id") != brief_id:
        raise RuntimeError("brief_id does not match manifest")
    if manifest.get("delivered_complete") or manifest.get("completed_at"):
        raise RuntimeError("refusing to alter a completed manifest")

    invalidated: list[dict[str, str]] = []
    for part in manifest.get("parts", []):
        if part.get("kind") != "image" or not part.get("sent_at"):
            continue
        invalidated.append({"id": str(part.get("id")), "sent_at": str(part["sent_at"])})
        part["sent_at"] = None

    if not invalidated:
        raise RuntimeError("manifest has no image receipts to invalidate")

    manifest.setdefault("repair_history", []).append(
        {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "invalidated": invalidated,
            "reason": reason,
        }
    )
    return manifest


def atomic_write(path: Path, payload: dict) -> None:
    original_mode = stat.S_IMODE(path.stat().st_mode)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".manifest.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, original_mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--brief-id", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()

    path = Path(args.manifest).resolve()
    root = MANIFEST_ROOT.resolve()
    if path.name != "manifest.json" or root not in path.parents:
        raise RuntimeError("manifest path is outside the delivery-manifests root")

    payload = json.loads(path.read_text(encoding="utf-8"))
    atomic_write(path, invalidate(payload, brief_id=args.brief_id, reason=args.reason))
    print(f"invalidated false media acknowledgements in {args.brief_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
