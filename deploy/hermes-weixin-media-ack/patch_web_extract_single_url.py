#!/usr/bin/env python3
"""Require exactly one URL per native web_extract call."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys


EXPECTED_SOURCE_SHA256 = "427dab1a75634389b522849d5deb8518029594d95b07de2b2c1d4e3d66bc6c18"

OLD_GUARD_BLOCK = '''    Raises:
        Exception: If extraction fails or API key is not set
    """
    # Block URLs containing embedded secrets (exfiltration prevention).
'''

NEW_GUARD_BLOCK = '''    Raises:
        Exception: If extraction fails or API key is not set
    """
    if not isinstance(urls, list) or len(urls) != 1:
        return json.dumps({
            "success": False,
            "error": (
                "web_extract accepts exactly one URL per call in this deployment. "
                "Retry with one URL, then call web_extract again for the next source."
            ),
        })

    # Block URLs containing embedded secrets (exfiltration prevention).
'''

OLD_SCHEMA_BLOCK = '''            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            },
'''

NEW_SCHEMA_BLOCK = '''            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exactly one public URL to extract. Call web_extract again for each additional source.",
                "minItems": 1,
                "maxItems": 1
            },
'''


def patch_text(source: str) -> str:
    for label, block in (
        ("runtime guard", OLD_GUARD_BLOCK),
        ("tool schema", OLD_SCHEMA_BLOCK),
    ):
        occurrences = source.count(block)
        if occurrences != 1:
            raise RuntimeError(f"expected one {label} block, found {occurrences}")
    return source.replace(OLD_GUARD_BLOCK, NEW_GUARD_BLOCK, 1).replace(
        OLD_SCHEMA_BLOCK, NEW_SCHEMA_BLOCK, 1
    )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_web_extract_single_url.py PATH_TO_WEB_TOOLS_PY")

    source_path = Path(sys.argv[1])
    source_bytes = source_path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    if source_hash != EXPECTED_SOURCE_SHA256:
        raise RuntimeError(
            f"refusing to patch unexpected source: {source_hash} != {EXPECTED_SOURCE_SHA256}"
        )

    patched = patch_text(source_bytes.decode("utf-8"))
    compile(patched, str(source_path), "exec")

    temporary_path = source_path.with_suffix(source_path.suffix + ".tmp")
    temporary_path.write_text(patched, encoding="utf-8")
    os.chmod(temporary_path, source_path.stat().st_mode)
    os.replace(temporary_path, source_path)
    print(f"patched {source_path} sha256={hashlib.sha256(patched.encode()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
