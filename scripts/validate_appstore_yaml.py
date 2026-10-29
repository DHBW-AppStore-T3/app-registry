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

# The apps' packages (app-ci publishes to apps/<name>), not every package of the organisation: the same
# allowlist as the platform's (APP_IMAGE_REGISTRY_ALLOWLIST, review S16).
REGISTRY_PREFIX = "ghcr.io/dhbw-appstore-t3/apps/"
MAX_CPU_M, MAX_MEM_MI, MAX_STORAGE_MI, MAX_CONTAINERS = 4000, 8192, 20 * 1024, 3
MAX_FILE_VARIABLE_BYTES = 1024 * 1024
# Same limits as the backend's app/utils/safe_yaml.py, so that an app cannot pass its CI and then be refused at submission.
MAX_YAML_BYTES = 256 * 1024
MAX_YAML_NESTING = 32

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


class UnsafeYaml(ValueError):
    """The document is refused before it is loaded."""


def load_yaml(raw: bytes) -> object:
    """``yaml.safe_load`` with the backend's limits: size, no aliases, bounded nesting."""
    if len(raw) > MAX_YAML_BYTES:
        raise UnsafeYaml(f"the YAML document is larger than {MAX_YAML_BYTES // 1024} KiB")
    depth = 0
    try:
        for event in yaml.parse(raw, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise UnsafeYaml("the YAML document uses aliases, which are not allowed")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > MAX_YAML_NESTING:
                    raise UnsafeYaml("the YAML document is nested too deeply")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
    except RecursionError:
        raise UnsafeYaml("the YAML document is nested too deeply") from None
    return yaml.safe_load(raw)


def cpu_m(v: object) -> int | None:
    m = CPU_RE.match(v) if isinstance(v, str) else None
    if not m:
        return None
    return int(m.group(1)) if m.group(1) is not None else round(float(m.group(2)) * 1000)


def mem_mi(v: object) -> int | None:
    m = MEM_RE.match(v) if isinstance(v, str) else None
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1) if m else None


