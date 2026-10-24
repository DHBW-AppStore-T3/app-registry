#!/usr/bin/env python3
"""Check registry entries: schema, file name, repo public + reachable, contract files on the default branch.

    check_entry.py apps/foo.yaml [apps/bar.yaml ...] [--offline]

Always checked: schema, file name == name, repo not used by another entry.
Unless --offline:
  * the repo exists, is public and not archived,
  * the repo has a version tag; the checks below run on the default branch AND on the newest
    vX.Y.Z tag (the backend reads the contract at the commit of the submitted tag),
  * runtime pods: appstore.yaml is there, passes the app contract (digest-pinned,
    validate_appstore_yaml.py), its name is the entry name and every image can be pulled
    anonymously from ghcr.io (a private package would fail on the cluster),
  * runtime vm: terraform/ and packer/ exist and appstore.yaml does NOT (the backend runs
    a version as pods as soon as the file is there).
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
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_appstore_yaml import validate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "app-entry.schema.json").read_text(encoding="utf-8"))
SPEC_FILE = "appstore.yaml"
MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


def fetch(url: str, headers: dict[str, str] | None = None, method: str = "GET") -> tuple[int, bytes]:
    """(HTTP status, body). Status 0 = the request itself failed (DNS, timeout, ...); the body is the reason."""
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e).encode()


def gh(path: str, raw: bool = False) -> tuple[int, object]:
    """GET from the GitHub API. The token only lifts the rate limit; orgs with an IP allow list (aquasecurity)
    refuse it from runner IPs although their public data is open, so a 403 is retried once without it."""
    headers = {"Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"}
    url = f"https://api.github.com/{path}"
    token = os.environ.get("GITHUB_TOKEN")
    code, body = fetch(url, {**headers, "Authorization": f"Bearer {token}"} if token else headers)
    if token and code == 403:
        anon_code, anon_body = fetch(url, headers)
        if anon_code == 200:
            code, body = anon_code, anon_body
    if code != 200:
        return code, api_message(body)
    return code, body.decode("utf-8") if raw else json.loads(body)


def api_message(body: bytes) -> str | None:
    """The ``message`` of a GitHub API error body, or the raw text (shortened)."""
    text = body.decode(errors="replace").strip()
    try:
        return str(json.loads(text).get("message") or "")[:200] or None
    except (ValueError, AttributeError):
        return text[:200] or None


def http_problem(code: int, detail: object) -> str:
    if code == 0:
        return f"request failed ({detail})"
    extra = f": {detail}" if detail else ""
    if code in (403, 429):
        return f"HTTP {code}{extra} (rate limit or no access; set GITHUB_TOKEN)"
    return f"HTTP {code}{extra}"


def image_pullable(image: str) -> str | None:
    """None if ``ghcr.io/<repo>@sha256:...`` can be pulled without credentials, else the reason."""
    ref, _, digest = image.partition("@")
    repo = ref.removeprefix("ghcr.io/")
    code, body = fetch(f"https://ghcr.io/token?service=ghcr.io&scope={urllib.parse.quote(f'repository:{repo}:pull')}")
    token = json.loads(body).get("token") if code == 200 else None
    if not token:
        return f"no anonymous token for {repo} ({http_problem(code, api_message(body))})"
    code, detail = fetch(f"https://ghcr.io/v2/{repo}/manifests/{digest}",
                         {"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT}, method="HEAD")
    if code == 200:
        return None
    if code in (401, 403):
        return "not pullable without credentials: set the package to Public (Package settings -> Change visibility)"
    if code == 404:
        return "digest not found in the registry"
    return http_problem(code, api_message(detail))


def latest_tag(owner_repo: str) -> str | None:
    """The highest ``vX.Y.Z`` tag of the repo (None without one or if the lookup fails)."""
    code, tags = gh(f"repos/{owner_repo}/tags?per_page=100")
    if code != 200:
        return None
    found = []
    for t in tags:
        m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", t["name"])
        if m:
            found.append((tuple(map(int, m.groups())), t["name"]))
    return max(found)[1] if found else None


def check_spec(entry: dict, text: str) -> list[str]:
    """Problems of the repo's appstore.yaml (strict: the version submitted to the AppStore needs digests)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"{SPEC_FILE}: invalid YAML: {exc}"]
    result = validate(data, allow_tags=False)
    errs = [f"{SPEC_FILE}: {loc}: {msg}" for loc, msg in result.items]
    if errs or not isinstance(data, dict):
        return errs
    if data.get("name") != entry["name"]:
        errs.append(f"{SPEC_FILE}: name {data.get('name')!r} must equal the registry name {entry['name']!r}")
    if data.get("runtime") != "kubernetes":
        errs.append(f"{SPEC_FILE}: runtime is {data.get('runtime')!r}, registry runtime 'pods' needs 'kubernetes'")
    return errs


