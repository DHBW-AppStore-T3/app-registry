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
from _mutate import mutants  # noqa: E402

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
        try:
            self.backend.parse_spec(copy.deepcopy(doc), self.backend.PlatformLimits())
            return True
        except self.backend.SpecValidationError:
            return False

    def test_same_verdict_as_the_backend(self):
        seed = yaml.safe_load((HERE / "fixtures" / "valid.appstore.yaml").read_text(encoding="utf-8"))
        self.assertTrue(self.backend_accepts(seed), "the valid fixture is rejected by the backend")
        problems = []
        for doc in [seed, *mutants([seed], ITERATIONS)]:
            mine = not v.validate(copy.deepcopy(doc), False).items
            theirs = self.backend_accepts(doc)
            if mine != theirs:
                problems.append(f"validator={'valid' if mine else 'invalid'} backend={'valid' if theirs else 'invalid'}: {doc}")
        self.assertEqual(problems[:3], [], f"{len(problems)} of {ITERATIONS} variations disagree")


if __name__ == "__main__":
    unittest.main()
