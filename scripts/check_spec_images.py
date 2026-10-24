#!/usr/bin/env python3
"""Check that the images an appstore.yaml pins by digest can be pulled from ghcr.io without credentials.

    check_spec_images.py appstore.yaml

The cluster pulls anonymously, so a package that GitHub created as private (the default) breaks every deployment
although the build is green. Images without ``@sha256:`` are skipped (the contract validator rejects them).
Dependencies: PyYAML, jsonschema (via check_entry).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_entry import image_pullable  # noqa: E402


def images(spec: object) -> list[str]:
    workload = spec.get("workload") if isinstance(spec, dict) else None
    containers = workload.get("containers") if isinstance(workload, dict) else None
    found = {c["image"] for c in containers or [] if isinstance(c, dict) and isinstance(c.get("image"), str)}
    return sorted(i for i in found if i.startswith("ghcr.io/") and "@sha256:" in i)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    try:
        spec = yaml.safe_load(Path(argv[0]).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"::error file={argv[0]}::cannot read/parse: {exc}")
        return 2
    failed = 0
    for image in images(spec):
        problem = image_pullable(image)
        if problem:
            failed += 1
            print(f"::error file={argv[0]}::image {image}: {problem}")
        else:
            print(f"{image}: pullable without credentials")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
