#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Check the Python port of the image manifest code against the Go original.

Needs a checkout of container-libs in PODMAN_GO_REFERENCE and the go toolchain
on PATH. The script copies Go functions verbatim out of the checkout
(image/internal/manifest and image/internal/pkg/platform), builds them in a
temporary Go module with small type stubs, and runs the same generated
inputs through both sides:

  guess       GuessMIMEType on generated JSON, including wrong field types
  normalize   NormalizedMIMEType
  wanted      WantedPlatforms with explicit and empty contexts (host path too)
  choose_oci  chooseInstance (OCI index) with prefer-gzip values
  choose_list ChooseInstance (Docker manifest list)

    PODMAN_GO_REFERENCE=/path/to/container-libs python check_manifest_port.py [count]

Exit status: 0 all lines equal, 1 mismatches, 2 missing go or checkout.
"""
from __future__ import print_function

import base64
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

import podmanlib as pl

GO_MOD = """module mdriver

go 1.24

require (
	github.com/opencontainers/go-digest v1.0.0
	github.com/opencontainers/image-spec v1.1.1
	github.com/sirupsen/logrus v1.10.2
)
"""

TYPES_GO = """package types

// SystemContext stands in for go.podman.io/image/v5/types (driver only).
type SystemContext struct {
	ArchitectureChoice string
	OSChoice           string
	VariantChoice      string
}

type OptionalBool byte

const (
	OptionalBoolUndefined OptionalBool = iota
	OptionalBoolTrue
	OptionalBoolFalse
)
"""

STUBS_GO = """package manifest

// Types that the extracted functions use from the real package (driver only).

import (
	"github.com/opencontainers/go-digest"
	imgspecv1 "github.com/opencontainers/image-spec/specs-go/v1"
)

type OCI1IndexPublic struct {
	Manifests []imgspecv1.Descriptor
}

type Schema2PlatformSpec struct {
	Architecture string
	OS           string
	OSVersion    string
	OSFeatures   []string
	Variant      string
	Features     []string
}

type Schema2ManifestDescriptor struct {
	Digest   digest.Digest
	Platform Schema2PlatformSpec
}

type Schema2ListPublic struct {
	Manifests []Schema2ManifestDescriptor
}
"""

EXTRACTED_IMPORTS = """package manifest

// Functions copied verbatim from container-libs (driver only).

import (
	"encoding/json"
	"fmt"
	"math"
	"slices"

	"github.com/opencontainers/go-digest"
	imgspecv1 "github.com/opencontainers/image-spec/specs-go/v1"
	"mdriver/platform"
	"mdriver/types"
)

