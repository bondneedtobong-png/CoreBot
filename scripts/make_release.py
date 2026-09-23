#!/usr/bin/env python3
"""Generate the release manifest (RELEASE.json) for a CoreBot code tree.

Reads product version from VERSION, HEAD SHA from git (or --sha), computes
the stdlib code checksum via scripts/release_lib.py. Used by the update flow
after staging and inspectable via GET /version on the Control Plane.

Exit codes: 0 = ok, 2 = error. Stdlib only, never reads .env (no secrets).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_lib import MANIFEST_FILENAME, build_manifest, verify_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate CoreBot RELEASE.json manifest."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="code tree to describe (default: repo root)",
    )
    parser.add_argument(
        "--output", default=None, help="manifest path (default: <root>/RELEASE.json)"
    )
    parser.add_argument(
        "--sha", default=None, help="explicit full git SHA (default: git HEAD)"
    )
    parser.add_argument(
        "--print", dest="do_print", action="store_true", help="print manifest JSON"
    )
    args = parser.parse_args(argv)

    manifest = build_manifest(args.root, sha=args.sha)
    errors = verify_manifest(
        manifest, args.root if manifest["sha"] != "unknown" else None
    )
    if errors:
        for err in errors:
            print(f"make_release: error: {err}", file=sys.stderr)
        return 2
    out = Path(args.output) if args.output else Path(args.root) / MANIFEST_FILENAME
    out.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {out}")
    if args.do_print:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
