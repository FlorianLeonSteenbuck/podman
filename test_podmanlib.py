# -*- coding: utf-8 -*-
"""Tests for podmanlib. Run: python test_podmanlib.py"""
from __future__ import absolute_import, print_function

import json
import os
import shutil
import tempfile
import unittest
from collections import OrderedDict

import podmanlib as pl


class SemverPortTest(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertTrue(pl.IsValid("v1.0.0"))
        self.assertTrue(pl.IsValid("v1"))
        self.assertTrue(pl.IsValid("v1.2"))
        self.assertTrue(pl.IsValid("v1.2.3-alpha.1+build.5"))
        self.assertFalse(pl.IsValid("1.0.0"))
        self.assertFalse(pl.IsValid("v01.0.0"))
        self.assertFalse(pl.IsValid("v1.0.0-"))
        self.assertFalse(pl.IsValid("v1.0.0-01"))
        self.assertFalse(pl.IsValid(""))

    def test_canonical_and_parts(self):
        self.assertEqual(pl.Canonical("v1"), "v1.0.0")
        self.assertEqual(pl.Canonical("v1.2"), "v1.2.0")
        self.assertEqual(pl.Canonical("v1.2.3+meta"), "v1.2.3")
        self.assertEqual(pl.Major("v2.1.0"), "v2")
        self.assertEqual(pl.MajorMinor("v2.1.0"), "v2.1")
        self.assertEqual(pl.MajorMinor("v2"), "v2.0")
        self.assertEqual(pl.Prerelease("v1.0.0-pre+meta"), "-pre")
        self.assertEqual(pl.Build("v1.0.0+meta"), "+meta")
        self.assertEqual(pl.Major("nope"), "")

    def test_compare(self):
        self.assertEqual(pl.Compare("v1.0.0", "v1.0.0"), 0)
        self.assertEqual(pl.Compare("v1.0.0-alpha", "v1.0.0"), -1)
        self.assertEqual(pl.Compare("v1.10.0", "v1.9.0"), 1)
        self.assertEqual(pl.Compare("bad", "v0.0.1"), -1)
        self.assertEqual(pl.Compare("bad", "worse"), 0)

    def test_sort_follows_semver_spec_order(self):
        ordered = ["v1.0.0-alpha", "v1.0.0-alpha.1", "v1.0.0-alpha.beta", "v1.0.0-beta",
                   "v1.0.0-beta.2", "v1.0.0-beta.11", "v1.0.0-rc.1", "v1.0.0"]
        shuffled = list(reversed(ordered))
        pl.Sort(shuffled)
        self.assertEqual(shuffled, ordered)

    def test_max_canonicalizes(self):
        self.assertEqual(pl.Max("v1", "v1.0.1"), "v1.0.1")


class GoModTest(unittest.TestCase):
    SAMPLE = """
    module go.podman.io/podman/v6

    go 1.24.0 // comment

    require (
        go.podman.io/common v0.70.1-0.20261116084638-6da6e80dc141
        github.com/x/y v1.2.3 // indirect
    )

    require github.com/single/line v0.1.0

    replace (
        github.com/a/b => ../local/b
    )
    replace github.com/c/d v1.0.0 => github.com/e/f v2.0.0
    """

    def test_parse(self):
        mod = pl.parse_go_mod(self.SAMPLE)
        self.assertEqual(mod.module, "go.podman.io/podman/v6")
        self.assertEqual(mod.requires["go.podman.io/common"],
                         "v0.70.1-0.20261116084638-6da6e80dc141")
        self.assertEqual(mod.requires["github.com/x/y"], "v1.2.3")
        self.assertEqual(mod.requires["github.com/single/line"], "v0.1.0")
        self.assertEqual(len(mod.requires), 3)
        self.assertEqual(mod.replaces["github.com/a/b"], "../local/b")
        self.assertEqual(mod.replaces["github.com/c/d"], "github.com/e/f v2.0.0")


class DriftTest(unittest.TestCase):
    def test_drift_words(self):
        self.assertEqual(pl.drift_of("v1.0.0", "v1.0.0"), "same")
        self.assertEqual(pl.drift_of("v0.1.0", "v0.2.0"), "older")
        self.assertEqual(pl.drift_of("v0.70.1-0.2026-abc", "v0.70.0"), "newer (untagged)")
        self.assertEqual(pl.drift_of("v1.0.0", ""), "no tag")
        self.assertEqual(pl.drift_of("nope", "v1.0.0"), "invalid")


class LocalVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = pl.RepoSpec("demo", "file:///unused", "test", self.tmp)
        os.makedirs(self.repo.path)
        self.git("init", "-q")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com",
                 "commit", "-q", "--allow-empty", "-m", "init")
        for tag in ["v1.2.0", "v1.10.0", "sub/v0.3.0", "sub/v0.10.0", "sub/v0.4.0-rc1"]:
            self.git("tag", tag)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def git(self, *args):
        res = pl.git(self.repo.path, *args)
        self.assertTrue(res.ok, res.stderr)
        return res

    def test_root_and_subdir_tags_are_separate(self):
        self.assertEqual(pl.local_version(self.repo, "."), "v1.10.0")
        self.assertEqual(pl.local_version(self.repo, "sub"), "v0.10.0")
        self.assertEqual(pl.local_version(self.repo, "other"), "")


