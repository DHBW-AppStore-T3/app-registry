#!/usr/bin/env python3
"""Validate an ``appstore.yaml`` against app contract v2.

Mirrors the parser in DHBW-AppStore-T3/backend (``app/k8s/spec.py``, branch dev):
unknown fields are rejected, images must be pinned by digest and come from the
allowed registry, resources must stay within the platform limits.

Only dependency: PyYAML.  Exit code 0 = valid, 1 = invalid, 2 = usage error.

    validate_appstore_yaml.py appstore.yaml [--allow-tags] [--json]

--allow-tags  accept ``registry/repo:tag`` images (warning only). Used by the
              CI build, because the digest does not exist before the first build.
              The AppStore itself always requires ``@sha256:<digest>``.
"""
from __future__ import annotations

import argparse
import json
import re
import string
import sys

import yaml

REGISTRY_PREFIX = "ghcr.io/dhbw-appstore-t3/"
MAX_CPU_M, MAX_MEM_MI, MAX_STORAGE_MI, MAX_CONTAINERS = 4000, 8192, 20 * 1024, 3
MAX_FILE_VARIABLE_BYTES = 1024 * 1024

IMAGE_DIGEST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$")
IMAGE_TAG_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9._-]+)+:[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CPU_RE = re.compile(r"^(\d+)m$|^(\d+(\.\d+)?)$")
MEM_RE = re.compile(r"^(\d+)(Mi|Gi)$")
SIZE_RE = re.compile(r"^(\d+)(Ki|Mi|Gi)$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
URL_PLACEHOLDERS = {"workload", "deployment", "zone", "domain"}
USER_PLACEHOLDERS = {"user", "team"}


class Errors:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []

    def add(self, loc: str, msg: str) -> None:
        self.items.append((loc, msg))

    def warn(self, loc: str, msg: str) -> None:
        self.warnings.append((loc, msg))


def cpu_m(v: object) -> int | None:
    m = CPU_RE.match(v) if isinstance(v, str) else None
    if not m:
        return None
    return int(m.group(1)) if m.group(1) is not None else round(float(m.group(2)) * 1000)


def mem_mi(v: object) -> int | None:
    m = MEM_RE.match(v) if isinstance(v, str) else None
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1) if m else None


def check_path(e: Errors, loc: str, p: object, ingress: bool = False) -> None:
    if not isinstance(p, str) or not p.startswith("/") or len(p) > 255:
        return e.add(loc, "must be an absolute path of at most 255 characters")
    if p == "/":
        if not ingress:
            e.add(loc, "must not be '/'")
        return
    segs = p[1:].split("/")
    if ingress and segs[-1] == "":
        segs = segs[:-1]
    for s in segs:
        if s in (".", "..") or not SEGMENT_RE.match(s):
            return e.add(loc, f"{p!r} may only contain plain segments (letters, digits, '.', '_', '-')")


def only_keys(e: Errors, loc: str, obj: object, allowed: set[str], required: set[str] = frozenset()) -> bool:
    if not isinstance(obj, dict):
        e.add(loc or "<root>", "must be a mapping")
        return False
    for k in obj:
        if k not in allowed:
            e.add(f"{loc}.{k}" if loc else str(k), "unknown field (extra fields are forbidden)")
    for k in required:
        if k not in obj:
            e.add(f"{loc}.{k}" if loc else k, "field required")
    return True


def placeholders(e: Errors, loc: str, tpl: str, allowed: set[str]) -> None:
    try:
        for _, name, _, _ in string.Formatter().parse(tpl):
            if name is not None and name not in allowed:
                e.add(loc, f"unknown placeholder {{{name}}} (allowed: {', '.join(sorted(allowed))})")
    except ValueError as exc:
        e.add(loc, f"invalid template: {exc}")


def check_resource_bounds(e: Errors, loc: str, c: dict) -> None:
    """Optional ``resourceBounds``: the range people may choose cpu/memory from; resources are its defaults."""
    rb = c.get("resourceBounds")
    if rb is None:
        return
    here = f"{loc}.resourceBounds"
    if not only_keys(e, here, rb, {"cpu", "memory"}, {"cpu", "memory"}):
        return
    res = c.get("resources") if isinstance(c.get("resources"), dict) else {}
    for key, conv, limit, unit in (("cpu", cpu_m, MAX_CPU_M, "m"), ("memory", mem_mi, MAX_MEM_MI, "Mi")):
        rng = rb.get(key)
        if not only_keys(e, f"{here}.{key}", rng, {"min", "max"}, {"min", "max"}):
            continue
        lo, hi = conv(rng.get("min")), conv(rng.get("max"))
        if lo is None or hi is None:
            e.add(f"{here}.{key}", "invalid quantity")
            continue
        if lo > hi:
            e.add(f"{here}.{key}", "min must not exceed max")
        if hi > limit:
            e.add(f"{here}.{key}.max", f"exceeds platform limit of {limit}{unit}")
        default = conv(res.get(key))
        if default is not None and not lo <= default <= hi:
            e.add(f"{loc}.resources.{key}", f"the default must lie within resourceBounds.{key}")


def check_container(e: Errors, loc: str, c: object, allow_tags: bool) -> bool:
    if not only_keys(e, loc, c, {"name", "image", "expose", "resources", "resourceBounds", "env", "writablePaths"},
                     {"name", "image", "resources"}):
        return False
    if isinstance(c.get("name"), str) and not NAME_RE.match(c["name"]):
        e.add(f"{loc}.name", f"invalid container name {c['name']!r} (lowercase letters, digits, '-')")
    img = c.get("image")
    if isinstance(img, str):
        if not IMAGE_DIGEST_RE.match(img):
            if allow_tags and IMAGE_TAG_RE.match(img):
                e.warn(f"{loc}.image", "tag instead of digest: allowed in CI only, the AppStore requires @sha256:<digest>")
            else:
                e.add(f"{loc}.image", "image must be pinned by digest: <registry>/<repo>@sha256:<64 hex> (tags are not allowed)")
        if not img.startswith(REGISTRY_PREFIX):
            e.add(f"{loc}.image", f"registry not allowed (allowed prefixes: {REGISTRY_PREFIX})")
    elif "image" in c:
        e.add(f"{loc}.image", "must be a string")
    expose = c.get("expose")
    if expose is not None and only_keys(e, f"{loc}.expose", expose, {"port", "path"}, {"port"}):
        port = expose.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            e.add(f"{loc}.expose.port", "must be an integer between 1024 and 65535")
        check_path(e, f"{loc}.expose.path", expose.get("path", "/"), ingress=True)
    res = c.get("resources")
    if only_keys(e, f"{loc}.resources", res, {"cpu", "memory", "requests"}, {"cpu", "memory"}):
        cpu, mem = cpu_m(res.get("cpu")), mem_mi(res.get("memory"))
        if "cpu" in res and cpu is None:
            e.add(f"{loc}.resources.cpu", "invalid CPU quantity (use e.g. '500m' or '1')")
        elif cpu is not None and cpu > MAX_CPU_M:
            e.add(f"{loc}.resources.cpu", f"exceeds platform limit of {MAX_CPU_M}m")
        if "memory" in res and mem is None:
            e.add(f"{loc}.resources.memory", "invalid memory quantity (use e.g. '512Mi' or '1Gi')")
        elif mem is not None and mem > MAX_MEM_MI:
            e.add(f"{loc}.resources.memory", f"exceeds platform limit of {MAX_MEM_MI}Mi")
        rq = res.get("requests")
        if rq is not None and only_keys(e, f"{loc}.resources.requests", rq, {"cpu", "memory"}, {"cpu", "memory"}):
            rc, rm = cpu_m(rq.get("cpu")), mem_mi(rq.get("memory"))
            if rc is None or rm is None:
                e.add(f"{loc}.resources.requests", "invalid cpu/memory quantity")
            else:
                if cpu is not None and rc > cpu:
                    e.add(f"{loc}.resources.requests.cpu", "must not exceed cpu")
                if mem is not None and rm > mem:
                    e.add(f"{loc}.resources.requests.memory", "must not exceed memory")
    check_resource_bounds(e, loc, c)
    env = c.get("env", [])
    if not isinstance(env, list):
        e.add(f"{loc}.env", "must be a list")
        env = []
    for i, v in enumerate(env):
        l = f"{loc}.env[{i}]"
        if not only_keys(e, l, v, {"name", "from", "value"}, {"name"}):
            continue
        if not isinstance(v["name"], str) or not ENV_RE.match(v["name"]):
            e.add(f"{l}.name", f"invalid environment variable name {v['name']!r}")
        if ("from" in v) == ("value" in v):
            e.add(l, "exactly one of 'from' and 'value' is required")
        if "from" in v and v["from"] not in ("generated-password", "team-name", "user-name"):
            e.add(f"{l}.from", "must be one of generated-password, team-name, user-name")
    wp = c.get("writablePaths", [])
    if not isinstance(wp, list):
        e.add(f"{loc}.writablePaths", "must be a list")
    else:
        for i, p in enumerate(wp):
            check_path(e, f"{loc}.writablePaths[{i}]", p)
        if len(set(map(str, wp))) != len(wp):
            e.add(f"{loc}.writablePaths", "writablePaths must be unique")
    return True


def check_variable(e: Errors, loc: str, v: object) -> None:
    if not only_keys(e, loc, v, {"name", "type", "scope", "ext", "maxSize", "mountPath", "values", "default"},
                     {"name", "type"}):
        return
    if isinstance(v.get("name"), str) and not NAME_RE.match(v["name"].replace("_", "-")):
        e.add(f"{loc}.name", f"invalid variable name {v['name']!r}")
    if v.get("type") not in ("file", "enum", "string"):
        e.add(f"{loc}.type", "must be one of file, enum, string")
    if v.get("scope", "deployment") not in ("deployment", "team"):
        e.add(f"{loc}.scope", "must be deployment or team")
    if v.get("type") == "file":
        if not v.get("mountPath"):
            e.add(f"{loc}.mountPath", "file variables need an absolute mountPath")
        else:
            check_path(e, f"{loc}.mountPath", v["mountPath"])
        ms = v.get("maxSize")
        if ms is not None:
            m = SIZE_RE.match(str(ms))
            if not m:
                e.add(f"{loc}.maxSize", "invalid size (use e.g. '5Mi')")
            elif int(m.group(1)) * {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}[m.group(2)] > MAX_FILE_VARIABLE_BYTES:
                e.add(f"{loc}.maxSize", "maxSize above 1Mi is not supported yet (Secret mount limit)")
    if v.get("type") == "enum":
        if not v.get("values"):
            e.add(f"{loc}.values", "enum variables need 'values'")
        elif v.get("default") is not None and v["default"] not in v["values"]:
            e.add(f"{loc}.default", "default must be one of 'values'")


def check_access(e: Errors, loc: str, a: object) -> None:
    if not only_keys(e, loc, a, {"type", "template", "from", "username"}, {"type"}):
        return
    if a.get("type") == "url":
        t = a.get("template")
        if not t:
            e.add(loc, "url access needs 'template'")
        elif not isinstance(t, str) or not t.startswith("https://"):
            e.add(f"{loc}.template", "url access must start with 'https://'")
        else:
            placeholders(e, f"{loc}.template", t, URL_PLACEHOLDERS)
    elif a.get("type") == "password":
        if a.get("from") != "generated-password":
            e.add(loc, "password access needs 'from: generated-password'")
        if a.get("username"):
            placeholders(e, f"{loc}.username", str(a["username"]), USER_PLACEHOLDERS)
    else:
        e.add(f"{loc}.type", "must be url or password")


def validate(data: object, allow_tags: bool = False) -> Errors:
    e = Errors()
    if not only_keys(e, "", data, {"apiVersion", "name", "runtime", "scope", "workload", "egress", "variables", "access"},
                     {"apiVersion", "name", "runtime"}):
        return e
    if data.get("apiVersion") != "appstore/v2":
        e.add("apiVersion", "must be 'appstore/v2'")
    if isinstance(data.get("name"), str) and not NAME_RE.match(data["name"]):
        e.add("name", f"invalid app name {data['name']!r} (lowercase letters, digits, '-')")
    runtime = data.get("runtime")
    if runtime not in ("kubernetes", "openstack-vm"):
        e.add("runtime", "must be kubernetes or openstack-vm")
    if data.get("scope", "team") not in ("team", "user"):
        e.add("scope", "must be team or user")
    if data.get("egress", "none") not in ("none", "internet"):
        e.add("egress", "must be none or internet")
    wl = data.get("workload")
    if runtime == "kubernetes" and wl is None:
        e.add("workload", "runtime 'kubernetes' requires 'workload'")
    if runtime == "openstack-vm" and wl is not None:
        e.add("workload", "runtime 'openstack-vm' uses terraform/ + packer/, 'workload' is not allowed")
    containers: list = []
    storage = None
    if wl is not None and only_keys(e, "workload", wl, {"containers", "storage"}, {"containers"}):
        containers = wl.get("containers")
        if not isinstance(containers, list) or not containers:
            e.add("workload.containers", "at least one container required")
            containers = []
        else:
            if len(containers) > MAX_CONTAINERS:
                e.add("workload.containers", f"at most {MAX_CONTAINERS} containers")
            ok = [check_container(e, f"workload.containers[{i}]", c, allow_tags) for i, c in enumerate(containers)]
            if all(ok):
                names = [c.get("name") for c in containers]
                if len(set(map(str, names))) != len(names):
                    e.add("workload.containers", "container names must be unique")
                if sum(c.get("expose") is not None for c in containers) != 1:
                    e.add("workload.containers", "exactly one container must set 'expose'")
        storage = wl.get("storage")
        if storage is not None and only_keys(e, "workload.storage", storage, {"size", "mountPath"}, {"size", "mountPath"}):
            mi = mem_mi(storage.get("size"))
            if mi is None:
                e.add("workload.storage.size", "invalid storage size (use e.g. '5Gi')")
            elif mi > MAX_STORAGE_MI:
                e.add("workload.storage.size", f"exceeds platform limit of {MAX_STORAGE_MI}Mi")
            check_path(e, "workload.storage.mountPath", storage.get("mountPath"))
            if any(isinstance(c, dict) and isinstance(c.get("writablePaths"), list)
                   and storage.get("mountPath") in c["writablePaths"] for c in containers):
                e.add("workload", "storage.mountPath must not also be a writablePaths entry")
    variables = data.get("variables", [])
    if not isinstance(variables, list):
        e.add("variables", "must be a list")
        variables = []
    for i, v in enumerate(variables):
        check_variable(e, f"variables[{i}]", v)
    vd = [v for v in variables if isinstance(v, dict)]
    names = [v.get("name") for v in vd]
    if len(set(map(str, names))) != len(names):
        e.add("variables", "variable names must be unique")
    files = [v["mountPath"] for v in vd if v.get("type") == "file" and v.get("mountPath")]
    if len(set(files)) != len(files):
        e.add("variables", "file variables need distinct mountPaths")
    exposed = next((c for c in containers if isinstance(c, dict) and c.get("expose")), None)
    taken = set(exposed.get("writablePaths", []) if exposed and isinstance(exposed.get("writablePaths"), list) else [])
    if isinstance(storage, dict) and storage.get("mountPath"):
        taken.add(storage["mountPath"])
    if taken & set(files):
        e.add("variables", "a file variable's mountPath is already used by writablePaths or storage")
    access = data.get("access", [])
    if not isinstance(access, list):
        e.add("access", "must be a list")
        access = []
    for i, a in enumerate(access):
        check_access(e, f"access[{i}]", a)
    return e


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--allow-tags", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine readable output")
    args = ap.parse_args(argv)
    try:
        with open(args.file, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        print(f"::error file={args.file}::cannot read/parse: {exc}")
        return 2 if isinstance(exc, OSError) else 1
    e = validate(data, args.allow_tags)
    if args.json:
        print(json.dumps({"errors": e.items, "warnings": e.warnings}))
    for loc, msg in e.warnings:
        print(f"::warning file={args.file}::{loc}: {msg}")
    for loc, msg in e.items:
        print(f"::error file={args.file}::{loc}: {msg}")
    if e.items:
        print(f"{args.file}: INVALID ({len(e.items)} error(s))")
        return 1
    print(f"{args.file}: valid (app contract v2)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
