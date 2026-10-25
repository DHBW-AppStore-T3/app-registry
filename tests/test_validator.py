"""Tests for scripts/validate_appstore_yaml.py (run: python -m unittest discover -s tests -v)."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE))

import validate_appstore_yaml as v  # noqa: E402
from _mutate import BOUNDS_KEYS, mutants, with_bounds  # noqa: E402

FIXTURES = HERE / "fixtures"


def load(name: str) -> dict:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


def locs(doc: object, allow_tags: bool = False) -> set[str]:
    return {loc for loc, _ in v.validate(doc, allow_tags).items}


class Fixtures(unittest.TestCase):
    def test_valid_fixture_is_valid(self):
        self.assertEqual(v.validate(load("valid.appstore.yaml")).items, [])

    def test_invalid_fixture_reports_every_planted_error(self):
        self.assertEqual(locs(load("invalid.appstore.yaml")), {
            "foo",
            "name",
            "workload.containers[0].image",
            "workload.containers[0].expose.port",
            "workload.containers[0].resources.cpu",
        })

    def test_invalid_resource_bounds_fixture(self):
        self.assertEqual(locs(load("invalid-resource-bounds.appstore.yaml")), {
            "workload.containers[0].image",
            "workload.containers[0].resourceBounds.cpu.max",
            "workload.containers[0].resources.cpu",
        })


class Contract(unittest.TestCase):
    def setUp(self):
        self.doc = load("valid.appstore.yaml")
        self.c = self.doc["workload"]["containers"][0]

    def test_tag_is_error_unless_allowed(self):
        self.c["image"] = "ghcr.io/dhbw-appstore-t3/apps/demo-ide:latest"
        self.assertEqual(locs(self.doc), {"workload.containers[0].image"})
        result = v.validate(self.doc, allow_tags=True)
        self.assertEqual(result.items, [])
        self.assertEqual([loc for loc, _ in result.warnings], ["workload.containers[0].image"])

    def test_foreign_registry_is_always_an_error(self):
        self.c["image"] = self.c["image"].replace("ghcr.io/dhbw-appstore-t3/", "ghcr.io/dhbw-appstore-t3-evil/")
        self.assertEqual(locs(self.doc, allow_tags=True), {"workload.containers[0].image"})

    def test_a_platform_package_is_no_app_image(self):
        self.c["image"] = self.c["image"].replace("ghcr.io/dhbw-appstore-t3/apps/demo-ide", "ghcr.io/dhbw-appstore-t3/backend")
        self.assertEqual(locs(self.doc, allow_tags=True), {"workload.containers[0].image"})

    def test_unquoted_cpu_is_rejected(self):
        self.c["resources"]["cpu"] = 1
        self.assertEqual(locs(self.doc), {"workload.containers[0].resources.cpu"})

    def test_platform_limits(self):
        # the ceilings follow the pod-app limits of the deployment chart (4 vCPU / 8 GiB per container, 20 GiB volume)
        self.assertEqual((v.MAX_CPU_M, v.MAX_MEM_MI, v.MAX_STORAGE_MI), (4000, 8192, 20480))
        self.c["resources"]["cpu"] = "5"
        self.c["resources"]["memory"] = "9Gi"
        self.doc["workload"]["storage"]["size"] = "21Gi"
        self.assertEqual(locs(self.doc), {
            "workload.containers[0].resources.cpu",
            "workload.containers[0].resources.memory",
            "workload.storage.size",
        })

    def test_requests_must_not_exceed_limits(self):
        self.c["resources"]["requests"] = {"cpu": "1", "memory": "2Gi"}
        self.assertEqual(locs(self.doc), {
            "workload.containers[0].resources.requests.cpu",
            "workload.containers[0].resources.requests.memory",
        })

    def test_exactly_one_exposed_container(self):
        second = copy.deepcopy(self.c)
        second["name"] = "other"
        self.doc["workload"]["containers"].append(second)
        self.assertEqual(locs(self.doc), {"workload.containers"})

    def test_at_most_three_containers(self):
        for i in range(3):
            extra = copy.deepcopy(self.c)
            extra["name"], extra["expose"] = f"extra{i}", None
            self.doc["workload"]["containers"].append(extra)
        self.assertIn("workload.containers", locs(self.doc))

    def test_file_variable_must_not_collide_with_storage(self):
        self.doc["variables"][0]["mountPath"] = "/home/project"
        self.assertEqual(locs(self.doc), {"variables"})

    def test_file_variable_max_size(self):
        self.doc["variables"][0]["maxSize"] = "2Mi"
        self.assertEqual(locs(self.doc), {"variables[0].maxSize"})

    def test_file_variable_needs_an_ext_list(self):
        del self.doc["variables"][0]["ext"]
        self.assertEqual(locs(self.doc), {"variables[0].ext"})
        self.doc["variables"][0]["ext"] = []  # explicit empty list: allowed (same as absent for the backend)
        self.assertEqual(locs(self.doc), set())

    def test_access_template_must_be_https_with_known_placeholders(self):
        self.doc["access"][0]["template"] = "http://{workload}"
        self.doc["access"][1]["username"] = "{nope}"
        self.assertEqual(locs(self.doc), {"access[0].template", "access[1].username"})

    def test_port_is_read_like_the_backend(self):
        # pydantic's lax mode accepts "8080" and 8080.0 for an int field
        for ok in ("8080", 8080.0):
            self.c["expose"]["port"] = ok
            self.assertEqual(locs(self.doc), set(), ok)
        for bad in (True, "80", 70000, None, "eighty"):
            self.c["expose"]["port"] = bad
            self.assertEqual(locs(self.doc), {"workload.containers[0].expose.port"}, bad)

    def test_expose_path_other_than_root_warns(self):
        # Web-LaTeX used `/login` as a start page: the ingress then routed only /login and the editor answered 404.
        self.c["expose"]["path"] = "/login"
        result = v.validate(self.doc)
        self.assertEqual(result.items, [])
        self.assertEqual([loc for loc, _ in result.warnings], ["workload.containers[0].expose.path"])
        self.c["expose"]["path"] = "/"
        self.assertEqual(v.validate(self.doc).warnings, [])
        del self.c["expose"]["path"]
        self.assertEqual(v.validate(self.doc).warnings, [])

    def test_unknown_runtime_is_rejected(self):
        self.doc["runtime"] = "openstack-vm"
        self.assertIn("runtime", locs(self.doc))

    def test_wrong_api_version_and_non_mapping(self):
        self.doc["apiVersion"] = "appstore/v1"
        self.assertEqual(locs(self.doc), {"apiVersion"})
        self.assertEqual(locs(["not", "a", "mapping"]), {"<root>"})
        self.assertEqual(locs(None), {"<root>"})


class ResourceBounds(unittest.TestCase):
    def setUp(self):
        self.doc = with_bounds(load("valid.appstore.yaml"))
        self.c = self.doc["workload"]["containers"][0]
        self.b = self.c["resourceBounds"]

    def test_valid_bounds(self):
        self.assertEqual(locs(self.doc), set())
        self.b["cpu"]["max"], self.b["memory"]["max"] = "4", "8Gi"  # the platform ceilings themselves
        self.assertEqual(locs(self.doc), set())

    def test_max_must_stay_within_platform_limits(self):
        self.b["cpu"]["max"], self.b["memory"]["max"] = "5", "9Gi"
        self.assertEqual(locs(self.doc), {
            "workload.containers[0].resourceBounds.cpu.max", "workload.containers[0].resourceBounds.memory.max"})

    def test_min_must_not_exceed_max(self):
        self.b["cpu"]["min"] = "3"
        self.assertEqual(locs(self.doc) & {"workload.containers[0].resourceBounds.cpu"},
                         {"workload.containers[0].resourceBounds.cpu"})

    def test_defaults_must_lie_within_bounds(self):
        self.c["resources"]["cpu"], self.c["resources"]["memory"] = "100m", "4Gi"
        self.assertEqual(locs(self.doc), {
            "workload.containers[0].resources.cpu", "workload.containers[0].resources.memory"})

    def test_both_ranges_are_required_and_unknown_fields_rejected(self):
        del self.b["memory"]
        self.assertEqual(locs(self.doc), {"workload.containers[0].resourceBounds.memory"})
        self.b["memory"] = {"min": "1Gi", "max": "2Gi", "step": "1"}
        self.assertEqual(locs(self.doc), {"workload.containers[0].resourceBounds.memory.step"})

    def test_unquoted_numbers_are_rejected(self):
        self.b["cpu"]["max"] = 2
        self.assertEqual(locs(self.doc), {"workload.containers[0].resourceBounds.cpu"})


class Robustness(unittest.TestCase):
    def test_garbage_never_crashes_the_validator(self):
        seeds = [load("valid.appstore.yaml"), with_bounds(load("valid.appstore.yaml"))]
        for doc in mutants(seeds, 4000, extra_keys=BOUNDS_KEYS):
            v.validate(doc, False)
            v.validate(doc, True)


class KubevirtVm(unittest.TestCase):
    """``vm`` of a kubevirt-vm app: the same limits as VmSpec in the backend."""

    BASE = {
        "apiVersion": "appstore/v2", "name": "windows", "runtime": "kubevirt-vm", "scope": "user",
        "vm": {"cpuCores": 4, "memoryGiB": 8, "diskGiB": 90, "imageRef": "windows11-matlab-golden"},
        "userChoices": {"internet": True, "podNetwork": True},
    }
    BOUNDS = {"cpuCores": {"min": 2, "max": 8}, "memoryGiB": {"min": 4, "max": 16}}

    def doc(self, **vm):
        out = copy.deepcopy(self.BASE)
        out["vm"].update(vm)
        return out

    def test_a_vm_app_with_and_without_bounds_is_valid(self):
        self.assertEqual(locs(self.doc()), set())
        self.assertEqual(locs(self.doc(bounds=self.BOUNDS, imageNamespace="vm-images", vncAccess=True)), set())

    def test_sizes_outside_the_vm_limits_are_refused(self):
        self.assertEqual(locs(self.doc(cpuCores=17)), {"vm.cpuCores"})
        self.assertEqual(locs(self.doc(memoryGiB=1)), {"vm.memoryGiB"})
        self.assertEqual(locs(self.doc(diskGiB=20)), {"vm.diskGiB"})
        self.assertEqual(locs(self.doc(cpuCores="many")), {"vm.cpuCores"})

    def test_unknown_and_missing_fields_are_refused(self):
        self.assertEqual(locs(self.doc(gpu=True)), {"vm.gpu"})
        doc = self.doc()
        del doc["vm"]["imageRef"]
        self.assertEqual(locs(doc), {"vm.imageRef"})

    def test_only_the_image_is_required_the_sizes_have_defaults(self):
        doc = self.doc()
        for field in ("cpuCores", "memoryGiB", "diskGiB"):
            del doc["vm"][field]
        self.assertEqual(locs(doc), set())

    def test_the_image_must_be_a_plain_name(self):
        self.assertEqual(locs(self.doc(imageRef="Golden Image")), {"vm.imageRef"})
        self.assertEqual(locs(self.doc(imageNamespace="../kube-system")), {"vm.imageNamespace"})

    def test_bounds_must_be_ordered_within_the_limits_and_contain_the_default(self):
        bad_order = {"cpuCores": {"min": 8, "max": 2}, "memoryGiB": self.BOUNDS["memoryGiB"]}
        self.assertEqual(locs(self.doc(bounds=bad_order)), {"vm.bounds.cpuCores"})
        too_big = {"cpuCores": {"min": 2, "max": 32}, "memoryGiB": self.BOUNDS["memoryGiB"]}
        self.assertEqual(locs(self.doc(bounds=too_big)), {"vm.bounds.cpuCores.max"})
        self.assertEqual(locs(self.doc(cpuCores=1, bounds=self.BOUNDS)), {"vm.cpuCores"})
        self.assertEqual(locs(self.doc(bounds={"cpuCores": self.BOUNDS["cpuCores"]})), {"vm.bounds.memoryGiB"})


class YamlLimits(unittest.TestCase):
    """The same three rules as the backend's safe_yaml: size, aliases, nesting."""

    def test_plain_document_loads(self):
        self.assertEqual(v.load_yaml(b"a: [1, 2]\nb: {c: d}\n"), {"a": [1, 2], "b": {"c": "d"}})

    def test_aliases_are_refused(self):
        with self.assertRaisesRegex(v.UnsafeYaml, "aliases"):
            v.load_yaml(b"a: &x [1]\nb: *x\n")

    def test_billion_laughs_is_refused_without_expanding(self):
        lol = b"a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\nc: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
        with self.assertRaises(v.UnsafeYaml):
            v.load_yaml(lol)

    def test_nesting_limit(self):
        ok = b"[" * v.MAX_YAML_NESTING + b"]" * v.MAX_YAML_NESTING
        self.assertIsNotNone(v.load_yaml(ok))
        too_deep = b"[" * (v.MAX_YAML_NESTING + 1) + b"]" * (v.MAX_YAML_NESTING + 1)
        with self.assertRaisesRegex(v.UnsafeYaml, "nested too deeply"):
            v.load_yaml(too_deep)

    def test_size_limit(self):
        with self.assertRaisesRegex(v.UnsafeYaml, "larger than"):
            v.load_yaml(b"a: " + b"x" * v.MAX_YAML_BYTES)


if __name__ == "__main__":
    unittest.main()
