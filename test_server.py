# -*- coding: utf-8 -*-
"""Tests for the engine server (part 10) with the real client. Uses a fake OCI runtime that
runs the process on the host, so no root and no crun are needed. Run: python test_server.py"""
from __future__ import absolute_import, print_function

import io
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import podmanlib as pl

HERE = os.path.dirname(os.path.abspath(__file__))

FAKE_RUNTIME = """#!%(python)s
import json, os, signal, subprocess, sys
STATE = %(state)r
args = sys.argv[1:]


def pidfile(cid):
    return os.path.join(STATE, cid + ".pid")


if args and args[0] == "run":
    bundle = args[args.index("--bundle") + 1]
    cid = args[-1]
    with open(os.path.join(bundle, "config.json")) as fh:
        proc = json.load(fh)["process"]
    env = dict(item.split("=", 1) for item in proc["env"] if "=" in item)
    cwd = proc.get("cwd") or "/"
    if not os.path.isdir(cwd):
        cwd = "/"
    child = subprocess.Popen(proc["args"], env=env, cwd=cwd)
    with open(pidfile(cid), "w") as fh:
        fh.write(str(child.pid))
    code = child.wait()
    if os.path.exists(pidfile(cid)):
        os.remove(pidfile(cid))
    sys.exit(128 - code if code < 0 else code)
if args and args[0] == "kill":
    try:
        with open(pidfile(args[1])) as fh:
            pid = int(fh.read())
    except (IOError, OSError, ValueError):
        sys.stderr.write("container %%s is not running\\n" %% args[1])
        sys.exit(1)
    os.kill(pid, getattr(signal, "SIG" + args[2]))
    sys.exit(0)
sys.exit(0)
"""

ECHO_SERVER = """
import socket
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", %d))
s.listen(5)
while True:
    c, _ = s.accept()
    data = c.recv(100)
    c.sendall(data.upper())
    c.close()
"""

COUNTER = """
import os, sys
path = %r
n = int(open(path).read()) if os.path.exists(path) else 0
open(path, "w").write(str(n + 1))
sys.exit(1 if n < %d else 0)
"""

SLEEP_LONG = "import time; time.sleep(30)"


class EngineServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.state_dir = os.path.join(self.tmp, "fake-state")
        os.makedirs(self.state_dir)
        self.runtime = os.path.join(self.tmp, "fake-crun")
        with io.open(self.runtime, "w", encoding="utf-8") as fh:
            fh.write(FAKE_RUNTIME % {"python": sys.executable, "state": self.state_dir})
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
        # stop the processes of the fake runtime that are still running
        for name in os.listdir(self.state_dir):
            if name.endswith(".pid"):
                try:
                    with open(os.path.join(self.state_dir, name)) as fh:
                        os.kill(int(fh.read()), signal.SIGKILL)
                except (IOError, OSError, ValueError):
                    pass
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp)

    def podman(self, args):
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "podman.py")] + list(args),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        out, err = proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def client(self):
        return pl.EngineClient(self.sock)

    def wait_for(self, condition, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def free_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def echo(self, port, data):
        """Sends data to the published port. None if nothing listens there."""
        try:
            conn = socket.create_connection(("127.0.0.1", port), 2)
        except socket.error:
            return None
        try:
            conn.sendall(data)
            conn.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        except socket.error:
            return None
        finally:
            conn.close()

    def create(self, **spec):
        base = {"image": "alpine:3.20", "command": ["true"]}
        base.update(spec)
        cid, _ = self.engine.create(base)
        return cid

    def record(self, cid):
        return self.engine.state["containers"][cid]

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

    # --- networking ------------------------------------------------------------

    def test_network_none_gets_its_own_namespace_and_takes_no_ports(self):
        cid = self.create(name="isolated", network="none")
        with open(os.path.join(self.record(cid)["bundle"], "config.json")) as fh:
            namespaces = json.load(fh)["linux"]["namespaces"]
        self.assertIn({"type": "network"}, namespaces)
        with self.assertRaises(pl.ServerError) as ctx:
            self.create(name="nope", network="none", portmappings=[{"host_port": 9, "container_port": 9}])
        self.assertEqual(ctx.exception.status, 400)

    def test_unknown_network_modes_are_not_implemented(self):
        with self.assertRaises(pl.ServerError) as ctx:
            self.create(network="bridge")
        self.assertEqual(ctx.exception.status, 501)

    def test_host_networking_is_the_default_and_is_reported(self):
        cid = self.create(name="plain")
        self.assertEqual(self.engine.container_json(self.record(cid))["HostConfig"]["NetworkMode"], "host")
        with open(os.path.join(self.record(cid)["bundle"], "config.json")) as fh:
            namespaces = json.load(fh)["linux"]["namespaces"]
        self.assertNotIn({"type": "network"}, namespaces)

    def test_published_port_forwards_to_the_container_and_closes_on_stop(self):
        host, inner = self.free_port(), self.free_port()
        self.create(name="web", command=[sys.executable, "-c", ECHO_SERVER % inner],
                    portmappings=[{"host_port": host, "container_port": inner}])
        self.engine.start("web")
        self.assertTrue(self.wait_for(lambda: self.echo(host, b"ping") == b"PING"))
        self.engine.stop("web", 2)
        self.assertIsNone(self.echo(host, b"ping"))

    def test_a_taken_host_port_is_an_error_and_releases_nothing_else(self):
        host, inner = self.free_port(), self.free_port()
        self.create(name="first", command=[sys.executable, "-c", ECHO_SERVER % inner],
                    portmappings=[{"host_port": host, "container_port": inner}])
        self.create(name="second", command=[sys.executable, "-c", ECHO_SERVER % inner],
                    portmappings=[{"host_port": host, "container_port": inner}])
        self.engine.start("first")
        with self.assertRaises(pl.ServerError) as ctx:
            self.engine.start("second")
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(self.record(self.engine._container("second")["id"])["status"], "created")
        self.engine.stop("first", 2)

    def test_publish_on_the_command_line(self):
        host, inner = self.free_port(), self.free_port()
        code, out, err = self.podman(["run", "-d", "--name", "cli-web", "-p",
                                      "%d:%d" % (host, inner), "docker.io/library/alpine:3.20",
                                      sys.executable, "-c", ECHO_SERVER % inner])
        self.assertEqual(code, 0, err)
        self.assertTrue(self.wait_for(lambda: self.echo(host, b"hi") == b"HI"))
        self.engine.stop("cli-web", 2)

    # --- restart policies -----------------------------------------------------

    def test_on_failure_restarts_until_the_command_succeeds(self):
        counter = os.path.join(self.tmp, "count-a")
        cid = self.create(name="flaky", command=[sys.executable, "-c", COUNTER % (counter, 2)],
                          restart_policy="on-failure")
        self.engine.start("flaky")
        self.assertEqual(self.engine.wait(cid), 0)
        self.assertEqual(self.record(cid)["restart_count"], 2)
        self.assertEqual(self.record(cid)["status"], "exited")

    def test_on_failure_with_a_retry_count_gives_up(self):
        counter = os.path.join(self.tmp, "count-b")
        cid = self.create(name="giveup", command=[sys.executable, "-c", COUNTER % (counter, 5)],
                          restart_policy="on-failure:1")
        self.engine.start("giveup")
        self.assertEqual(self.engine.wait(cid), 1)
        self.assertEqual(self.record(cid)["restart_count"], 1)

    def test_always_restarts_until_stopped(self):
        cid = self.create(name="forever", command=[sys.executable, "-c", "import sys; sys.exit(0)"],
                          restart_policy="always")
        self.engine.start("forever")
        self.assertTrue(self.wait_for(lambda: self.record(cid)["restart_count"] >= 2))
        self.engine.stop("forever", 5)
        self.assertEqual(self.record(cid)["status"], "exited")
        count = self.record(cid)["restart_count"]
        time.sleep(0.5)
        self.assertEqual(self.record(cid)["restart_count"], count)

    def test_a_stop_during_restart_backoff_is_honoured(self):
        cid = self.create(name="slow", command=[sys.executable, "-c", "import sys; sys.exit(3)"],
                          restart_policy="always")
        self.engine.start("slow")
        self.assertTrue(self.wait_for(lambda: self.record(cid)["restart_count"] >= 1))
        self.engine.stop("slow", 5)
        self.assertEqual(self.record(cid)["status"], "exited")

    def test_bad_restart_policy_is_rejected(self):
        with self.assertRaises(pl.ServerError) as ctx:
            self.create(restart_policy="sometimes")
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(pl.ServerError):
            self.create(restart_policy="always:3")

    def test_resume_starts_containers_that_were_running_with_a_policy(self):
        cid = self.create(name="svc", command=[sys.executable, "-c", SLEEP_LONG],
                          restart_policy="always")
        self.record(cid).update(status="running", interrupted=True)
        self.engine._save()
        again = pl.EngineServer(self.home, runtime_path=self.runtime)
        self.assertEqual(again.resume(), [])
        self.assertEqual(again.state["containers"][cid]["status"], "running")
        again.stop("svc", 1)

    def test_resume_leaves_stopped_and_policy_less_containers_alone(self):
        stopped = self.create(name="stopped", command=[sys.executable, "-c", SLEEP_LONG],
                              restart_policy="always")
        self.record(stopped).update(status="running", interrupted=True, stop_requested=True)
        plain = self.create(name="plain-job", command=["true"])
        self.record(plain).update(status="running", interrupted=True)
        self.engine._save()
        again = pl.EngineServer(self.home, runtime_path=self.runtime)
        again.resume()
        self.assertEqual(again.state["containers"][stopped]["status"], "exited")
        self.assertEqual(again.state["containers"][plain]["status"], "exited")

    # --- logs and attach ------------------------------------------------------

    def test_logs_follow_streams_output_until_the_container_stops(self):
        script = "import sys, time\nprint('a'); sys.stdout.flush(); time.sleep(0.3)\nprint('b')"
        cid = self.create(name="follow", command=[sys.executable, "-c", script])
        self.engine.start("follow")
        chunks = list(self.client().stream_logs("follow", follow=True))
        out = b"".join(payload for stream, payload in chunks if stream == 1)
        self.assertEqual(out, b"a\nb\n")
        self.assertEqual(self.record(cid)["status"], "exited")

    def test_logs_follow_on_a_stopped_container_returns_the_output_so_far(self):
        self.create(name="done", command=[sys.executable, "-c", "print('x')"])
        self.engine.start("done")
        self.engine.wait("done")
        chunks = list(self.client().stream_logs("done", follow=True))
        self.assertEqual(b"".join(p for s, p in chunks if s == 1), b"x\n")

    def test_attached_run_shows_output_while_it_runs(self):
        code, out, err = self.podman(["run", "--name", "attached", "docker.io/library/alpine:3.20",
                                      sys.executable, "-c", "print('one'); import sys; sys.exit(6)"])
        self.assertEqual(code, 6, err)
        self.assertEqual(out, "one\n")

    def test_logs_follow_on_the_command_line(self):
        self.create(name="cli-follow", command=[sys.executable, "-c", "print('cli')"])
        self.engine.start("cli-follow")
        self.engine.wait("cli-follow")
        code, out, _ = self.podman(["logs", "-f", "cli-follow"])
        self.assertEqual((code, out), (0, "cli\n"))

    # --- pods ----------------------------------------------------------------

    def test_pod_shares_its_ports_and_reports_its_state(self):
        host, inner = self.free_port(), self.free_port()
        pod_id = self.engine.pod_create({"name": "site", "portmappings": [
            {"host_port": host, "container_port": inner}]})
        cid = self.create(name="web", pod="site", command=[sys.executable, "-c", ECHO_SERVER % inner])
        self.assertEqual(self.engine.pod_json(self.engine._pod("site"))["Status"], "Created")
        self.engine.pod_start("site")
        self.assertTrue(self.wait_for(lambda: self.echo(host, b"ok") == b"OK"))
        self.assertEqual(self.engine.pod_json(self.engine._pod("site"))["Status"], "Running")
        self.assertEqual(self.engine.container_json(self.record(cid))["Pod"], pod_id)
        self.engine.pod_stop("site", 2)
        self.assertEqual(self.engine.pod_json(self.engine._pod("site"))["Status"], "Exited")
        self.assertIsNone(self.echo(host, b"ok"))
        removed = self.engine.pod_remove("site", False)
        self.assertEqual(removed, [cid])
        self.assertNotIn(cid, self.engine.state["containers"])
        self.assertNotIn(pod_id, self.engine.state["pods"])

    def test_a_pod_with_a_running_container_needs_force(self):
        self.engine.pod_create({"name": "busy"})
        self.create(name="busy-one", pod="busy", command=[sys.executable, "-c", SLEEP_LONG])
        self.engine.pod_start("busy")
        with self.assertRaises(pl.ServerError) as ctx:
            self.engine.pod_remove("busy", False)
        self.assertEqual(ctx.exception.status, 409)
        self.engine.pod_remove("busy", True)

    def test_containers_in_a_pod_cannot_publish_ports_themselves(self):
        self.engine.pod_create({"name": "rules"})
        with self.assertRaises(pl.ServerError) as ctx:
            self.create(name="x", pod="rules", command=["true"],
                        portmappings=[{"host_port": 9, "container_port": 9}])
        self.assertEqual(ctx.exception.status, 400)

    def test_pod_commands_through_the_cli(self):
        code, out, err = self.podman(["pod", "create", "--name", "cli-pod"])
        self.assertEqual(code, 0, err)
        code, out, _ = self.podman(["pod", "ps", "--format", "{{.Name}} {{.NumberOfContainers}}"])
        self.assertEqual((code, out.strip()), (0, "cli-pod 0"))
        code, out, err = self.podman(["pod", "rm", "cli-pod"])
        self.assertEqual(code, 0, err)
        code, out, _ = self.podman(["pod", "ps"])
        self.assertEqual(len(out.strip().splitlines()), 1)  # header only


if __name__ == "__main__":
    unittest.main()