def as_int(v: object) -> int | None:
    """An int the way pydantic's lax mode reads it: ints, integral floats and digit strings (not bools)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*[+-]?\d+\s*", v):
        return int(v)
    return None


def is_str(v: object) -> bool:
    return isinstance(v, str)


def need_str(e: Errors, loc: str, v: object) -> bool:
    """Report a non-string (pydantic ``str`` fields reject numbers, e.g. an unquoted ``cpu: 1``)."""
    if is_str(v):
        return True
    hint = " (quote it)" if isinstance(v, (int, float)) and not isinstance(v, bool) else ""
    e.add(loc, f"must be a string{hint}")
    return False


def need_str_list(e: Errors, loc: str, v: object) -> list[str]:
    """Report a non-list or non-string items; returns the valid string items."""
    if not isinstance(v, list):
        e.add(loc, "must be a list")
        return []
    for i, item in enumerate(v):
        need_str(e, f"{loc}[{i}]", item)
    return [item for item in v if is_str(item)]


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
    if "name" in c and need_str(e, f"{loc}.name", c["name"]) and not NAME_RE.match(c["name"]):
        e.add(f"{loc}.name", f"invalid container name {c['name']!r} (lowercase letters, digits, '-')")
    img = c.get("image")
    if "image" in c and need_str(e, f"{loc}.image", img):
        if not IMAGE_DIGEST_RE.match(img):
            if allow_tags and IMAGE_TAG_RE.match(img):
                e.warn(f"{loc}.image", "tag instead of digest: allowed in CI only, the AppStore requires @sha256:<digest>")
            else:
                e.add(f"{loc}.image", "image must be pinned by digest: <registry>/<repo>@sha256:<64 hex> (tags are not allowed)")
        if not img.startswith(REGISTRY_PREFIX):
            e.add(f"{loc}.image", f"registry not allowed (allowed prefixes: {REGISTRY_PREFIX})")
    expose = c.get("expose")
    if expose is not None and only_keys(e, f"{loc}.expose", expose, {"port", "path"}, {"port"}):
        port = as_int(expose.get("port"))
        if port is None or not 1024 <= port <= 65535:
            e.add(f"{loc}.expose.port", "must be an integer between 1024 and 65535")
        check_path(e, f"{loc}.expose.path", expose.get("path", "/"), ingress=True)
        if expose.get("path", "/") != "/" and is_str(expose.get("path")):
            e.warn(f"{loc}.expose.path", "only this path and below is routed to the app (the platform writes it as the "
                   "ingress path, prefix match): '/' unless the whole app really lives below it. A start page is "
                   "not what this field is for.")
    res = c.get("resources")
    if only_keys(e, f"{loc}.resources", res, {"cpu", "memory", "requests"}, {"cpu", "memory"}):
        cpu, mem = cpu_m(res.get("cpu")), mem_mi(res.get("memory"))
        if "cpu" in res and cpu is None:
            e.add(f"{loc}.resources.cpu", "invalid CPU quantity (a quoted string, e.g. '500m' or '1')")
        elif cpu is not None and cpu > MAX_CPU_M:
            e.add(f"{loc}.resources.cpu", f"exceeds platform limit of {MAX_CPU_M}m")
        if "memory" in res and mem is None:
            e.add(f"{loc}.resources.memory", "invalid memory quantity (a quoted string, e.g. '512Mi' or '1Gi')")
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
        if "name" in v and need_str(e, f"{l}.name", v["name"]) and not ENV_RE.match(v["name"]):
            e.add(f"{l}.name", f"invalid environment variable name {v['name']!r}")
        if (v.get("from") is None) == (v.get("value") is None):
            e.add(l, "exactly one of 'from' and 'value' is required")
        if v.get("from") is not None and v["from"] not in ("generated-password", "team-name", "user-name"):
            e.add(f"{l}.from", "must be one of generated-password, team-name, user-name")
        if v.get("value") is not None:
            need_str(e, f"{l}.value", v["value"])
    wp = need_str_list(e, f"{loc}.writablePaths", c.get("writablePaths", []))
    for i, p in enumerate(wp):
        check_path(e, f"{loc}.writablePaths[{i}]", p)
    if len(set(wp)) != len(wp):
        e.add(f"{loc}.writablePaths", "writablePaths must be unique")
    return True


def check_variable(e: Errors, loc: str, v: object) -> None:
    if not only_keys(e, loc, v, {"name", "type", "scope", "ext", "maxSize", "mountPath", "values", "default"},
                     {"name", "type"}):
        return
    if "name" in v and need_str(e, f"{loc}.name", v["name"]) and not NAME_RE.match(v["name"].replace("_", "-")):
        e.add(f"{loc}.name", f"invalid variable name {v['name']!r}")
    if v.get("type") not in ("file", "enum", "string"):
        e.add(f"{loc}.type", "must be one of file, enum, string")
    if v.get("scope", "deployment") not in ("deployment", "team"):
        e.add(f"{loc}.scope", "must be deployment or team")
    exts = need_str_list(e, f"{loc}.ext", v.get("ext", []))
    values = need_str_list(e, f"{loc}.values", v.get("values", []))
    if "default" in v and v["default"] is not None:
        need_str(e, f"{loc}.default", v["default"])
    if v.get("type") == "file":
        # Without ext (absent or []) the backend accepts any type except programs and uninspectable archives.
        # Require a non-empty list so an app cannot accidentally accept more than intended.
        if not exts and not any(l == f"{loc}.ext" for l, _ in e.items):
            e.add(f"{loc}.ext", "file variables need a non-empty ext list (e.g. [zip])")
        mp = v.get("mountPath")
        if not mp:
            e.add(f"{loc}.mountPath", "file variables need an absolute mountPath")
        elif need_str(e, f"{loc}.mountPath", mp):
            check_path(e, f"{loc}.mountPath", mp)
        ms = v.get("maxSize")
        if ms is not None and need_str(e, f"{loc}.maxSize", ms):
            m = SIZE_RE.match(ms)
            if not m:
                e.add(f"{loc}.maxSize", "invalid size (use e.g. '1Mi')")
            elif int(m.group(1)) * {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}[m.group(2)] > MAX_FILE_VARIABLE_BYTES:
                e.add(f"{loc}.maxSize", "maxSize above 1Mi is not supported yet (Secret mount limit)")
    elif "maxSize" in v and v["maxSize"] is not None:
        need_str(e, f"{loc}.maxSize", v["maxSize"])
    if v.get("type") == "enum":
        if not v.get("values"):
            e.add(f"{loc}.values", "enum variables need 'values'")
        elif is_str(v.get("default")) and v["default"] not in values:
            e.add(f"{loc}.default", "default must be one of 'values'")


def check_access(e: Errors, loc: str, a: object) -> None:
    if not only_keys(e, loc, a, {"type", "template", "from", "username"}, {"type"}):
        return
    if a.get("type") == "url":
        t = a.get("template")
        if not t:
            e.add(loc, "url access needs 'template'")
        elif need_str(e, f"{loc}.template", t):
            if not t.startswith("https://"):
                e.add(f"{loc}.template", "url access must start with 'https://'")
            else:
                placeholders(e, f"{loc}.template", t, URL_PLACEHOLDERS)
    elif a.get("type") == "password":
        if a.get("from") != "generated-password":
            e.add(loc, "password access needs 'from: generated-password'")
        if is_str(a.get("username")):
            placeholders(e, f"{loc}.username", a["username"], USER_PLACEHOLDERS)
    else:
        e.add(f"{loc}.type", "must be url or password")
    if a.get("type") != "password" and a.get("from") not in (None, "generated-password"):
        e.add(f"{loc}.from", "must be generated-password")
    if a.get("username") is not None:
        need_str(e, f"{loc}.username", a["username"])


VM_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
# The VM limits of the contract (VmSpec in the backend's app/k8s/spec.py).
VM_CORES = (1, 16)
VM_MEMORY_GIB = (2, 32)
VM_DISK_GIB = (40, 200)


_LAX_BOOLS = {"0", "1", "t", "f", "y", "n", "true", "false", "yes", "no", "on", "off"}


def _lax_bool(v: object) -> bool:
    """A bool the way pydantic's lax mode reads it (VmSpec.vnc_access is not StrictBool)."""
    return isinstance(v, bool) or (isinstance(v, int) and v in (0, 1)) or (isinstance(v, str) and v.lower() in _LAX_BOOLS)


