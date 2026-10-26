"""Seeded random mutations of a valid appstore.yaml, shared by the robustness and drift tests."""
from __future__ import annotations

import copy
import random
from typing import Any, Iterator

DIGEST = "sha256:" + "a" * 64
VALUES: list[Any] = [
    None, True, False, 0, 1, 80, 1023, 1024, 65536, -1, 1.5, 8080.0, "8080", "", "x", "a", "9a", "UPPER", "a_b",
    "ü", "/", "/a", "/a/", "/a/../b", "/tmp", "/home/project", "/opt/tasks.zip", "a\n", "/a\n",
    "500m", "1", "2", "3", "0m", "1.5", "1Ki", "1Mi", "2Mi", "1Gi", "4Gi", "5Gi", "20Gi", "21Gi", "500m\n",
    [], {}, ["a"], [1], [{}], [[1]], {"a": 1},
    "https://x/{foo}", "http://x", "https://{workload}.{zone}/", "https://", "{", "{user}", "{team}", "{deployment}",
    f"ghcr.io/dhbw-appstore-t3/x@{DIGEST}", "ghcr.io/dhbw-appstore-t3/x:tag", "ghcr.io/dhbw-appstore-t3/",
    f"ghcr.io/dhbw-appstore-t3-evil/x@{DIGEST}",
    "generated-password", "team-name", "user-name", "none", "internet", "kubernetes", "team", "user",
    "file", "enum", "string", "url", "password", "appstore/v2",
    # Both sides of every limit, so that a limit changed in the backend shows up as drift: container CPU (4 = 4000m),
    # memory (8Gi = 8192Mi), and the VM's cpuCores (1-16), memoryGiB (2-32), diskGiB (40-200).
    "4", "5", "4000m", "4001m", "8Gi", "9Gi", "8192Mi", "8193Mi",
    2, 16, 17, 32, 33, 39, 40, 200, 201,
]
KEYS = ["x", "name", "from", "value", "expose", "mountPath", "type", "path", "size"]
BOUNDS_KEYS = ["resourceBounds", "cpu", "memory", "min", "max"]
VM_KEYS = ["vm", "bounds", "cpuCores", "memoryGiB", "diskGiB", "imageRef", "imageNamespace", "storageClass", "vncAccess"]


def with_bounds(doc: Any, cpu: tuple[str, str] = ("250m", "2"), memory: tuple[str, str] = ("512Mi", "2Gi")) -> Any:
    """``doc`` with a valid ``resourceBounds`` on its first container (defaults: 500m / 1Gi lie inside)."""
    doc = copy.deepcopy(doc)
    doc["workload"]["containers"][0]["resourceBounds"] = {
        "cpu": {"min": cpu[0], "max": cpu[1]}, "memory": {"min": memory[0], "max": memory[1]}}
    return doc


def _paths(obj: Any, prefix: tuple = ()) -> Iterator[tuple]:
    yield prefix
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _paths(v, prefix + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _paths(v, prefix + (i,))


def _get(obj: Any, path: tuple) -> Any:
    for k in path:
        obj = obj[k]
    return obj


def mutate_once(doc: Any, rnd: random.Random, keys: list[str] = KEYS) -> Any:
    doc = copy.deepcopy(doc)
    path = rnd.choice([p for p in _paths(doc) if p])
    parent, key = _get(doc, path[:-1]), path[-1]
    op = rnd.choice(["del", "set", "set", "set", "addkey", "dup"])
    if op == "del":
        del parent[key]
    elif op == "set":
        parent[key] = rnd.choice(VALUES)
    elif op == "addkey" and isinstance(parent, dict):
        parent[rnd.choice(keys)] = rnd.choice(VALUES)
    elif op == "dup" and isinstance(parent, list):
        parent.append(copy.deepcopy(parent[key]))
    return doc


def mutants(seeds: list[Any], count: int, seed: int = 1, extra_keys: list[str] = ()) -> Iterator[Any]:
    rnd = random.Random(seed)
    keys = [*KEYS, *extra_keys]
    for _ in range(count):
        doc = rnd.choice(seeds)
        for _ in range(rnd.choice([1, 1, 2, 3])):
            doc = mutate_once(doc, rnd, keys)
        yield doc
