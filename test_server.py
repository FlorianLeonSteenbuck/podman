# -*- coding: utf-8 -*-
"""Tests for the engine server (part 10) with the real client. Uses a fake OCI runtime that
runs the process on the host, so no root and no crun are needed. Run: python test_server.py"""
from __future__ import absolute_import, print_function

import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

import podmanlib as pl

HERE = os.path.dirname(os.path.abspath(__file__))

FAKE_RUNTIME = """#!%(python)s
import json, os, subprocess, sys
args = sys.argv[1:]
if args and args[0] == "run":
    bundle = args[args.index("--bundle") + 1]
    with open(os.path.join(bundle, "config.json")) as fh:
        proc = json.load(fh)["process"]
    env = dict(item.split("=", 1) for item in proc["env"] if "=" in item)
    cwd = proc.get("cwd") or "/"
    if not os.path.isdir(cwd):
        cwd = "/"
    done = subprocess.run(proc["args"], env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sys.stdout.write(done.stdout.decode("utf-8", "replace"))
    sys.stderr.write(done.stderr.decode("utf-8", "replace"))
    sys.exit(done.returncode)
sys.exit(0)
"""


class EngineServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.runtime = os.path.join(self.tmp, "fake-crun")
        with io.open(self.runtime, "w", encoding="utf-8") as fh:
            fh.write(FAKE_RUNTIME % {"python": sys.executable})
        os.chmod(self.runtime, stat.S_IRWXU)
        rootfs = os.path.join(self.tmp, "rootfs")
        os.makedirs(rootfs)
        self.engine = pl.EngineServer(self.home, runtime_path=self.runtime)
        self.engine.image_import("alpine:3.20", rootfs)
        self.sock = os.path.join(self.tmp, "api.sock")
        self.server = pl.EngineUnixServer(self.sock, self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.env = dict(os.environ)
        self.env["CONTAINER_HOST"] = "unix://" + self.sock
        self.env.pop("PODMAN_CONNECTIONS_CONF", None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp)

    def podman(self, args):
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "podman.py")] + list(args),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        out, err = proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def test_version_comes_from_the_server(self):
        code, out, _ = self.podman(["version", "--format", "{{.Server.Version}}"])
        self.assertEqual((code, out.strip()), (0, pl.ENGINE_VERSION))

    def test_run_passes_output_env_and_exit_status(self):
        code, out, err = self.podman(["run", "--rm", "-e", "A=1", "docker.io/library/alpine:3.20",
                                      "sh", "-c", "echo $A; echo oops >&2; exit 4"])
        self.assertEqual(code, 4)
        self.assertEqual(out, "1\n")
        self.assertIn("oops", err)

    def test_rm_removes_the_container(self):
        self.podman(["run", "--rm", "docker.io/library/alpine:3.20", "true"])
        code, out, _ = self.podman(["ps", "-a"])
        self.assertEqual((code, len(out.strip().splitlines())), (0, 1))  # header only

    def test_detached_container_logs_and_status(self):
        code, out, _ = self.podman(["run", "-d", "--name", "job", "docker.io/library/alpine:3.20",
                                    "sh", "-c", "echo hi"])
        self.assertEqual(code, 0)
        self.engine.wait("job")
        code, out, _ = self.podman(["logs", "job"])
        self.assertEqual((code, out), (0, "hi\n"))
        code, out, _ = self.podman(["inspect", "job", "--format", "{{.State.Status}} {{.State.ExitCode}}"])
        self.assertEqual((code, out.strip()), (0, "exited 0"))

    def test_pull_and_unknown_image_are_clear_errors(self):
        code, _, err = self.podman(["pull", "alpine:3.20"])
        self.assertEqual(code, 1)
        self.assertIn("no image pull yet", err)
        code, _, err = self.podman(["run", "docker.io/library/nothere:1", "true"])
        self.assertEqual(code, 1)
        self.assertIn("image not known", err)

    def test_images_listed_and_exists(self):
        code, out, _ = self.podman(["image", "exists", "alpine:3.20"])
        self.assertEqual(code, 0)
        code, out, _ = self.podman(["images", "--format", "{{.Repository}}:{{.Tag}}"])
        self.assertEqual(out.strip(), "docker.io/library/alpine:3.20")

    def test_state_is_read_back_after_a_restart(self):
        self.engine.create({"image": "alpine:3.20", "command": ["true"], "name": "kept"})
        again = pl.EngineServer(self.home, runtime_path=self.runtime)
        self.assertIn("kept", [c["name"] for c in again.state["containers"].values()])

    def test_running_container_is_reported_exited_after_restart(self):
        cid, _ = self.engine.create({"image": "alpine:3.20", "command": ["sleep", "9"], "name": "lost"})
        self.engine.state["containers"][cid]["status"] = "running"
        self.engine._save()
        again = pl.EngineServer(self.home, runtime_path=self.runtime)
        self.assertEqual(again.state["containers"][cid]["status"], "exited")


if __name__ == "__main__":
    unittest.main()
