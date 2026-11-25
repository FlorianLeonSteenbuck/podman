# -*- coding: utf-8 -*-
"""Tests for the remote commands of podman.py, against a fake libpod service.

The fake service listens on a unix socket in a thread and answers the paths the
commands use. Run: python test_engine.py
"""
from __future__ import absolute_import, print_function

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

try:  # Python 3
    import socketserver
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import unquote
except ImportError:  # Python 2
    import SocketServer as socketserver
    from BaseHTTPServer import BaseHTTPRequestHandler
    from urllib import unquote

HERE = os.path.dirname(os.path.abspath(__file__))

CONTAINERS = [{"Id": "9f8e7d6c5b4a3210aabbccddeeff0011223344556677889900aabbccddeeff00",
               "Names": ["web"], "Image": "docker.io/library/alpine:3.20",
               "Command": ["/bin/sh"], "Created": 1700000000, "Status": "Up 2 minutes",
               "State": "running", "Ports": "", "PodName": ""}]
IMAGES = [{"Id": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
           "RepoTags": ["docker.io/library/alpine:3.20"], "Created": 1700000000, "Size": 7800000}]


def frame(stream, payload):
    """One log frame as the server writes it for a container without a tty."""
    return bytes(bytearray([stream, 0, 0, 0, (len(payload) >> 24) & 255, (len(payload) >> 16) & 255,
                            (len(payload) >> 8) & 255, len(payload) & 255])) + payload


