#!/usr/bin/env python3
"""Check that every ``uses: owner/repo@ref`` in the workflows and templates resolves.

    check_action_refs.py [file ...]      (default: .github/workflows/*.yml and templates/*.yml)

actionlint checks the syntax, not whether the action version exists: a typo such as
``aquasecurity/trivy-action@0.28.0`` (the tag is ``v0.28.0``) only fails when the workflow
runs. Local actions (``./``), docker:// references and this repo's own reusable workflow are skipped.
Uses GITHUB_TOKEN if set (rate limit).  Dependencies: PyYAML (via check_entry: jsonschema).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_entry import gh, http_problem  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^'\"\s#]+)", re.M)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OWN = "dhbw-appstore-t3/app-registry/"


def refs(text: str) -> list[str]:
    return sorted(set(USES_RE.findall(text)))


def resolves(owner_repo: str, ref: str) -> str | None:
    """None if the ref exists, else why not."""
    code, detail = gh(f"repos/{owner_repo}/commits/{ref}")
    if code == 200:
        return None
    if code in (404, 422):
        return "ref does not exist"
    return http_problem(code, detail)


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or sorted([*(ROOT / ".github" / "workflows").glob("*.yml"),
                                              *(ROOT / "templates").glob("*.yml")])
    failed = 0
    for f in files:
        for use in refs(f.read_text(encoding="utf-8")):
            if use.startswith(("./", "docker://")) or use.lower().startswith(OWN) or "@" not in use:
                continue
            action, ref = use.rsplit("@", 1)
            owner_repo = "/".join(action.split("/")[:2])
            problem = resolves(owner_repo, ref)
            if problem:
                failed += 1
                print(f"::error file={f}::{use}: {problem}")
            else:
                kind = "commit" if SHA_RE.match(ref) else "ref"
                print(f"{f.name}: {use} ({kind}) ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
