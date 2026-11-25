# -*- coding: utf-8 -*-
"""Tests for podmanlib. Run: python test_podmanlib.py"""
from __future__ import absolute_import, print_function

import io
import json
import os
import shutil
import tempfile
import unittest

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
        self.assertEqual(mod.go_version, "1.24.0")
        self.assertEqual(mod.requires["go.podman.io/common"],
                         "v0.70.1-0.20261116084638-6da6e80dc141")
        self.assertEqual(mod.requires["github.com/x/y"], "v1.2.3")
        self.assertEqual(mod.requires["github.com/single/line"], "v0.1.0")
        self.assertEqual(len(mod.requires), 3)
        self.assertEqual(mod.replaces["github.com/a/b"], "../local/b")
        self.assertEqual(mod.replaces["github.com/c/d"], "github.com/e/f v2.0.0")


class DigestPortTest(unittest.TestCase):
    def test_valid_sha256(self):
        d = "sha256:" + "a" * 64
        self.assertEqual(pl.digest_parse(d), d)

    def test_length_format_and_algorithm_errors(self):
        cases = [
            ("sha256:" + "a" * 63, "invalid checksum digest length"),
            ("sha256:" + "Z" * 64, "invalid checksum digest format"),
            ("md5:" + "a" * 32, "unsupported digest algorithm"),
            (":" + "a" * 64, "invalid checksum digest format"),
            ("sha256:", "invalid checksum digest format"),
        ]
        for value, message in cases:
            with self.assertRaises(pl.DigestError) as cm:
                pl.digest_parse(value)
            self.assertEqual(str(cm.exception), message)


class ReferencePortTest(unittest.TestCase):
    def test_familiar_and_normalized_names(self):
        named = pl.ParseNormalizedNamed("alpine:3.20")
        self.assertEqual(named.String(), "docker.io/library/alpine:3.20")
        self.assertEqual(pl.FamiliarString(named), "alpine:3.20")
        self.assertEqual(pl.FamiliarName(named), "alpine")
        self.assertEqual(pl.Domain(named), "docker.io")
        self.assertEqual(pl.Path(named), "library/alpine")

    def test_names_must_be_lowercase(self):
        with self.assertRaises(pl.RefError) as cm:
            pl.ParseNormalizedNamed("Ubuntu")
        self.assertEqual(str(cm.exception),
                         "invalid reference format: repository name must be lowercase")

    def test_domain_and_digest_reference(self):
        text = "localhost:5000/a/b@sha256:" + "a" * 64
        ref = pl.Parse(text)
        self.assertIsInstance(ref, pl.CanonicalReference)
        self.assertEqual(pl.Domain(ref), "localhost:5000")
        self.assertEqual(pl.Path(ref), "a/b")
        self.assertEqual(ref.String(), text)

    def test_docker_ref_keeps_only_the_digest(self):
        text = "busybox:latest@sha256:" + "a" * 64
        self.assertEqual(pl.ParseDockerRef(text).String(),
                         "docker.io/library/busybox@sha256:" + "a" * 64)

    def test_tag_name_only_adds_latest(self):
        named = pl.ParseNormalizedNamed("redis")
        self.assertEqual(pl.TagNameOnly(named).String(), "docker.io/library/redis:latest")
        self.assertTrue(pl.IsNameOnly(named))
        self.assertFalse(pl.IsNameOnly(pl.Parse("redis:7")))

    def test_identifier_and_any_reference(self):
        self.assertTrue(pl.IsFullIdentifier("a" * 64))
        self.assertFalse(pl.IsFullIdentifier("a" * 63))
        any_ref = pl.ParseAnyReference("sha256:" + "a" * 64)
        self.assertIsInstance(any_ref, pl.digestReference)

    def test_parse_named_requires_canonical_form(self):
        self.assertEqual(pl.ParseNamed("docker.io/library/alpine").String(),
                         "docker.io/library/alpine")
        with self.assertRaises(pl.RefError):
            pl.ParseNamed("alpine")  # canonical is docker.io/library/alpine


