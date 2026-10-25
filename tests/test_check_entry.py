"""Tests for scripts/check_entry.py with a faked network (run: python -m unittest discover -s tests -v)."""
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

import check_entry as ce  # noqa: E402

SPEC = (HERE / "fixtures" / "valid.appstore.yaml").read_text(encoding="utf-8")
IMAGE = yaml.safe_load(SPEC)["workload"]["containers"][0]["image"]


def entry(**kw) -> dict:
    return {"name": "demo-ide", "repo": "https://github.com/DHBW-AppStore-T3/demo-ide", "owner": "DHBW-AppStore-T3",
            "runtime": "pods", "description": "Eine Beispiel-App fuer die Tests.", **kw}


class FakeGitHub:
    """Stands in for ce.gh / ce.fetch: ``files`` are the repo's paths on every ref, ``at_tag`` overrides them on the
    newest tag (a path mapped to None is absent there), ``repo`` is its metadata."""

    def __init__(self, files: dict[str, str], repo: dict | None = None, ghcr: int = 200, api: int = 200,
                 tags: list[str] | None = None, at_tag: dict[str, str | None] | None = None):
        self.files, self.ghcr, self.api = files, ghcr, api
        self.tags = ["v1.0.0", "v2.0.0", "v1.10.0"] if tags is None else tags
        self.at_tag = at_tag or {}
        self.repo = {"private": False, "visibility": "public", "archived": False, "default_branch": "main", **(repo or {})}

    def gh(self, path: str, raw: bool = False):
        if self.api != 200:
            return self.api, None
        if "/tags?" in path:
            return 200, [{"name": t} for t in self.tags + ["not-a-version", "latest"]]
        if path.startswith("repos/") and "/contents/" not in path:
            return 200, self.repo
        name, ref = path.split("/contents/")[1].split("?ref=")
        files = {**self.files, **self.at_tag} if ref == (max(self.tags, key=lambda t: tuple(map(int, t[1:].split(".")))) if self.tags else None) else self.files
        if files.get(name) is None:
            return 404, None
        return 200, files[name] if raw else [{"name": name}]

    def fetch(self, url: str, headers=None, method: str = "GET"):
        if "/token?" in url:
            return 200, json.dumps({"token": "t"}).encode()
        return self.ghcr, b""


