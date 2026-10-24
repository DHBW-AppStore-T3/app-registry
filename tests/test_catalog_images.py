"""Tests for scripts/catalog_images.py with a faked GitHub (run: python -m unittest discover -s tests -v)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import catalog_images as ci  # noqa: E402

SPEC = (HERE / "fixtures" / "valid.appstore.yaml").read_text(encoding="utf-8")
IMAGE = yaml.safe_load(SPEC)["workload"]["containers"][0]["image"]
NEWER = IMAGE.split("@")[0] + "@sha256:" + "b" * 64


def entry(**kw) -> dict:
    return {"name": "demo-ide", "repo": "https://github.com/DHBW-AppStore-T3/demo-ide", "owner": "DHBW-AppStore-T3",
            "runtime": "pods", "description": "Eine Beispiel-App fuer die Tests.", **kw}


class FakeGitHub:
    """``specs`` and ``ignores``: appstore.yaml / .trivyignore per ref (absent: 404)."""

    def __init__(self, specs: dict[str, str], ignores: dict[str, str] | None = None, tags=("v1.0.0", "v1.2.0")):
        self.specs, self.ignores, self.tags = specs, ignores or {}, list(tags)

    def gh(self, path: str, raw: bool = False):
        if "/tags?" in path:
            return 200, [{"name": t} for t in self.tags]
        if "/contents/" not in path:
            return 200, {"default_branch": "main"}
        name, ref = path.split("/contents/")[1].split("?ref=")
        files = self.specs if name == "appstore.yaml" else self.ignores
        if ref not in files:
            return 404, None
        return 200, files[ref] if raw else {"name": name}


class CatalogImages(unittest.TestCase):
    def rows(self, ent: dict, fake: FakeGitHub) -> list[dict]:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / f"{ent['name']}.yaml"
            path.write_text(yaml.safe_dump(ent), encoding="utf-8")
            with mock.patch.object(ci, "gh", fake.gh), mock.patch.object(ci, "latest_tag", lambda _r: fake.tags[-1]):
                return ci.rows(path)

    def test_the_newest_tag_and_the_default_branch_once_per_image(self):
        fake = FakeGitHub({"v1.2.0": SPEC, "main": SPEC.replace(IMAGE, NEWER)}, ignores={"v1.2.0": "CVE-1\n"})
        self.assertEqual(self.rows(entry(), fake), [
            {"app": "demo-ide", "ref": "v1.2.0", "image": IMAGE,
             "trivyignore": "https://raw.githubusercontent.com/DHBW-AppStore-T3/demo-ide/v1.2.0/.trivyignore"},
            {"app": "demo-ide", "ref": "main", "image": NEWER, "trivyignore": ""},
        ])

    def test_an_image_pinned_on_both_refs_is_scanned_once(self):
        fake = FakeGitHub({"v1.2.0": SPEC, "main": SPEC})
        self.assertEqual([r["ref"] for r in self.rows(entry(), fake)], ["v1.2.0"])

    def test_vm_and_development_entries_are_skipped(self):
        fake = FakeGitHub({"v1.2.0": SPEC, "main": SPEC})
        self.assertEqual(self.rows(entry(runtime="vm"), fake), [])
        self.assertEqual(self.rows(entry(status="development"), fake), [])

    def test_only_digest_pinned_ghcr_images(self):
        spec = yaml.safe_load(SPEC)
        spec["workload"]["containers"][0]["image"] = "ghcr.io/dhbw-appstore-t3/apps/x:latest"
        self.assertEqual(ci.images_of(yaml.safe_dump(spec)), [])
        self.assertEqual(ci.images_of("not: [yaml"), [])
        self.assertEqual(ci.images_of(SPEC), [IMAGE])

    def test_main_prints_a_matrix(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ci, "rows", lambda p: [{"app": p.stem}]), \
                mock.patch("builtins.print") as out:
            self.assertEqual(ci.main([str(Path(d) / "a.yaml")]), 0)
        self.assertEqual(json.loads(out.call_args.args[0]), {"include": [{"app": "a"}]})


if __name__ == "__main__":
    unittest.main()
