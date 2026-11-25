# -*- coding: utf-8 -*-
"""Tests for pulling images (part 11): a fake registry v2 with bearer token auth on
127.0.0.1, layers with whiteouts, index choice, digest checks, and the path rules.
Run: python test_registry.py"""
from __future__ import absolute_import, print_function

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest

try:  # Python 3
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:  # Python 2
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

import podmanlib as pl
from test_server import FAKE_RUNTIME

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "fake-token-1"


def make_layer(entries):
    """entries: (name, kind, payload, mode) with kind file, dir, sym. Returns tar.gz bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, kind, payload, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
            else:
                data = payload.encode("utf-8")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_handler(registry):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body=b"", headers=None, ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.partition("?")[0]
            if path == "/token":
                self._send(200, json.dumps({"token": TOKEN}).encode("utf-8"))
                return
            if not path.startswith("/v2/"):
                self._send(404)
                return
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                challenge = 'Bearer realm="http://127.0.0.1:%d/token",service="fake"' % registry.port
                self._send(401, b"", {"WWW-Authenticate": challenge})
                return
            rest = path[len("/v2/"):]
            if "/manifests/" in rest:
                ref = rest.split("/manifests/", 1)[1]
                if ref not in registry.manifests:
                    self._send(404)
                    return
                media, body = registry.manifests[ref]
                self._send(200, body, {"Docker-Content-Digest": pl.Digest(body)}, ctype=media)
            elif "/blobs/" in rest:
                digest = rest.split("/blobs/", 1)[1]
                if digest not in registry.blobs:
                    self._send(404)
                    return
                body = registry.blobs[digest]
                if digest in registry.corrupt:
                    body = b"tampered"
                self._send(200, body, ctype="application/octet-stream")
            else:
                self._send(404)
    return Handler


class FakeRegistry(object):
    def __init__(self):
        self.blobs = {}
        self.manifests = {}
        self.corrupt = set()
        self.server = HTTPServer(("127.0.0.1", 0), make_handler(self))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    @property
    def host(self):
        return "127.0.0.1:%d" % self.port

    def add_blob(self, data):
        digest = pl.Digest(data)
        self.blobs[digest] = data
        return digest

    def add_manifest(self, refs, media, body):
        digest = pl.Digest(body)
        for ref in refs:
            self.manifests[ref] = (media, body)
        self.manifests[digest] = (media, body)
        return digest

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def image_config(env, cmd, workdir):
    return json.dumps({"architecture": pl.host_goarch(), "os": pl.host_goos(),
                       "config": {"Env": env, "Cmd": cmd, "WorkingDir": workdir}}).encode("utf-8")


class PullTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.registry = FakeRegistry()
        self.engine = pl.EngineServer(self.home)
        # layer 1: a base system with a tool and a directory
        self.layer1 = make_layer([
            ("etc", "dir", None, 0o755),
            ("etc/os-release", "file", "NAME=fake\n", 0o644),
            ("bin", "dir", None, 0o755),
            ("bin/tool", "file", "old tool\n", 0o755),
            ("etc/dir", "dir", None, 0o755),
            ("etc/dir/keep", "file", "keep\n", 0o644),
        ])
        # layer 2: overwrites a file, removes bin/tool, and empties etc/dir (opaque)
        self.layer2 = make_layer([
            ("etc/os-release", "file", "NAME=fake2\n", 0o644),
            ("bin/.wh.tool", "file", "", 0o644),
            ("etc/dir/.wh..wh..opq", "file", "", 0o644),
            ("etc/dir/new", "file", "new\n", 0o644),
        ])
        # layer 3: a link to the root and a file through it; it must land inside the rootfs
        self.layer3 = make_layer([
            ("evil", "sym", "/", 0o777),
            ("evil/pwned", "file", "inside\n", 0o644),
        ])
        self.config = image_config(["FOO=bar"], ["/bin/sh", "-c", "echo hi"], "/etc")
        self.push()

    def tearDown(self):
        self.registry.close()
        shutil.rmtree(self.tmp)

    def push(self):
        blobs = [self.registry.add_blob(self.layer1), self.registry.add_blob(self.layer2),
                 self.registry.add_blob(self.layer3)]
        config_digest = self.registry.add_blob(self.config)
        layers = [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": d,
                   "size": 1} for d in blobs]
        manifest = json.dumps({"schemaVersion": 2, "mediaType": pl.OCI1_MANIFEST_MEDIA_TYPE,
                               "config": {"mediaType": pl.OCI1_CONFIG_MEDIA_TYPE,
                                          "digest": config_digest, "size": len(self.config)},
                               "layers": layers}).encode("utf-8")
        self.single = self.registry.add_manifest(["single"], pl.OCI1_MANIFEST_MEDIA_TYPE, manifest)
        decoy = json.dumps({"schemaVersion": 2, "mediaType": pl.OCI1_MANIFEST_MEDIA_TYPE,
                            "config": {"digest": "sha256:" + "0" * 64, "size": 1},
                            "layers": []}).encode("utf-8")
        decoy_digest = self.registry.add_manifest([], pl.OCI1_MANIFEST_MEDIA_TYPE, decoy)
        index = json.dumps({"schemaVersion": 2, "mediaType": pl.OCI1_INDEX_MEDIA_TYPE, "manifests": [
            {"mediaType": pl.OCI1_MANIFEST_MEDIA_TYPE, "digest": decoy_digest, "size": len(decoy),
             "platform": {"architecture": "s390x", "os": "linux"}},
            {"mediaType": pl.OCI1_MANIFEST_MEDIA_TYPE, "digest": self.single, "size": len(manifest),
             "platform": {"architecture": pl.host_goarch(), "os": pl.host_goos()}},
        ]}).encode("utf-8")
        self.registry.add_manifest(["multi"], pl.OCI1_INDEX_MEDIA_TYPE, index)
        self.config_digest = config_digest

    def rootfs(self, key):
        return self.engine.state["images"][key]["rootfs"]

    def test_pull_chooses_this_machine_and_applies_the_layers(self):
        key, image_id = self.engine.image_pull("%s/library/alpine:multi" % self.registry.host)
        self.assertEqual(key, "%s/library/alpine:multi" % self.registry.host)
        self.assertEqual(image_id, self.config_digest[len("sha256:"):])
        root = self.rootfs(key)
        with io.open(os.path.join(root, "etc", "os-release")) as fh:
            self.assertEqual(fh.read(), "NAME=fake2\n")  # overwritten by layer 2
        self.assertFalse(os.path.lexists(os.path.join(root, "bin", "tool")))  # whiteout
        self.assertFalse(os.path.lexists(os.path.join(root, "etc", "dir", "keep")))  # opaque
        self.assertTrue(os.path.isfile(os.path.join(root, "etc", "dir", "new")))
        # evil is a link to / in the image; the entry under it lands inside the rootfs
        self.assertTrue(os.path.isfile(os.path.join(root, "pwned")))
        self.assertFalse(os.path.lexists("/pwned"))
        record = self.engine.state["images"][key]
        self.assertEqual(record["Env"], ["FOO=bar"])
        self.assertEqual(record["Cmd"], ["/bin/sh", "-c", "echo hi"])
        self.assertEqual(record["WorkingDir"], "/etc")

    def test_a_single_manifest_is_used_as_it_is(self):
        key, _ = self.engine.image_pull("%s/library/alpine:single" % self.registry.host)
        self.assertTrue(os.path.isfile(os.path.join(self.rootfs(key), "etc", "dir", "new")))

    def test_blobs_are_cached_after_the_first_pull(self):
        self.engine.image_pull("%s/library/alpine:single" % self.registry.host)
        digest = pl.Digest(self.layer1)
        del self.registry.blobs[digest]  # the registry no longer has it; the cache does
        client = pl.RegistryClient(self.registry.host, "library/alpine")
        self.assertEqual(self.engine._blob(client, digest), self.layer1)

    def test_a_blob_that_does_not_match_its_digest_is_refused(self):
        self.registry.corrupt.add(self.registry.add_blob(self.layer2))
        with self.assertRaises(pl.ServerError) as ctx:
            self.engine.image_pull("%s/library/alpine:single" % self.registry.host)
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("does not match", ctx.exception.message)

    def test_an_unknown_tag_is_not_found(self):
        with self.assertRaises(pl.ServerError) as ctx:
            self.engine.image_pull("%s/library/alpine:nothere" % self.registry.host)
        self.assertIn("not found", ctx.exception.message)

    def test_a_pull_needs_a_reference(self):
        with self.assertRaises(pl.ServerError):
            self.engine.image_pull("")

    def test_a_registry_asking_for_basic_auth_is_refused(self):
        client = pl.RegistryClient("127.0.0.1:1", "library/alpine")
        with self.assertRaises(pl.RegistryError):
            client._authorize('Basic realm="x"')

    def test_whiteouts_and_escapes_on_their_own(self):
        root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(root, "d"))
        with io.open(os.path.join(root, "d", "gone"), "w") as fh:
            fh.write("x")
        pl.apply_layer(make_layer([("d/.wh.gone", "file", "", 0o644)]), root)
        self.assertFalse(os.path.lexists(os.path.join(root, "d", "gone")))
        pl.apply_layer(make_layer([("../../escape-me", "file", "bad\n", 0o644)]), root)
        self.assertTrue(os.path.isfile(os.path.join(root, "escape-me")))
        self.assertFalse(os.path.lexists(os.path.join(self.tmp, "escape-me")))


class CliPullTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.registry = FakeRegistry()
        self.home = os.path.join(self.tmp, "home")
        state_dir = os.path.join(self.tmp, "fake-state")
        os.makedirs(state_dir)
        runtime = os.path.join(self.tmp, "fake-crun")
        with io.open(runtime, "w", encoding="utf-8") as fh:
            fh.write(FAKE_RUNTIME % {"python": sys.executable, "state": state_dir})
        os.chmod(runtime, stat.S_IRWXU)
        self.engine = pl.EngineServer(self.home, runtime_path=runtime)
        self.sock = os.path.join(self.tmp, "api.sock")
        self.server = pl.EngineUnixServer(self.sock, self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.env = dict(os.environ)
        self.env["CONTAINER_HOST"] = "unix://" + self.sock

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.registry.close()
        shutil.rmtree(self.tmp)

    def podman(self, args):
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "podman.py")] + list(args),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        out, err = proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def test_pull_prints_the_image_id(self):
        layer = make_layer([("etc", "dir", None, 0o755)])
        blob = self.registry.add_blob(layer)
        config = image_config(["A=1"], ["/bin/true"], "/")
        config_digest = self.registry.add_blob(config)
        manifest = json.dumps({"schemaVersion": 2, "mediaType": pl.OCI1_MANIFEST_MEDIA_TYPE,
                               "config": {"digest": config_digest, "size": len(config)},
                               "layers": [{"digest": blob, "size": len(layer)}]}).encode("utf-8")
        self.registry.add_manifest(["t"], pl.OCI1_MANIFEST_MEDIA_TYPE, manifest)
        code, out, err = self.podman(["pull", "%s/library/x:t" % self.registry.host])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), config_digest[len("sha256:"):])

    def test_run_pulls_a_missing_image_and_uses_its_environment(self):
        layer = make_layer([("etc", "dir", None, 0o755)])
        blob = self.registry.add_blob(layer)
        config = image_config(["GREETING=from-image"], ["/bin/true"], "/")
        config_digest = self.registry.add_blob(config)
        manifest = json.dumps({"schemaVersion": 2, "mediaType": pl.OCI1_MANIFEST_MEDIA_TYPE,
                               "config": {"digest": config_digest, "size": len(config)},
                               "layers": [{"digest": blob, "size": len(layer)}]}).encode("utf-8")
        self.registry.add_manifest(["r"], pl.OCI1_MANIFEST_MEDIA_TYPE, manifest)
        code, out, err = self.podman(["run", "--rm", "%s/library/x:r" % self.registry.host,
                                      "sh", "-c", "echo $GREETING"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "from-image\n")


if __name__ == "__main__":
    unittest.main()
