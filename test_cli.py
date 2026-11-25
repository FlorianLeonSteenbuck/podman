# -*- coding: utf-8 -*-
"""Tests for the command line programs. Run: python test_cli.py"""
from __future__ import absolute_import, print_function

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def run_cli(script, args, env=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, script)] + list(args),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=merged)
    out, err = proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class PodmanCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, obj):
        path = os.path.join(self.tmp, name)
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(obj if isinstance(obj, type(u"")) else json.dumps(obj))
        return path

    def test_version_and_image_parse(self):
        code, out, _ = run_cli("podman.py", ["version"])
        self.assertEqual(code, 0)
        self.assertIn("podman version", out)
        code, out, _ = run_cli("podman.py", ["image", "parse", "alpine:3.20"])
        self.assertEqual(code, 0)
        self.assertIn("docker.io/library/alpine:3.20", out)

    def test_manifest_choose_from_oci_index(self):
        index = self.write("index.json", {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                 "digest": "sha256:amd", "size": 1,
                 "platform": {"architecture": "amd64", "os": "linux"}},
                {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                 "digest": "sha256:arm", "size": 1,
                 "platform": {"architecture": "arm64", "os": "linux", "variant": "v8"}},
            ]})
        code, out, _ = run_cli("podman.py", ["manifest", "choose", index,
                                             "--arch", "arm64", "--os", "linux"])
        self.assertEqual((code, out.strip()), (0, "sha256:arm"))
        code, out, _ = run_cli("podman.py", ["manifest", "choose", index,
                                             "--arch=amd64", "--os=linux"])
        self.assertEqual((code, out.strip()), (0, "sha256:amd"))

    def test_manifest_guess_and_digest(self):
        blob = self.write("m.json", {"schemaVersion": 2,
                                     "mediaType": "application/vnd.docker.distribution.manifest.v2+json"})
        code, out, _ = run_cli("podman.py", ["manifest", "guess", blob])
        self.assertEqual((code, out.strip()), (0, "application/vnd.docker.distribution.manifest.v2+json"))
        code, out, _ = run_cli("podman.py", ["manifest", "digest", blob])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("sha256:"))

    def test_usage_and_not_implemented_exit_codes(self):
        self.assertEqual(run_cli("podman.py", ["bogus"])[0], 2)
        self.assertEqual(run_cli("podman.py", ["manifest", "choose"])[0], 2)
        self.assertEqual(run_cli("podman.py", ["manifest", "choose", "x", "--nope"])[0], 2)
        code, _, err = run_cli("podman.py", ["pull", "alpine"])
        self.assertEqual(code, 3)
        self.assertIn("not implemented", err)

    def test_manifest_choose_rejects_a_non_index(self):
        blob = self.write("m2.json", {"schemaVersion": 2,
                                      "mediaType": "application/vnd.docker.distribution.manifest.v2+json"})
        code, _, err = run_cli("podman.py", ["manifest", "choose", blob])
        self.assertEqual(code, 1)
        self.assertIn("not an image index", err)

    def test_machine_connection_through_podman(self):
        conf = os.path.join(self.tmp, "podman-connections.json")
        env = {"PODMAN_CONNECTIONS_CONF": conf, "PODMAN_DEFAULT": "1"}
        code, _, _ = run_cli("podman.py", ["machine", "connection", "add-pair", "dev",
                                           "1000", "2222", "core", "/k"], env=env)
        self.assertEqual(code, 0)
        code, out, _ = run_cli("podman.py", ["machine", "connection", "list"], env=env)
        self.assertEqual(code, 0)
        self.assertIn("dev-root", out)


class OtherProgramsTest(unittest.TestCase):
    def test_skopeo_buildah_machine_os_version_and_not_implemented(self):
        for script, name in (("skopeo.py", "skopeo"), ("buildah.py", "buildah"),
                             ("podman_machine_os.py", "podman-machine-os")):
            code, out, _ = run_cli(script, ["version"])
            self.assertEqual(code, 0, script)
            self.assertIn(name, out)
        self.assertEqual(run_cli("skopeo.py", ["copy", "a", "b"])[0], 3)
        self.assertEqual(run_cli("buildah.py", ["build", "."])[0], 3)
        self.assertEqual(run_cli("podman_machine_os.py", ["verify"])[0], 3)


if __name__ == "__main__":
    unittest.main()