def duplicate_repos(path: Path, entry: dict) -> list[str]:
    """Other entries in the same apps/ directory that point at the same repo."""
    mine = entry["repo"].lower()
    errs = []
    for other in sorted(path.resolve().parent.glob("*.yaml")):
        if other.resolve() == path.resolve():
            continue
        try:
            o = yaml.safe_load(other.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(o, dict) and str(o.get("repo", "")).lower() == mine:
            errs.append(f"repo {entry['repo']} is already registered by {other.name}")
    return errs


def check(path: Path, offline: bool) -> list[str]:
    errs: list[str] = []
    try:
        entry = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [f"cannot read/parse: {exc}"]
    errs += [f"schema: {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
             for e in Draft202012Validator(SCHEMA).iter_errors(entry)]
    if errs or not isinstance(entry, dict):
        return errs
    if path.stem != entry["name"]:
        errs.append(f"file name {path.name} must be {entry['name']}.yaml")
    errs += duplicate_repos(path, entry)
    if offline:
        return errs
    owner_repo = re.sub(r"^https://github\.com/", "", entry["repo"])
    code, repo = gh(f"repos/{owner_repo}")
    if code != 200:
        return errs + [f"repo {entry['repo']} not reachable ({http_problem(code, repo)})"]
    if repo.get("private") or repo.get("visibility") not in (None, "public"):
        errs.append("repo must be PUBLIC (AppStore backend requirement)")
    if repo.get("archived"):
        errs.append("repo is archived")
    branch = repo["default_branch"]
    tag = latest_tag(owner_repo)
    if tag is None:
        errs.append("no version tag (vX.Y.Z): the AppStore registers versions from git tags")
    # The backend reads appstore.yaml at the commit of the submitted tag, so the newest tag counts as much
    # as the default branch (which is what the next release will be cut from).
    refs = [(f"default branch '{branch}'", branch)] + ([(f"tag {tag}", tag)] if tag else [])

    def lookup(ref: str, p: str) -> bool | None:
        code, detail = gh(f"repos/{owner_repo}/contents/{p}?ref={urllib.parse.quote(ref)}")
        if code in (200, 404):
            return code == 200
        errs.append(f"cannot look up {p} on '{ref}': {http_problem(code, detail)}")
        return None

    specs: dict[str, str] = {}  # spec text -> where it was found first
    for label, ref in refs:
        has_spec = lookup(ref, SPEC_FILE)
        if entry["runtime"] == "pods":
            if has_spec is False:
                errs.append(f"{SPEC_FILE} missing on {label}")
            elif has_spec:
                code, text = gh(f"repos/{owner_repo}/contents/{SPEC_FILE}?ref={urllib.parse.quote(ref)}", raw=True)
                if code != 200:
                    errs.append(f"cannot read {SPEC_FILE} on {label}: {http_problem(code, text)}")
                elif text not in specs:
                    specs[text] = label
                    errs += [f"{label}: {e}" for e in check_spec(entry, text)]
        else:
            if has_spec:
                errs.append(f"{SPEC_FILE} exists on {label}: the backend would run this app as pods, not as a VM")
            for d in ("terraform", "packer"):
                if lookup(ref, d) is False:
                    errs.append(f"directory {d}/ missing on {label} (runtime vm)")
    images = {}
    for text, label in specs.items():
        try:
            for c in yaml.safe_load(text)["workload"]["containers"]:
                images.setdefault(c["image"], label)
        except (yaml.YAMLError, KeyError, TypeError):
            pass  # already reported by check_spec
    for image, label in sorted(images.items()):
        if (problem := image_pullable(image)) is not None:
            errs.append(f"{label}: image {image}: {problem}")
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