class FakeLibpod(BaseHTTPRequestHandler):
    created = []
    removed = []

    def log_message(self, *args):
        pass

    def send(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def json(self, status, obj):
        self.send(status, json.dumps(obj).encode("utf-8"))

    def route(self):
        path, _, query = self.path.partition("?")
        return unquote(path), query

    def do_GET(self):
        path, query = self.route()
        if path == "/_ping":
            return self.send(200, b"OK", "text/plain")
        if path == "/libpod/version":
            return self.json(200, {"Version": "4.9.3", "ApiVersion": "4.9.0"})
        if path == "/libpod/containers/json":
            return self.json(200, CONTAINERS if "all=true" in query else [])
        if path == "/libpod/images/json":
            return self.json(200, IMAGES)
        if path == "/libpod/images/alpine:3.20/exists":
            return self.send(204)
        if path == "/libpod/images/missing/exists":
            return self.send(404)
        if path == "/libpod/containers/web/json":
            return self.json(200, {"Id": CONTAINERS[0]["Id"], "Name": "web", "State": {"Status": "running"}})
        if path == "/libpod/containers/run0001/logs":
            return self.send(200, frame(1, b"Linux\n"), "application/vnd.docker.raw-stream")
        if path == "/libpod/containers/web/logs":
            return self.send(200, frame(1, b"hello from web\n") + frame(2, b"warning\n"),
                             "application/vnd.docker.raw-stream")
        if path.startswith("/libpod/containers/") or path.startswith("/libpod/images/"):
            return self.json(404, {"cause": "no such object", "message": "no such object: %s" % path,
                                   "response": 404})
        return self.json(404, {"message": "not found"})

    def do_POST(self):
        path, query = self.route()
        if path == "/libpod/containers/create":
            length = int(self.headers.get("Content-Length", "0"))
            spec = json.loads(self.rfile.read(length).decode("utf-8"))
            FakeLibpod.created.append(spec)
            return self.json(201, {"Id": "run0001", "Warnings": []})
        if path == "/libpod/containers/run0001/wait":
            return self.json(200, 3)
        if path == "/libpod/images/pull":
            return self.json(200, {"Images": [IMAGES[0]["Id"]], "ID": IMAGES[0]["Id"]})
        if path in ("/libpod/containers/run0001/start",):
            return self.send(204)
        if path in ("/libpod/containers/web/start", "/libpod/containers/web/stop",
                    "/libpod/containers/web/restart"):
            return self.send(204)
        return self.json(404, {"message": "not found"})

    def do_DELETE(self):
        path, query = self.route()
        if path == "/libpod/containers/web":
            return self.send(200 if "force=true" in query else 409, b"{}")
        if path == "/libpod/containers/run0001":
            FakeLibpod.removed.append(path)
            return self.send(200, b"{}")
        if path == "/libpod/images/alpine:3.20":
            return self.send(200, b"{}")
        return self.json(404, {"message": "not found"})


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock = os.path.join(self.tmp, "podman.sock")
        self.server = UnixServer(self.sock, FakeLibpod)
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

    def run_podman(self, args, env=None):
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "podman.py")] + list(args),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env or self.env)
        out, err = proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def test_ps_lists_running_and_all(self):
        code, out, _ = self.run_podman(["ps"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)  # header only: nothing is running
        code, out, _ = self.run_podman(["ps", "-a"])
        self.assertEqual(code, 0)
        self.assertIn("alpine:3.20", out)
        self.assertIn("web", out)

    def test_ps_with_format_template(self):
        code, out, _ = self.run_podman(["ps", "-a", "--format", "{{.Names}}\\t{{.Image}}\\t{{.Status}}"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "web\tdocker.io/library/alpine:3.20\tUp 2 minutes")

    def test_images_and_version(self):
        code, out, _ = self.run_podman(["images", "--format", "{{.Repository}}:{{.Tag}}"])
        self.assertEqual((code, out.strip()), (0, "docker.io/library/alpine:3.20"))
        code, out, _ = self.run_podman(["version", "--format", "{{.Server.Version}}"])
        self.assertEqual((code, out.strip()), (0, "4.9.3"))

    def test_pull_prints_the_image_id(self):
        code, out, _ = self.run_podman(["pull", "-q", "alpine:3.20"])
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().startswith("abcdef1234"))

    def test_image_exists_and_inspect(self):
        self.assertEqual(self.run_podman(["image", "exists", "alpine:3.20"])[0], 0)
        self.assertEqual(self.run_podman(["image", "exists", "missing"])[0], 1)
        code, out, _ = self.run_podman(["inspect", "web", "--format", "{{.State.Status}}"])
        self.assertEqual((code, out.strip()), (0, "running"))

    def test_start_stop_rm_and_logs(self):
        code, out, _ = self.run_podman(["start", "web"])
        self.assertEqual((code, out.strip()), (0, "web"))
        self.assertEqual(self.run_podman(["stop", "-t", "5", "web"])[0], 0)
        self.assertEqual(self.run_podman(["rm", "-f", "web"])[0], 0)
        code, out, _ = self.run_podman(["logs", "web"])
        self.assertEqual((code, out), (0, "hello from web\n"))  # stderr frames go to stderr

    def test_server_error_becomes_exit_1_with_message(self):
        code, _, err = self.run_podman(["rm", "web"])  # no force: the fake answers 409
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)

    def test_connection_name_resolves_through_the_connections_file(self):
        conf = os.path.join(self.tmp, "podman-connections.json")
        env = dict(self.env)
        env.pop("CONTAINER_HOST", None)
        env["PODMAN_CONNECTIONS_CONF"] = conf
        code, _, _ = self.run_podman(["system", "connection", "add", "--default", "qbox-t1",
                                      "unix://" + self.sock], env=env)
        self.assertEqual(code, 0)
        code, out, _ = self.run_podman(["-c", "qbox-t1", "images"], env=env)
        self.assertEqual(code, 0)
        self.assertIn("alpine", out)
        code, out, _ = self.run_podman(["--remote", "ps", "-a"], env=env)
        self.assertEqual(code, 0)
        self.assertIn("web", out)
        code, out, _ = self.run_podman(["system", "connection", "list"], env=env)
        self.assertIn("qbox-t1", out)
        self.assertEqual(self.run_podman(["system", "connection", "rm", "qbox-t1"], env=env)[0], 0)
        self.assertEqual(self.run_podman(["system", "connection", "rm", "qbox-t1"], env=env)[0], 1)

    def test_run_creates_starts_waits_and_prints_logs(self):
        FakeLibpod.created = []
        FakeLibpod.removed = []
        code, out, _ = self.run_podman(["run", "--rm", "-e", "A=1", "-p", "8080:80/tcp",
                                        "-v", "/data:/srv:ro", "--name", "job",
                                        "docker.io/library/alpine", "uname", "-m"])
        self.assertEqual(code, 3)  # the fake waits with exit code 3
        self.assertEqual(out, "Linux\n")
        spec = FakeLibpod.created[-1]
        self.assertEqual(spec["image"], "docker.io/library/alpine")
        self.assertEqual(spec["command"], ["uname", "-m"])
        self.assertEqual(spec["env"], {"A": "1"})
        self.assertEqual(spec["portmappings"], [{"host_port": 8080, "container_port": 80,
                                                 "protocol": "tcp"}])
        self.assertEqual(spec["mounts"], [{"type": "bind", "source": "/data",
                                            "destination": "/srv", "options": ["ro"]}])
        self.assertEqual(spec["name"], "job")
        self.assertEqual(FakeLibpod.removed, ["/libpod/containers/run0001"])

    def test_run_detached_prints_the_id(self):
        code, out, _ = self.run_podman(["run", "-d", "docker.io/library/alpine", "sleep", "9"])
        self.assertEqual((code, out.strip()), (0, "run0001"))

    def test_no_endpoint_means_not_implemented(self):
        env = dict(self.env)
        env.pop("CONTAINER_HOST", None)
        code, _, err = self.run_podman(["ps"], env=env)
        self.assertEqual(code, 3)
        self.assertIn("not implemented", err)


if __name__ == "__main__":
    unittest.main()
