#!/usr/bin/env python3
"""Check registry entries: schema, file name, repo public + reachable, contract files on the default branch.

    check_entry.py apps/foo.yaml [apps/bar.yaml ...] [--offline]

--offline skips the GitHub checks (schema + file name only).
Uses GITHUB_TOKEN if set (only for the rate limit); public visibility is checked
explicitly via the repo metadata.
Dependencies: PyYAML, jsonschema.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "app-entry.schema.json").read_text())


def gh(path: str):
    req = urllib.request.Request(f"https://api.github.com/{path}", headers={"Accept": "application/vnd.github+json"})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def check(path: Path, offline: bool) -> list[str]:
    errs: list[str] = []
    try:
        entry = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"invalid YAML: {exc}"]
    errs += [f"schema: {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
             for e in Draft202012Validator(SCHEMA).iter_errors(entry)]
    if errs or not isinstance(entry, dict):
        return errs
    if path.stem != entry["name"]:
        errs.append(f"file name {path.name} must be {entry['name']}.yaml")
    if offline:
        return errs
    owner_repo = re.sub(r"^https://github\.com/", "", entry["repo"])
    code, repo = gh(f"repos/{owner_repo}")
    if code != 200:
        return errs + [f"repo {entry['repo']} not reachable (HTTP {code})"]
    if repo.get("private") or repo.get("visibility") not in (None, "public"):
        errs.append("repo must be PUBLIC (AppStore backend requirement)")
    if repo.get("archived"):
        errs.append("repo is archived")
    branch = repo["default_branch"]

    def exists(p: str) -> bool:
        return gh(f"repos/{owner_repo}/contents/{p}?ref={branch}")[0] == 200

    if entry["runtime"] == "pods":
        if not exists("appstore.yaml"):
            errs.append(f"appstore.yaml missing on default branch '{branch}'")
    else:
        for d in ("terraform", "packer"):
            if not exists(d):
                errs.append(f"directory {d}/ missing on default branch '{branch}' (runtime vm)")
    return errs


def main(argv: list[str]) -> int:
    offline = "--offline" in argv
    files = [Path(a) for a in argv if not a.startswith("--")]
    if not files:
        print("no entries to check")
        return 0
    failed = 0
    for f in files:
        errs = check(f, offline)
        for m in errs:
            print(f"::error file={f}::{m}")
        print(f"{f}: {'FAILED' if errs else 'ok'}")
        failed += bool(errs)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