var _ = json.Marshal
var _ = fmt.Sprintf
var _ = math.MaxInt
var _ digest.Digest
var _ imgspecv1.Descriptor
var _ = platform.WantedPlatforms
var _ types.SystemContext
"""

DRIVER_GO = r'''package manifest

// RunOp runs one operation against the copied functions (driver only).

import (
	"encoding/json"

	"github.com/opencontainers/go-digest"
	imgspecv1 "github.com/opencontainers/image-spec/specs-go/v1"
	"mdriver/platform"
	"mdriver/types"
)

type ctxJSON struct {
	ArchitectureChoice string `json:"ArchitectureChoice"`
	OSChoice           string `json:"OSChoice"`
	VariantChoice      string `json:"VariantChoice"`
}

type platJSON struct {
	Architecture string   `json:"architecture"`
	OS           string   `json:"os"`
	OSVersion    string   `json:"os.version"`
	OSFeatures   []string `json:"os.features"`
	Variant      string   `json:"variant"`
}

type descJSON struct {
	Digest      string            `json:"digest"`
	Platform    *platJSON         `json:"platform"`
	Annotations map[string]string `json:"annotations"`
}

type opJSON struct {
	Op         string     `json:"op"`
	Blob       string     `json:"blob"`
	Input      string     `json:"input"`
	Ctx        *ctxJSON   `json:"ctx"`
	Manifests  []descJSON `json:"manifests"`
	PreferGzip int        `json:"prefer_gzip"`
}

func toCtx(c *ctxJSON) *types.SystemContext {
	if c == nil {
		return nil
	}
	return &types.SystemContext{ArchitectureChoice: c.ArchitectureChoice, OSChoice: c.OSChoice, VariantChoice: c.VariantChoice}
}

func toPlatform(p *platJSON) imgspecv1.Platform {
	if p == nil {
		return imgspecv1.Platform{}
	}
	return imgspecv1.Platform{Architecture: p.Architecture, OS: p.OS, OSVersion: p.OSVersion, OSFeatures: p.OSFeatures, Variant: p.Variant}
}

func RunOp(line string) string {
	var o opJSON
	if err := json.Unmarshal([]byte(line), &o); err != nil {
		return "E bad op: " + err.Error()
	}
	switch o.Op {
	case "guess":
		return "OK " + GuessMIMEType([]byte(o.Blob))
	case "normalize":
		return "OK " + NormalizedMIMEType(o.Input)
	case "wanted":
		out := ""
		for i, p := range platform.WantedPlatforms(toCtx(o.Ctx)) {
			if i > 0 {
				out += ";"
			}
			out += p.OS + "/" + p.Architecture + "/" + p.Variant
		}
		return "OK " + out
	case "choose_oci":
		idx := OCI1IndexPublic{}
		for _, d := range o.Manifests {
			desc := imgspecv1.Descriptor{Digest: digest.Digest(d.Digest), Annotations: d.Annotations}
			if d.Platform != nil {
				p := toPlatform(d.Platform)
				desc.Platform = &p
			}
			idx.Manifests = append(idx.Manifests, desc)
		}
		dg, err := idx.chooseInstance(toCtx(o.Ctx), types.OptionalBool(o.PreferGzip))
		if err != nil {
			return "E " + err.Error()
		}
		return "OK " + string(dg)
	case "choose_list":
		list := Schema2ListPublic{}
		for _, d := range o.Manifests {
			p := toPlatform(d.Platform)
			list.Manifests = append(list.Manifests, Schema2ManifestDescriptor{
				Digest:   digest.Digest(d.Digest),
				Platform: Schema2PlatformSpec{Architecture: p.Architecture, OS: p.OS, OSVersion: p.OSVersion, OSFeatures: p.OSFeatures, Variant: p.Variant},
			})
		}
		dg, err := list.ChooseInstance(toCtx(o.Ctx))
		if err != nil {
			return "E " + err.Error()
		}
		return "OK " + string(dg)
	}
	return "E unknown op"
}
'''

MAIN_GO = r'''package main

// Reads one operation per line on stdin, prints one result per line.

import (
	"bufio"
	"os"

	"mdriver/manifest"
)

func main() {
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	w := bufio.NewWriter(os.Stdout)
	defer w.Flush()
	for sc.Scan() {
		w.WriteString(manifest.RunOp(sc.Text()) + "\n")
	}
}
'''

GO_FILES = {
    "manifest": os.path.join("image", "internal", "manifest"),
    "platform": os.path.join("image", "internal", "pkg", "platform"),
}

DOCKER_CONST_MARKER = "DockerV2Schema1MediaType"
ZSTD_CONST_MARKER = "OCI1InstanceAnnotationCompressionZSTD"

MEDIA = {
    "s1": "application/vnd.docker.distribution.manifest.v1+json",
    "s1s": "application/vnd.docker.distribution.manifest.v1+prettyjws",
    "s2": "application/vnd.docker.distribution.manifest.v2+json",
    "s2c": "application/vnd.docker.container.image.v1+json",
    "list": "application/vnd.docker.distribution.manifest.list.v2+json",
    "oci": "application/vnd.oci.image.manifest.v1+json",
    "ocii": "application/vnd.oci.image.index.v1+json",
    "ocic": "application/vnd.oci.image.config.v1+json",
}
ARCHES = ["amd64", "arm64", "arm", "386", "ppc64le", "s390x"]
OSES = ["linux", "windows", ""]
VARIANTS = ["", "v8", "v7", "v6", "v5", "v4", "v9"]


def extract(text, header, closing="\n}\n"):
    """A top-level Go declaration, from its header line to the closing brace at column 0."""
    m = re.search(header, text, re.M)
    if m is None:
        raise RuntimeError("declaration not found: " + header)
    end = text.index(closing, m.end()) + len(closing)
    return text[m.start():end]


def extract_const_block(text, marker):
    for m in re.finditer(r"^const \(\n", text, re.M):
        end = text.index("\n)\n", m.end()) + len("\n)\n")
        block = text[m.start():end]
        if marker in block:
            return block
    raise RuntimeError("const block not found: " + marker)


def read(path):
    return io.open(path, encoding="utf-8").read()


def write_text(path, text):
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def build_driver(ref_dir, workdir):
    base = os.path.join(ref_dir, GO_FILES["manifest"])
    manifest_go = read(os.path.join(base, "manifest.go"))
    oci_index_go = read(os.path.join(base, "oci_index.go"))
    list_go = read(os.path.join(base, "docker_schema2_list.go"))
    platform_go = read(os.path.join(ref_dir, GO_FILES["platform"], "platform_matcher.go"))

    extracted = [
        EXTRACTED_IMPORTS,
        extract_const_block(manifest_go, DOCKER_CONST_MARKER),
        extract_const_block(oci_index_go, ZSTD_CONST_MARKER),
        extract(manifest_go, r"^func GuessMIMEType\("),
        extract(manifest_go, r"^func NormalizedMIMEType\("),
        extract(oci_index_go, r"^func \(index \*OCI1IndexPublic\) chooseInstance\("),
        extract(oci_index_go, r"^func \(index \*OCI1IndexPublic\) ChooseInstance\("),
        extract(oci_index_go, r"^func instanceIsZstd\("),
        extract(oci_index_go, r"^type instanceCandidate struct \{"),
        extract(oci_index_go, r"^func \(ic instanceCandidate\) isPreferredOver\("),
        extract(oci_index_go, r"^func ociPlatformClone\("),
        extract(list_go, r"^func \(list \*Schema2ListPublic\) ChooseInstance\("),
        extract(list_go, r"^func ociPlatformFromSchema2PlatformSpec\("),
    ]
    dirs = ["types", "platform", "manifest"]
    for d in dirs:
        os.mkdir(os.path.join(workdir, d))
    write_text(os.path.join(workdir, "go.mod"), GO_MOD)
    write_text(os.path.join(workdir, "types", "types.go"), TYPES_GO)
    write_text(os.path.join(workdir, "platform", "platform_matcher.go"),
               platform_go.replace('"go.podman.io/image/v5/types"', '"mdriver/types"'))
    write_text(os.path.join(workdir, "manifest", "extracted.go"), "\n".join(extracted))
    write_text(os.path.join(workdir, "manifest", "stubs.go"), STUBS_GO)
    write_text(os.path.join(workdir, "manifest", "driver.go"), DRIVER_GO)
    write_text(os.path.join(workdir, "main.go"), MAIN_GO)
    tidy = pl.run(["go", "mod", "tidy"], cwd=workdir)
    if not tidy.ok:
        raise RuntimeError("go mod tidy failed: " + tidy.stderr)
    built = pl.run(["go", "build", "-o", os.path.join(workdir, "mdriver"), "."], cwd=workdir)
    if not built.ok:
        raise RuntimeError("go build failed: " + built.stderr)
    return os.path.join(workdir, "mdriver")


# --- generated inputs -----------------------------------------------------


def rand_hex(rnd, n):
    return "".join(rnd.choice("0123456789abcdef") for _ in range(n))


def rand_scalar(rnd):
    return rnd.choice([None, 0, 1, 2, -1, 2.0, 1e400, True, "", "2", "x",
                       MEDIA["oci"], MEDIA["s2"], [], {}, [1], "AA=="])


def rand_descriptor(rnd):
    d = {"mediaType": rnd.choice([MEDIA["oci"], MEDIA["s2"], "x"]),
         "digest": "sha256:" + rand_hex(rnd, 64), "size": rnd.randint(0, 9000)}
    if rnd.random() < 0.4:
        d["platform"] = {"architecture": rnd.choice(ARCHES), "os": rnd.choice(OSES)}
        if rnd.random() < 0.5:
            d["platform"]["variant"] = rnd.choice(VARIANTS)
    if rnd.random() < 0.2:
        d["annotations"] = {"io.github.containers.compression.zstd": rnd.choice(["true", "false", "x"])}
    if rnd.random() < 0.05:
        d["data"] = base64.b64encode(b"hello").decode("ascii")
    if rnd.random() < 0.08:
        key = rnd.choice(["size", "digest", "platform", "urls", "annotations", "data"])
        d[key] = rand_scalar(rnd)
    return d


def rand_manifest_obj(rnd):
    kind = rnd.choice(["s2", "oci", "ocii", "list", "s1", "s1s", "other", "scalar"])
    if kind == "s2":
        obj = {"schemaVersion": 2, "mediaType": MEDIA["s2"],
               "config": {"mediaType": MEDIA["s2c"], "size": 10, "digest": "sha256:" + rand_hex(rnd, 64)}}
    elif kind == "oci":
        obj = {"schemaVersion": 2,
               "config": {"mediaType": MEDIA["ocic"], "size": 10, "digest": "sha256:" + rand_hex(rnd, 64)}}
    elif kind == "ocii":
        obj = {"schemaVersion": 2, "manifests": [rand_descriptor(rnd) for _ in range(rnd.randint(0, 3))]}
    elif kind == "list":
        obj = {"schemaVersion": 2, "mediaType": MEDIA["list"],
               "manifests": [rand_descriptor(rnd) for _ in range(rnd.randint(0, 3))]}
    elif kind == "s1":
        obj = {"schemaVersion": 1, "name": "x"}
        if rnd.random() < 0.5:
            obj["signatures"] = [{"header": {}}]
    elif kind == "s1s":
        obj = {"schemaVersion": 1, "signatures": [{}]}
    elif kind == "other":
        obj = {"schemaVersion": rnd.choice([0, 3, 2.5, None])}
        if rnd.random() < 0.5:
            obj["config"] = {"mediaType": rnd.choice([MEDIA["oci"], MEDIA["s2c"], "text/plain"])}
        if rnd.random() < 0.5:
            obj["manifests"] = [rand_descriptor(rnd)]
    else:
        return rnd.choice(["", "null", "[]", "{", "42", "\"x\"", "{\"schemaVersion\":2,\"mediaType\":\"é\"}"])
    # Wrong field types in known places.
    for _ in range(rnd.choice([0, 0, 0, 1])):
        key = rnd.choice(["schemaVersion", "mediaType", "config", "manifests", "signatures"])
        obj[key] = rand_scalar(rnd)
    return json.dumps(obj, ensure_ascii=False)


def rand_ctx(rnd):
    r = rnd.random()
    if r < 0.1:
        return None
    ctx = {}
    if rnd.random() < 0.9:
        ctx["ArchitectureChoice"] = rnd.choice(ARCHES)
    if rnd.random() < 0.6:
        ctx["OSChoice"] = rnd.choice(OSES)
    if rnd.random() < 0.6:
        ctx["VariantChoice"] = rnd.choice(VARIANTS)
    return ctx


def rand_platform(rnd):
    p = {"architecture": rnd.choice(ARCHES), "os": rnd.choice(OSES)}
    if rnd.random() < 0.7:
        p["variant"] = rnd.choice(VARIANTS)
    return p


def rand_choice_manifests(rnd, oci):
    out = []
    for _ in range(rnd.randint(1, 6)):
        d = {"digest": "sha256:" + rand_hex(rnd, 8)}
        if oci:
            d["platform"] = None if rnd.random() < 0.2 else rand_platform(rnd)
            if rnd.random() < 0.4:
                d["annotations"] = {"io.github.containers.compression.zstd":
                                    rnd.choice(["true", "false", "x"])}
            else:
                d["annotations"] = None
        else:
            d["platform"] = rand_platform(rnd) if rnd.random() < 0.9 else {}
        out.append(d)
    return out


def generate(rnd, count):
    ops = []
    for _ in range(count):
        kind = rnd.choice(["guess", "guess", "guess", "normalize", "wanted",
                           "choose_oci", "choose_oci", "choose_list", "choose_list"])
        if kind == "guess":
            ops.append({"op": "guess", "blob": rand_manifest_obj(rnd)})
        elif kind == "normalize":
            ops.append({"op": "normalize", "input": rnd.choice(
                list(MEDIA.values()) + ["application/json", "text/plain", ""])})
        elif kind == "wanted":
            ops.append({"op": "wanted", "ctx": rand_ctx(rnd)})
        elif kind == "choose_oci":
            ops.append({"op": "choose_oci", "manifests": rand_choice_manifests(rnd, True),
                        "ctx": rand_ctx(rnd), "prefer_gzip": rnd.choice([0, 1, 2])})
        else:
            ops.append({"op": "choose_list", "manifests": rand_choice_manifests(rnd, False),
                        "ctx": rand_ctx(rnd)})
    return ops


# --- the Python side of RunOp -------------------------------------------------


def py_apply(op):
    kind = op["op"]
    if kind == "guess":
        return "OK " + pl.GuessMIMEType(op["blob"])
    if kind == "normalize":
        return "OK " + pl.NormalizedMIMEType(op["input"])
    if kind == "wanted":
        return "OK " + ";".join("%s/%s/%s" % (p["os"], p["architecture"], p["variant"])
                                for p in pl.WantedPlatforms(op["ctx"]))
    try:
        if kind == "choose_oci":
            digest = pl.OCI1IndexChooseInstance(op["manifests"], op["ctx"], op["prefer_gzip"])
        else:
            digest = pl.Schema2ListChooseInstance(op["manifests"], op["ctx"])
    except pl.ManifestError as exc:
        return "E " + str(exc)
    return "OK " + digest


def check(ref_dir, count):
    rnd = random.Random(20261125)
    ops = generate(rnd, count)
    workdir = tempfile.mkdtemp()
    try:
        binary = build_driver(ref_dir, workdir)
        data = "".join(json.dumps(op, ensure_ascii=False) + "\n" for op in ops).encode("utf-8")
        proc = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        go_out, _ = proc.communicate(data)
    finally:
        shutil.rmtree(workdir)
    go_lines = go_out.decode("utf-8").split("\n")
    bad = 0
    for i, op in enumerate(ops):
        got = go_lines[i] if i < len(go_lines) else "<no output: driver stopped>"
        want = py_apply(op)
        if got != want:
            bad += 1
            if bad <= 5:
                print("mismatch for %s\n  go: %s\n  py: %s"
                      % (json.dumps(op, ensure_ascii=False)[:300], got, want))
    print("operations checked: %d, mismatches: %d" % (len(ops), bad))
    return 0 if bad == 0 else 1


def main(argv=None):
    argv = sys.argv if argv is None else argv
    ref_dir = os.environ.get("PODMAN_GO_REFERENCE", "")
    if not ref_dir or not os.path.isdir(os.path.join(ref_dir, GO_FILES["manifest"])):
        print("set PODMAN_GO_REFERENCE to a container-libs checkout", file=sys.stderr)
        return 2
    if not pl.run(["go", "version"]).ok:
        print("go toolchain not found on PATH", file=sys.stderr)
        return 2
    count = 5000
    if len(argv) > 1:
        try:
            count = int(argv[1])
        except ValueError:
            print("usage: python check_manifest_port.py [count]", file=sys.stderr)
            return 2
    return check(ref_dir, count)


if __name__ == "__main__":
    sys.exit(main())