def _vm_number(e: Errors, loc: str, v: object, lo: int, hi: int) -> int | None:
    # pydantic's int fields (VmSpec) read True as 1, unlike as_int.
    n = int(v) if isinstance(v, bool) else as_int(v)
    if n is None:
        e.add(loc, "must be a whole number")
    elif not lo <= n <= hi:
        e.add(loc, f"must be between {lo} and {hi}")
        return None
    return n


def check_vm(e: Errors, vm: object) -> None:
    """``vm`` of a kubevirt-vm app: sizes within the VM limits, the image to clone, optional bounds."""
    if not only_keys(e, "vm", vm, {"cpuCores", "memoryGiB", "diskGiB", "imageRef", "imageNamespace", "storageClass",
                                  "vncAccess", "bounds"}, {"imageRef"}):
        return
    assert isinstance(vm, dict)
    # Defaults as in VmSpec: 4 vCPUs, 8 GiB, 80 GiB.
    cores = _vm_number(e, "vm.cpuCores", vm["cpuCores"], *VM_CORES) if "cpuCores" in vm else 4
    memory = _vm_number(e, "vm.memoryGiB", vm["memoryGiB"], *VM_MEMORY_GIB) if "memoryGiB" in vm else 8
    if "diskGiB" in vm:
        _vm_number(e, "vm.diskGiB", vm["diskGiB"], *VM_DISK_GIB)
    for field in ("imageRef", "imageNamespace"):
        if field in vm and need_str(e, f"vm.{field}", vm[field]) and not VM_NAME_RE.match(vm[field]):
            e.add(f"vm.{field}", f"invalid {field} {vm[field]!r} (lowercase letters, digits, '-')")
    if "storageClass" in vm:
        need_str(e, "vm.storageClass", vm["storageClass"])
    if "vncAccess" in vm and not _lax_bool(vm["vncAccess"]):
        e.add("vm.vncAccess", "must be true or false")
    bounds = vm.get("bounds")
    if bounds is None or not only_keys(e, "vm.bounds", bounds, {"cpuCores", "memoryGiB"}, {"cpuCores", "memoryGiB"}):
        return
    for field, limits, default in (("cpuCores", VM_CORES, cores), ("memoryGiB", VM_MEMORY_GIB, memory)):
        loc = f"vm.bounds.{field}"
        if field not in bounds or not only_keys(e, loc, bounds[field], {"min", "max"}, {"min", "max"}):
            continue
        rng = bounds[field]
        lo = _vm_number(e, f"{loc}.min", rng.get("min"), *limits) if "min" in rng else None
        hi = _vm_number(e, f"{loc}.max", rng.get("max"), *limits) if "max" in rng else None
        if lo is not None and hi is not None:
            if lo > hi:
                e.add(loc, "min must not exceed max")
            elif default is not None and not lo <= default <= hi:
                e.add(f"vm.{field}", f"the default must lie within bounds.{field}")


