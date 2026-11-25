# -*- coding: utf-8 -*-
"""Tests for the OCI runtime part (no root and no runtime needed). Run: python test_oci.py"""
from __future__ import absolute_import, print_function

import json
import os
import shutil
import stat
import tempfile
import unittest

import podmanlib as pl


class RuntimeChoiceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def make_tool(self, name):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, stat.S_IRWXU)
        return path

    def test_crun_is_preferred_over_runc(self):
        self.make_tool("runc")
        crun = self.make_tool("crun")
        self.assertEqual(pl.find_oci_runtime(self.dir), crun)

    def test_runc_is_the_fallback(self):
        runc = self.make_tool("runc")
        self.assertEqual(pl.find_oci_runtime(self.dir), runc)

    def test_no_runtime(self):
        self.assertIsNone(pl.find_oci_runtime(self.dir))


class SpecTest(unittest.TestCase):
    def test_spec_has_the_process_and_the_namespaces(self):
        spec = pl.make_spec(["uname", "-m"], cwd="/tmp")
        self.assertEqual(spec["process"]["args"], ["uname", "-m"])
        self.assertEqual(spec["process"]["cwd"], "/tmp")
        self.assertEqual(spec["root"]["path"], "rootfs")
        self.assertEqual([ns["type"] for ns in spec["linux"]["namespaces"]],
                         ["pid", "ipc", "uts", "mount"])
        self.assertIn("CAP_CHOWN", spec["process"]["capabilities"]["bounding"])
        self.assertTrue(spec["process"]["noNewPrivileges"])

    def test_bundle_is_written_as_json(self):
        bundle = tempfile.mkdtemp()
        try:
            pl.write_bundle(bundle, pl.make_spec(["true"]))
            with open(os.path.join(bundle, "config.json")) as fh:
                self.assertEqual(json.load(fh)["process"]["args"], ["true"])
        finally:
            shutil.rmtree(bundle)

    def test_missing_runtime_is_an_error(self):
        saved = os.environ.get("PATH")
        os.environ["PATH"] = tempfile.mkdtemp()
        try:
            with self.assertRaises(pl.OCIRuntimeError):
                pl.OCIRuntime()
        finally:
            os.environ["PATH"] = saved


if __name__ == "__main__":
    unittest.main()