class CheckEntry(unittest.TestCase):
    def run_check(self, ent: dict, fake: FakeGitHub | None = None, offline: bool = False, name: str | None = None,
                  others: dict[str, dict] | None = None) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            apps = Path(d)
            for fname, other in (others or {}).items():
                (apps / fname).write_text(yaml.safe_dump(other), encoding="utf-8")
            path = apps / (name or f"{ent.get('name')}.yaml")
            path.write_text(yaml.safe_dump(ent), encoding="utf-8")
            with mock.patch.object(ce, "gh", fake.gh if fake else None), \
                    mock.patch.object(ce, "fetch", fake.fetch if fake else None):
                return ce.check(path, offline)

    def test_good_pods_entry(self):
        self.assertEqual(self.run_check(entry(), FakeGitHub({"appstore.yaml": SPEC})), [])

    def test_the_vm_runtime_is_gone(self):
        errs = self.run_check(entry(runtime="vm"), offline=True)
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].startswith("schema:"))

    def test_offline_only_checks_schema_and_file_name(self):
        self.assertEqual(self.run_check(entry(), offline=True), [])
        self.assertEqual(self.run_check(entry(), offline=True, name="other.yaml"), ["file name other.yaml must be demo-ide.yaml"])
        errs = self.run_check(entry(runtime="docker", description="kurz"), offline=True)
        self.assertEqual(len(errs), 2)
        self.assertTrue(all(e.startswith("schema:") for e in errs))

    def test_repo_url_must_not_end_in_dot_git(self):
        errs = self.run_check(entry(repo="https://github.com/DHBW-AppStore-T3/demo-ide.git"), offline=True)
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].startswith("schema: repo"))

    def test_duplicate_repo(self):
        other = entry(name="zweite-app", repo="https://github.com/dhbw-appstore-t3/DEMO-IDE")
        errs = self.run_check(entry(), offline=True, others={"zweite-app.yaml": other})
        self.assertEqual(errs, ["repo https://github.com/DHBW-AppStore-T3/demo-ide is already registered by zweite-app.yaml"])

    def test_private_archived_or_missing_repo(self):
        fake = FakeGitHub({"appstore.yaml": SPEC}, repo={"private": True, "visibility": "private", "archived": True})
        self.assertEqual(len(self.run_check(entry(), fake)), 2)
        self.assertEqual(self.run_check(entry(), FakeGitHub({}, api=404)),
                         ["repo https://github.com/DHBW-AppStore-T3/demo-ide not reachable (HTTP 404)"])
        self.assertIn("rate limit", self.run_check(entry(), FakeGitHub({}, api=403))[0])

    def test_pods_entry_needs_appstore_yaml(self):
        self.assertEqual(self.run_check(entry(), FakeGitHub({})),
                         ["appstore.yaml missing on default branch 'main'", "appstore.yaml missing on tag v2.0.0"])

    def test_appstore_yaml_must_be_valid_named_alike_and_kubernetes(self):
        tag = SPEC.replace(IMAGE, "ghcr.io/dhbw-appstore-t3/apps/demo-ide:latest")
        errs = self.run_check(entry(), FakeGitHub({"appstore.yaml": tag}))
        self.assertEqual(len(errs), 1)
        self.assertIn("workload.containers[0].image", errs[0])
        errs = self.run_check(entry(name="andere-app"), FakeGitHub({"appstore.yaml": SPEC}))
        self.assertEqual(len(errs), 1)
        self.assertIn("must equal the registry name", errs[0])
        vm_spec = yaml.safe_dump({"apiVersion": "appstore/v2", "name": "demo-ide", "runtime": "openstack-vm"})
        errs = self.run_check(entry(), FakeGitHub({"appstore.yaml": vm_spec}))
        self.assertEqual(len(errs), 1)
        self.assertIn("needs 'kubernetes'", errs[0])

    def test_newest_tag_is_checked_like_the_default_branch(self):
        tag_spec = SPEC.replace(IMAGE, "ghcr.io/dhbw-appstore-t3/apps/demo-ide:latest")
        errs = self.run_check(entry(), FakeGitHub({"appstore.yaml": SPEC}, at_tag={"appstore.yaml": tag_spec}))
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].startswith("tag v2.0.0: appstore.yaml: workload.containers[0].image"), errs)
        errs = self.run_check(entry(), FakeGitHub({"appstore.yaml": SPEC}, at_tag={"appstore.yaml": None}))
        self.assertEqual(errs, ["appstore.yaml missing on tag v2.0.0"])

    def test_branch_error_is_labelled_and_not_repeated_for_an_identical_tag(self):
        bad = SPEC.replace("runtime: kubernetes", "runtime: openstack-vm")
        errs = self.run_check(entry(), FakeGitHub({"appstore.yaml": bad}))
        self.assertTrue(errs and all(e.startswith("default branch 'main': ") for e in errs), errs)

    def test_repo_without_version_tag(self):
        errs = self.run_check(entry(), FakeGitHub({"appstore.yaml": SPEC}, tags=[]))
        self.assertEqual(errs, ["no version tag (vX.Y.Z): the AppStore registers versions from git tags"])

    def test_development_entry_needs_no_tag_digest_or_public_image(self):
        tag_spec = SPEC.replace(IMAGE, "ghcr.io/dhbw-appstore-t3/apps/demo-ide:dev")
        fake = FakeGitHub({"appstore.yaml": tag_spec}, tags=[], ghcr=403)
        self.assertEqual(self.run_check(entry(status="development"), fake), [])
        # the same repo as a released app fails on all of it
        self.assertEqual(len(self.run_check(entry(), fake)), 3)  # no tag, tag-pinned image, image not pullable

    def test_development_entry_still_needs_a_valid_spec_and_a_public_repo(self):
        broken = SPEC.replace("runtime: kubernetes", "runtime: openstack-vm")
        errs = self.run_check(entry(status="development"), FakeGitHub({"appstore.yaml": broken}, tags=[]))
        self.assertTrue(errs and all(e.startswith("default branch 'main': ") for e in errs), errs)
        errs = self.run_check(entry(status="development"),
                              FakeGitHub({"appstore.yaml": SPEC}, tags=[], repo={"private": True, "visibility": "private"}))
        self.assertEqual(errs, ["repo must be PUBLIC (AppStore backend requirement)"])

    def test_image_must_be_pullable_anonymously(self):
        self.assertIn("set the package to Public", self.run_check(entry(), FakeGitHub({"appstore.yaml": SPEC}, ghcr=403))[0])
        self.assertIn("digest not found", self.run_check(entry(), FakeGitHub({"appstore.yaml": SPEC}, ghcr=404))[0])


class SpecImages(unittest.TestCase):
    def test_only_digest_pinned_ghcr_images_are_checked(self):
        import check_spec_images as csi
        digest = "ghcr.io/dhbw-appstore-t3/apps/a@sha256:" + "a" * 64
        spec = {"workload": {"containers": [
            {"image": digest}, {"image": digest}, {"image": "ghcr.io/dhbw-appstore-t3/apps/b:latest"},
            {"image": "docker.io/library/nginx@sha256:" + "b" * 64}, {"name": "no-image"}]}}
        self.assertEqual(csi.images(spec), [digest])
        self.assertEqual(csi.images(None), [])
        self.assertEqual(csi.images({"workload": None}), [])


if __name__ == "__main__":
    unittest.main()
