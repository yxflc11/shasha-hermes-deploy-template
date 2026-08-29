#!/usr/bin/env python3
"""Record a user-observed delivery after an ambiguous successful iLink response."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile


MANIFEST_ROOT = Path("/opt/data/act-ai-brief/delivery-manifests")


def _parse_utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RuntimeError("sent-at must be an explicit UTC timestamp")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def confirm(
    manifest: dict,
    *,
    brief_id: str,
    part_id: str,
    sent_at: str,
    evidence: str,
) -> dict:
    if manifest.get("brief_id") != brief_id:
        raise RuntimeError("brief_id does not match manifest")
    if manifest.get("delivered_complete") or manifest.get("completed_at"):
        raise RuntimeError("refusing to alter a completed manifest")

    matches = [part for part in manifest.get("parts", []) if part.get("id") == part_id]
    if len(matches) != 1 or matches[0].get("kind") != "image":
        raise RuntimeError("part must identify exactly one image")
    part = matches[0]
    if part.get("sent_at"):
        raise RuntimeError("image part is already marked delivered")

    normalized_sent_at = _parse_utc(sent_at)
    part["sent_at"] = normalized_sent_at
    manifest.setdefault("observed_delivery_history", []).append(
        {
            "confirmed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "evidence": evidence,
            "part_id": part_id,
            "sent_at": normalized_sent_at,
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
    parser.add_argument("--part-id", required=True)
    parser.add_argument("--sent-at", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()

    path = Path(args.manifest).resolve()
    root = MANIFEST_ROOT.resolve()
    if path.name != "manifest.json" or root not in path.parents:
        raise RuntimeError("manifest path is outside the delivery-manifests root")

    payload = json.loads(path.read_text(encoding="utf-8"))
    atomic_write(
        path,
        confirm(
            payload,
            brief_id=args.brief_id,
            part_id=args.part_id,
            sent_at=args.sent_at,
            evidence=args.evidence,
        ),
    )
    print(f"recorded user-observed delivery for {args.brief_id}/{args.part_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