class FormatterTest(unittest.TestCase):
    def rows(self):
        first = OrderedDict([("repo", "a"), ("commit", "x")])
        second = OrderedDict([("repo", "bb"), ("commit", "yy")])
        return [first, second]

    def test_table(self):
        lines = pl.make_formatter("table").render(self.rows()).splitlines()
        self.assertEqual(lines[0].split(), ["repo", "commit"])
        self.assertEqual(lines[2].split(), ["a", "x"])
        self.assertEqual(lines[3].split(), ["bb", "yy"])

    def test_json(self):
        data = json.loads(pl.make_formatter("json").render(self.rows()))
        self.assertEqual(data[1]["repo"], "bb")

    def test_dot(self):
        rows = [OrderedDict([("src", "a"), ("dst", "b"), ("module", "m"), ("drift", "same")])]
        text = pl.make_formatter("dot").render(rows)
        self.assertTrue(text.startswith("digraph"))
        self.assertIn('"a" -> "b"', text)

    def test_unknown_format_falls_back_to_table(self):
        self.assertIsInstance(pl.make_formatter("nope"), pl.TableFormatter)


class OperationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = pl.RepoSpec("demo", "u", "r", self.tmp)
        os.makedirs(os.path.join(self.repo.path, ".git"))  # pretend it is checked out

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_make_skipped_without_makefile(self):
        bus = pl.EventBus()
        rec = pl.RecordingObserver()
        bus.attach(rec)
        outcome = pl.Invoker(bus).execute(pl.MakeTarget(self.repo, "help", self.tmp))
        self.assertEqual(outcome.status, pl.SKIP)
        self.assertEqual([e for e, _ in rec.events], ["start", "skip"])

    def test_make_runs_and_reports_missing_target(self):
        with open(os.path.join(self.repo.path, "Makefile"), "w") as fh:
            fh.write("help:\n\t@echo hi\n")
        invoker = pl.Invoker(pl.EventBus())
        self.assertEqual(invoker.execute(pl.MakeTarget(self.repo, "help", self.tmp)).status,
                         pl.OK)
        missing = invoker.execute(pl.MakeTarget(self.repo, "nope", self.tmp))
        self.assertEqual(missing.status, pl.SKIP)
        self.assertIn("nope", missing.detail)

    def test_latest_skipped_when_not_checked_out(self):
        repo = pl.RepoSpec("missing", "u", "r", self.tmp)
        outcome = pl.LatestSubmodule(repo, self.tmp).run(pl.EventBus())
        self.assertEqual(outcome.status, pl.SKIP)

    def test_missing_program_does_not_raise(self):
        res = pl.run(["definitely-not-a-program-xyz"])
        self.assertFalse(res.ok)
        self.assertEqual(res.returncode, 127)


class FactoryTest(unittest.TestCase):
    def test_all_five_repos_in_catalogue(self):
        names = sorted(n for n, _, _ in pl.CATALOGUE)
        self.assertEqual(names, sorted(["podman", "container-libs", "skopeo",
                                        "buildah", "podman-machine-os"]))

    def test_selection_keeps_catalogue_order(self):
        repos = pl.RepoFactory("/x").build(pl.CATALOGUE, ["skopeo", "podman"])
        self.assertEqual([r.name for r in repos], ["podman", "skopeo"])

    def test_unknown_name_raises(self):
        self.assertRaises(ValueError, pl.RepoFactory("/x").build, pl.CATALOGUE, ["nope"])

    def test_command_name(self):
        self.assertEqual(pl.command_name(["x", "pin"], {"pin": 1}), "pin")
        self.assertIsNone(pl.command_name(["x", "bogus"], {"pin": 1}))
        self.assertIsNone(pl.command_name(["x"], {"pin": 1}))


if __name__ == "__main__":
    unittest.main()