LEGACY_NETWORK_KEYS = ("egress", "podNetwork", "userChoices")


def check_network(e: Errors, data: dict) -> None:
    """``network:`` (what the pods may reach, and what the person deploying may change), or the older keys.

    ``network.internet`` and ``network.podNetwork`` each take ``allowed`` (a choice in the wizard) and ``default``
    (the value it starts with, or the fixed one where it is not allowed); both false when left out. The older
    ``egress`` / ``podNetwork`` / ``userChoices`` of apps up to v2.x are still read, not combined with ``network``.
    """
    legacy = [k for k in LEGACY_NETWORK_KEYS if k in data]
    if "network" in data:
        if legacy:
            e.add("network", "cannot be combined with the older egress / podNetwork / userChoices")
        net = data["network"]
        if only_keys(e, "network", net, {"internet", "podNetwork"}):
            for key, setting in net.items():
                if only_keys(e, f"network.{key}", setting, {"allowed", "default"}):
                    for field, value in setting.items():
                        if not isinstance(value, bool):
                            e.add(f"network.{key}.{field}", "must be true or false")
        return
    if "egress" in data and data["egress"] not in ("none", "internet"):
        e.add("egress", "must be none or internet")
    if "podNetwork" in data and data["podNetwork"] not in ("none", "namespace"):
        e.add("podNetwork", "must be none or namespace")
    choices = data.get("userChoices")
    if choices is not None and only_keys(e, "userChoices", choices, {"internet", "podNetwork"}):
        for key, value in choices.items():
            if not isinstance(value, bool):
                e.add(f"userChoices.{key}", "must be true or false")


def validate(data: object, allow_tags: bool = False) -> Errors:
    e = Errors()
    if not only_keys(e, "", data, {"apiVersion", "name", "runtime", "scope", "workload", "vm", "network", "egress", "podNetwork",
                   "userChoices", "variables", "access"},
                     {"apiVersion", "name", "runtime"}):
        return e
    if data.get("apiVersion") != "appstore/v2":
        e.add("apiVersion", "must be 'appstore/v2'")
    if "name" in data and need_str(e, "name", data["name"]) and not NAME_RE.match(data["name"]):
        e.add("name", f"invalid app name {data['name']!r} (lowercase letters, digits, '-')")
    runtime = data.get("runtime")
    if runtime not in ("kubernetes", "kubevirt-vm"):
        e.add("runtime", "must be kubernetes or kubevirt-vm")
    if data.get("scope", "team") not in ("team", "user"):
        e.add("scope", "must be team or user")
    check_network(e, data)
    wl = data.get("workload")
    if runtime == "kubernetes" and wl is None:
        e.add("workload", "runtime 'kubernetes' requires 'workload'")
    if runtime == "kubevirt-vm":
        vm = data.get("vm")
        if vm is None:
            e.add("vm", "runtime 'kubevirt-vm' requires 'vm'")
        else:
            check_vm(e, vm)
        if wl is not None:
            e.add("workload", "runtime 'kubevirt-vm' does not use 'workload'")
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
                names = [c.get("name") for c in containers if is_str(c.get("name"))]
                if len(set(names)) != len(names):
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
    names = [v["name"] for v in vd if is_str(v.get("name"))]
    if len(set(names)) != len(names):
        e.add("variables", "variable names must be unique")
    files = [v["mountPath"] for v in vd if v.get("type") == "file" and is_str(v.get("mountPath"))]
    if len(set(files)) != len(files):
        e.add("variables", "file variables need distinct mountPaths")
    exposed = next((c for c in containers if isinstance(c, dict) and c.get("expose")), None)
    wpaths = exposed.get("writablePaths") if exposed else None
    taken = {p for p in wpaths if is_str(p)} if isinstance(wpaths, list) else set()
    if isinstance(storage, dict) and is_str(storage.get("mountPath")):
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
        with open(args.file, "rb") as fh:
            data = load_yaml(fh.read())
    except (OSError, yaml.YAMLError, UnsafeYaml) as exc:
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