class MachineConnectionsTest(unittest.TestCase):
    def test_ssh_urls_like_go(self):
        self.assertEqual(pl.make_ssh_url("127.0.0.1", "/run/user/1000/podman/podman.sock",
                                         "2222", "core"),
                         "ssh://core@127.0.0.1:2222/run/user/1000/podman/podman.sock")
        self.assertEqual(pl.make_ssh_url("127.0.0.1", "/run/podman/podman.sock", "", "root"),
                         "ssh://root@127.0.0.1/run/podman/podman.sock")
        self.assertEqual(pl.make_ssh_url("::1", "/x", "22", "root"), "ssh://root@[::1]:22/x")
        self.assertEqual(pl.make_ssh_url("127.0.0.1", "/a b", "1", "us er"),
                         "ssh://us%20er@127.0.0.1:1/a%20b")
        self.assertEqual(pl.make_ssh_url("127.0.0.1", "/x", "1", "a+b,c;d=e$f&g"),
                         "ssh://a+b,c;d=e$f&g@127.0.0.1:1/x")

    def test_create_connections_pair(self):
        pair = pl.createConnections("dev", 1000, 2222, "core")
        self.assertEqual([name for name, _ in pair], ["dev", "dev-root"])
        self.assertEqual(pair[1][1], "ssh://root@127.0.0.1:2222/run/podman/podman.sock")

    def test_addssh_sets_default_and_pair(self):
        cfg = pl.ConnectionsFile()
        pl.AddSSHConnectionsToPodmanSocket(cfg, 1000, 2222, "/key", "dev", "core", False, True)
        self.assertEqual(cfg.default, "dev")
        self.assertEqual(sorted(cfg.connections), ["dev", "dev-root"])
        self.assertTrue(cfg.connections["dev"]["IsMachine"])
        self.assertEqual(cfg.connections["dev"]["Identity"], "/key")

    def test_duplicate_and_missing_identity_are_errors(self):
        text = pl.edit_connections_text(
            "{}", lambda cfg: pl.AddSSHConnectionsToPodmanSocket(
                cfg, 1000, 2222, "/key", "dev", "core", False, True))
        with self.assertRaises(pl.MachineConnectionError) as cm:
            pl.edit_connections_text(text, lambda cfg: pl.AddSSHConnectionsToPodmanSocket(
                cfg, 1000, 2222, "/key", "dev", "core", False, False))
        self.assertEqual(str(cm.exception), 'cannot overwrite connection "dev"')
        with self.assertRaises(pl.MachineConnectionError) as cm:
            pl.edit_connections_text("{}", lambda cfg: pl.addConnection(
                cfg, [("x", "ssh://x")], "", False))
        self.assertEqual(str(cm.exception), "identity must be defined")

    def test_remove_prefers_machine_default(self):
        cfg = pl.ConnectionsFile()
        pl.addConnection(cfg, [("a", "ssh://a")], "/k", False)
        pl.addConnection(cfg, [("podman-machine-default", "ssh://m")], "/k", False)
        pl.addConnection(cfg, [("z", "ssh://z")], "/k", False)
        self.assertFalse(pl.removal_is_ambiguous(cfg, ["z"]))
        pl.RemoveConnections(cfg, {}, ["z"])
        self.assertEqual(cfg.default, pl.DEFAULT_MACHINE_NAME)

    def test_ambiguous_removal_is_flagged(self):
        cfg = pl.ConnectionsFile()
        pl.addConnection(cfg, [("a", "ssh://a")], "/k", True)
        pl.addConnection(cfg, [("b", "ssh://b")], "/k", False)
        pl.addConnection(cfg, [("c", "ssh://c")], "/k", False)
        self.assertTrue(pl.removal_is_ambiguous(cfg, ["a"]))
        self.assertFalse(pl.removal_is_ambiguous(cfg, ["nope"]))

    def test_removing_all_leaves_go_shaped_file(self):
        text = pl.edit_connections_text("{}", lambda cfg: pl.addConnection(
            cfg, [("only", "ssh://o")], "/k", True))
        text = pl.edit_connections_text(text, lambda cfg: pl.RemoveConnections(cfg, {}, ["only"]))
        self.assertEqual(text, '{"Connection":{},"Farm":{}}\n')

    def test_unknown_keys_survive_an_edit(self):
        text = ('{"Connection":{"Connections":{"x":{"URI":"ssh://x","TLSCert":"/c.pem"}}},'
                '"Farm":{"Default":"f"}}')
        new = pl.edit_connections_text(text, lambda cfg: pl.UpdateConnectionPairPort(
            cfg, "dev", 22, 1000, "core", "/k"))
        self.assertEqual(pl.connections_from_text(new).raw["Connection"]["Connections"]["x"]
                         ["TLSCert"], "/c.pem")
        self.assertEqual(pl.connections_from_text(new).raw["Farm"]["Default"], "f")

    def test_failed_callback_writes_nothing(self):
        def bad(cfg):
            pl.addConnection(cfg, [("n", "ssh://n")], "/k", True)
            raise pl.MachineConnectionError("stop")
        with self.assertRaises(pl.MachineConnectionError):
            pl.edit_connections_text("{}", bad)

    def test_get_connection_and_read_only_destinations(self):
        cfg = pl.connections_from_text('{"Connection":{"Default":"dev","Connections":'
                                       '{"dev":{"URI":"ssh://d","IsMachine":true}}}}')
        got = pl.GetConnection(cfg, "", True, service_destinations={})
        self.assertEqual(got["name"], "dev")
        ro = pl.GetConnection(cfg, "ro", False, service_destinations={"ro": {"URI": "tcp://x"}})
        self.assertFalse(ro["read_write"])
        self.assertFalse(ro["default"])
        with self.assertRaises(pl.MachineConnectionError):
            pl.GetConnection(cfg, "missing", False)

    def test_file_is_written_and_read_back(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "sub", "podman-connections.json")
            pl.EditConnectionConfig(lambda cfg: pl.addConnection(cfg, [("d", "ssh://d")], "/k", True),
                                    path=path)
            with io.open(path, encoding="utf-8") as fh:
                self.assertEqual(json.loads(fh.read())["Connection"]["Default"], "d")
            self.assertTrue(os.path.exists(path + ".lock"))
        finally:
            shutil.rmtree(tmp)

    def test_config_path_follows_go_order(self):
        saved = dict(os.environ)
        try:
            os.environ["PODMAN_CONNECTIONS_CONF"] = "/explicit.json"
            self.assertEqual(pl.connections_config_path(), "/explicit.json")
            del os.environ["PODMAN_CONNECTIONS_CONF"]
            os.environ["XDG_CONFIG_HOME"] = "/xdg"
            self.assertEqual(pl.connections_config_path(),
                             os.path.join("/xdg", "containers", "podman-connections.json"))
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
