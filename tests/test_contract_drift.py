"""Does validate_appstore_yaml.py still agree with the backend's contract parser?

The backend (DHBW-AppStore-T3/backend, app/k8s/spec.py, branch dev) is the source of truth. This test
runs random broken variations of the valid fixture through both and fails when one accepts what the other
rejects. It needs the backend file, so it is skipped unless BACKEND_SPEC_PY points to a copy of it
(the contract-drift workflow downloads it; needs pydantic):

    BACKEND_SPEC_PY=/path/to/spec.py python -m unittest tests.test_contract_drift
"""
from __future__ import annotations

import copy
import importlib.util
import os
import sys
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE))

import validate_appstore_yaml as v  # noqa: E402
from _mutate import BOUNDS_KEYS, NETWORK_KEYS, VM_KEYS, mutants, with_bounds  # noqa: E402

SPEC_PY = os.environ.get("BACKEND_SPEC_PY")
ITERATIONS = int(os.environ.get("DRIFT_ITERATIONS", "20000"))


def load_backend(path: str):
    spec = importlib.util.spec_from_file_location("backend_spec", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["backend_spec"] = module  # pydantic resolves the module's annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SPEC_PY, "BACKEND_SPEC_PY not set")
class ContractDrift(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = load_backend(SPEC_PY)

    def backend_accepts(self, doc) -> bool:
        # The ceilings are platform configuration (deployment chart), not part of the parser: both sides get the
        # validator's, so this compares the rules. check_platform_limits.py compares the ceilings with the chart.
        extra = {}
        if hasattr(self.backend.PlatformLimits, "vm_image_namespaces"):
            # Which namespaces a VM may clone from is configuration too (APP_VM_IMAGE_NAMESPACES): allow the doc's.
            vm = doc.get("vm") if isinstance(doc, dict) else None
            namespace = vm.get("imageNamespace") if isinstance(vm, dict) else None
            extra["vm_image_namespaces"] = (namespace,) if isinstance(namespace, str) else ("vm-poc",)
        limits = self.backend.PlatformLimits(
            max_cpu_millicores=v.MAX_CPU_M, max_memory_mib=v.MAX_MEM_MI, max_storage_mib=v.MAX_STORAGE_MI, **extra)
        try:
            self.backend.parse_spec(copy.deepcopy(doc), limits)
            return True
        except self.backend.SpecValidationError:
            return False

    def _is_known_stricter(self, doc) -> bool:
        """Return True when the validator is intentionally stricter than the backend.

        Known stricter rules (validator rejects, backend accepts):
        - A file variable without an 'ext' key: the validator requires it so future
          apps cannot accidentally accept every file type.  Missing key and ext: [] are
          both rejected (.github#13, item 3.5).
        """
        vars_ = doc.get("variables") if isinstance(doc, dict) else []
        if not isinstance(vars_, list):
            return False
        return any(
            isinstance(v, dict) and v.get("type") == "file" and not v.get("ext")
            for v in vars_
        )

    def _is_backend_stricter(self, doc) -> bool:
        """Return True when the backend is intentionally stricter than the validator.

        Known stricter rules (backend rejects, validator accepts):
        - The backend's image registry allowlist is narrower than the validator's
          REGISTRY_PREFIX check: the backend also requires the image path to start
          with 'apps/' (imageRegistryAllowlist in the deployment chart); the
          validator only checks the hostname prefix.
        """
        if not isinstance(doc, dict):
            return False
        wl = doc.get("workload")
        if not isinstance(wl, dict):
            return False
        containers = wl.get("containers")
        if not isinstance(containers, list):
            return False
        from validate_appstore_yaml import REGISTRY_PREFIX
        for c in containers:
            if not isinstance(c, dict):
                continue
            img = c.get("image", "")
            if isinstance(img, str) and img.startswith(REGISTRY_PREFIX) and "@sha256:" in img:
                path_after_prefix = img[len(REGISTRY_PREFIX):]
                # backend requires the path to start with 'apps/'
                if not path_after_prefix.startswith("apps/"):
                    return True
        return False

    def test_same_verdict_as_the_backend(self):
        seed = yaml.safe_load((HERE / "fixtures" / "valid.appstore.yaml").read_text(encoding="utf-8"))
        self.assertTrue(self.backend_accepts(seed), "the valid fixture is rejected by the backend")
        # network: (allowed/default) and the older egress / podNetwork / userChoices of apps up to v2.x
        legacy = yaml.safe_load((HERE / "fixtures" / "valid-legacy-network.appstore.yaml").read_text(encoding="utf-8"))
        self.assertTrue(self.backend_accepts(legacy), "the legacy network fixture is rejected by the backend")
        seeds, extra_keys = [seed, legacy], list(NETWORK_KEYS)
        # resourceBounds is only in the backend once feat/resource-overrides is merged into dev
        if hasattr(self.backend, "ResourceBounds"):
            self.assertTrue(self.backend_accepts(with_bounds(seed)), "valid resourceBounds are rejected by the backend")
            seeds, extra_keys = [*seeds, with_bounds(seed)], [*extra_keys, *BOUNDS_KEYS]
        # kubevirt-vm (VmSpec, with bounds) is in the backend since feat/kubevirt-vm-complete
        if hasattr(self.backend, "VmBounds"):
            vm_seed = yaml.safe_load((HERE / "fixtures" / "valid-kubevirt.appstore.yaml").read_text(encoding="utf-8"))
            self.assertTrue(self.backend_accepts(vm_seed), "the valid kubevirt fixture is rejected by the backend")
            self.assertEqual(v.validate(copy.deepcopy(vm_seed)).items, [])
            # The same VM without bounds: with them, the defaults must lie inside the bounds, which hides the
            # limits themselves (cpuCores 1-16, memoryGiB 2-32, diskGiB 40-200) from the mutations.
            vm_plain = copy.deepcopy(vm_seed)
            del vm_plain["vm"]["bounds"]
            self.assertTrue(self.backend_accepts(vm_plain), "the kubevirt fixture without bounds is rejected by the backend")
            seeds, extra_keys = [*seeds, vm_seed, vm_plain], [*extra_keys, *VM_KEYS]
        problems = []
        for doc in [*seeds, *mutants(seeds, ITERATIONS, extra_keys=extra_keys)]:
            mine = not v.validate(copy.deepcopy(doc), False).items
            theirs = self.backend_accepts(doc)
            if mine != theirs:
                # Validator intentionally stricter: do not report as drift.
                if not mine and theirs and self._is_known_stricter(doc):
                    continue
                # Backend intentionally stricter: do not report as drift.
                if mine and not theirs and self._is_backend_stricter(doc):
                    continue
                problems.append(f"validator={'valid' if mine else 'invalid'} backend={'valid' if theirs else 'invalid'}: {doc}")
        self.assertEqual(problems[:3], [], f"{len(problems)} of {ITERATIONS} variations disagree")


    def test_scope_choice_same_verdict_as_the_backend(self):
        """``scopeChoice`` (the person deploying chooses the scope) is only in the backend once feat/scope-choice is merged."""
        if "scope_choice" not in self.backend.AppSpec.model_fields:
            self.skipTest("the backend does not know scopeChoice yet")
        seed = yaml.safe_load((HERE / "fixtures" / "valid.appstore.yaml").read_text(encoding="utf-8"))
        problems = []
        for scope in ("team", "user", "everyone", None, 1):
            for choice in (True, False, None, "yes", "true", 1, 0, [], {}, "user"):
                doc = copy.deepcopy(seed)
                doc["scope"], doc["scopeChoice"] = scope, choice
                mine, theirs = not v.validate(copy.deepcopy(doc), False).items, self.backend_accepts(doc)
                if mine != theirs:
                    problems.append(f"scope={scope!r} scopeChoice={choice!r}: validator={mine} backend={theirs}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
