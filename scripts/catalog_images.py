#!/usr/bin/env python3
"""The images the catalog deploys, as a GitHub Actions matrix for the weekly re-scan (rescan-images.yml).

    catalog_images.py apps/*.yaml

For every released pods entry, the digest-pinned images of appstore.yaml on the newest vX.Y.Z tag (the version an
admin approves and the AppStore deploys) and on the default branch (the next release). The build pipeline scans an
image once, when it is built; a CVE published later stays unnoticed in an approved version until someone rebuilds.

Prints {"include": [{"app", "ref", "image", "trivyignore"}, ...]}: one row per image (the first ref that pins it),
``trivyignore`` the raw URL of that ref's .trivyignore ("" if the repo has none), so the scan applies the app's own,
reviewed exceptions. Entries that cannot be read are reported on stderr and skipped (validate-registry fails on them).
Uses GITHUB_TOKEN if set (rate limit).  Dependencies: PyYAML, jsonschema (via check_entry).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_entry import SPEC_FILE, gh, latest_tag  # noqa: E402

DIGEST_IMAGE = re.compile(r"^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")


def images_of(spec_text: str) -> list[str]:
    """The digest-pinned ghcr.io images of an appstore.yaml (anything else is the validator's business)."""
    try:
        containers = yaml.safe_load(spec_text)["workload"]["containers"]
    except (yaml.YAMLError, KeyError, TypeError):
        return []
    found = (c.get("image") for c in containers if isinstance(c, dict))
    return sorted({i for i in found if isinstance(i, str) and DIGEST_IMAGE.match(i)})


def rows(path: Path) -> list[dict[str, str]]:
    entry = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(entry, dict) or entry.get("runtime") != "pods" or entry.get("status", "released") != "released":
        return []
    owner_repo = re.sub(r"^https://github\.com/", "", entry["repo"])
    code, repo = gh(f"repos/{owner_repo}")
    if code != 200:
        print(f"{path.name}: repo not reachable ({code}), skipped", file=sys.stderr)
        return []
    refs = [r for r in (latest_tag(owner_repo), repo["default_branch"]) if r]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref in refs:
        quoted = urllib.parse.quote(ref)
        code, text = gh(f"repos/{owner_repo}/contents/{SPEC_FILE}?ref={quoted}", raw=True)
        if code != 200:
            print(f"{path.name}: no {SPEC_FILE} on {ref} ({code}), skipped", file=sys.stderr)
            continue
        ignore_code, _ = gh(f"repos/{owner_repo}/contents/.trivyignore?ref={quoted}")
        ignore = f"https://raw.githubusercontent.com/{owner_repo}/{quoted}/.trivyignore" if ignore_code == 200 else ""
        for image in images_of(text):
            if image not in seen:
                seen.add(image)
                out.append({"app": entry["name"], "ref": ref, "image": image, "trivyignore": ignore})
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    include = [row for name in argv for row in rows(Path(name))]
    print(json.dumps({"include": include}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
