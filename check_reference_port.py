#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Check the Python port of container-libs image/docker/reference against Go.

Needs a checkout of container-libs in PODMAN_GO_REFERENCE and the go toolchain
on PATH. The script copies the original package into a temporary Go module,
adds a small driver, and feeds the same inputs to both implementations. Every
output line is compared. Inputs are the string literals of the package's own
*_test.go files plus random mutations of them.

    PODMAN_GO_REFERENCE=/path/to/container-libs python check_reference_port.py [count]

Exit status: 0 all lines equal, 1 mismatches, 2 missing go or checkout.
"""
from __future__ import print_function

import io
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

import podmanlib as pl

GO_MOD = "module refdriver\n\ngo 1.24\n\nrequire github.com/opencontainers/go-digest v1.0.0\n"

REGEXPSHIM_GO = """package regexpshim

import "regexp"

// Regexp stands in for go.podman.io/storage/pkg/regexp (driver only).
type Regexp struct{ re *regexp.Regexp }

func Delayed(val string) Regexp { return Regexp{regexp.MustCompile(val)} }

func (r Regexp) MatchString(s string) bool { return r.re.MatchString(s) }

func (r Regexp) FindStringSubmatch(s string) []string { return r.re.FindStringSubmatch(s) }
"""

TYPETAG_GO = """package reference

// TypeTag names the concrete type (driver only).
func TypeTag(r Reference) string {
	switch r.(type) {
	case repository:
		return "R"
	case reference:
		return "F"
	case taggedReference:
		return "T"
	case canonicalReference:
		return "C"
	case digestReference:
		return "D"
	}
	return "?"
}
"""

GO_MAIN = """package main

// Driver: runs the ORIGINAL image/docker/reference and go-digest.
// One output line per input line; fields separated by tabs.

import (
	"bufio"
	"fmt"
	"os"
	"strings"

	_ "crypto/sha256"
	_ "crypto/sha512"

	"github.com/opencontainers/go-digest"
	"refdriver/reference"
)

func fail(err error) string { return "E:" + err.Error() }

func main() {
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	w := bufio.NewWriter(os.Stdout)
	defer w.Flush()
	for sc.Scan() {
		s := sc.Text()
		var f []string

		r, err := reference.Parse(s)
		if err != nil {
			f = append(f, fail(err))
		} else {
			f = append(f, "OK:"+reference.TypeTag(r)+":"+r.String())
			if n, ok := r.(reference.Named); ok {
				f = append(f, reference.Domain(n)+"|"+reference.Path(n))
			} else {
				f = append(f, "-")
			}
		}

		n, err := reference.ParseNormalizedNamed(s)
		if err != nil {
			f = append(f, fail(err))
		} else {
			f = append(f, "OK:"+reference.TypeTag(n)+":"+n.String()+"|"+reference.FamiliarString(n)+"|"+reference.FamiliarName(n))
			f = append(f, "TNO:"+reference.TagNameOnly(n).String())
			f = append(f, fmt.Sprint("NO:", reference.IsNameOnly(n)))
			f = append(f, "TRIM:"+reference.TrimNamed(n).String())
		}

		if d, err := reference.ParseDockerRef(s); err != nil {
			f = append(f, fail(err))
		} else {
			f = append(f, "OK:"+d.String())
		}
		if p, err := reference.ParseNamed(s); err != nil {
			f = append(f, fail(err))
		} else {
			f = append(f, "OK:"+p.String())
		}
		if a, err := reference.ParseAnyReference(s); err != nil {
			f = append(f, fail(err))
		} else {
			f = append(f, "OK:"+reference.TypeTag(a)+":"+a.String())
		}
		f = append(f, fmt.Sprint("FULLID:", reference.IsFullIdentifier(s)))
		if dg, err := digest.Parse(s); err != nil {
			f = append(f, fail(err))
		} else {
			f = append(f, "OK:"+dg.String())
		}
		fmt.Fprintln(w, strings.Join(f, "\\t"))
	}
}
"""

TYPE_TAGS = {
    pl.repository: "R",
    pl.reference: "F",
    pl.taggedReference: "T",
    pl.canonicalReference: "C",
    pl.digestReference: "D",
}

EXTRA_INPUTS = ["", "a", "/", ":", "@", "sha256:", "docker.io/", "docker.io/library/",
                "localhost/", "localhost:5000/", "UPPER/case", "a/b/c:d@sha256:" + "f" * 64,
                "busybox:latest@sha256:" + "a" * 64, "x" * 300, "é/x", "a_b__c---d",
                "docker.io/library/alpine:3.20", "ubuntu", "sha256:" + "a" * 64]

MUTATION_CHARS = "abcXYZ019/:@._- sha256"


def driver_fields(s):
    """The Python side of GO_MAIN: the same fields, in the same order."""
    f = []
    try:
        r = pl.Parse(s)
        f.append("OK:%s:%s" % (TYPE_TAGS.get(type(r), "?"), r.String()))
        if isinstance(r, pl.Named):
            f.append("%s|%s" % (pl.Domain(r), pl.Path(r)))
        else:
            f.append("-")
    except ValueError as exc:
        f.append("E:" + str(exc))
    try:
        n = pl.ParseNormalizedNamed(s)
    except ValueError as exc:
        f.append("E:" + str(exc))
    else:
        f.append("OK:%s:%s|%s|%s" % (TYPE_TAGS.get(type(n), "?"), n.String(),
                                     pl.FamiliarString(n), pl.FamiliarName(n)))
        f.append("TNO:" + pl.TagNameOnly(n).String())
        f.append("NO:" + ("true" if pl.IsNameOnly(n) else "false"))
        f.append("TRIM:" + pl.TrimNamed(n).String())
    for fn in (pl.ParseDockerRef, pl.ParseNamed):
        try:
            f.append("OK:" + fn(s).String())
        except ValueError as exc:
            f.append("E:" + str(exc))
    try:
        a = pl.ParseAnyReference(s)
        f.append("OK:%s:%s" % (TYPE_TAGS.get(type(a), "?"), a.String()))
    except ValueError as exc:
        f.append("E:" + str(exc))
    f.append("FULLID:" + ("true" if pl.IsFullIdentifier(s) else "false"))
    try:
        f.append("OK:" + pl.digest_parse(s))
    except ValueError as exc:
        f.append("E:" + str(exc))
    return "\t".join(f)


def corpus(test_dir, rnd, count):
    """Literal strings from the package's tests, plus mutations of them."""
    literals = set(EXTRA_INPUTS)
    for name in sorted(os.listdir(test_dir)):
        if not name.endswith("_test.go"):
            continue
        text = io.open(os.path.join(test_dir, name), encoding="utf-8").read()
        for m in re.finditer(r'"([^"\\\n]*)"|`([^`\n]*)`', text):
            literals.add(m.group(1) if m.group(1) is not None else m.group(2))
    literals = sorted(s for s in literals if "\t" not in s and "\r" not in s)
    inputs = list(literals)
    for _ in range(count):
        s = rnd.choice(literals)
        for _ in range(rnd.randint(1, 4)):
            op = rnd.random()
            pos = rnd.randint(0, len(s))
            if op < 0.4 or s == "":
                s = s[:pos] + rnd.choice(MUTATION_CHARS) + s[pos:]
            elif op < 0.7:
                s = s[:pos] + s[pos + 1:]
            else:
                s = s[:pos] + rnd.choice(MUTATION_CHARS) + s[pos + 1:]
        inputs.append(s)
    return inputs


def write_text(path, text):
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def build_driver(ref_dir, workdir):
    pkg = os.path.join(workdir, "reference")
    os.mkdir(pkg)
    os.mkdir(os.path.join(workdir, "regexpshim"))
    for name in ("reference.go", "normalize.go", "regexp.go", "helpers.go",
                 "regexp-additions.go"):
        src = io.open(os.path.join(ref_dir, name), encoding="utf-8").read()
        src = src.replace('storageRegexp "go.podman.io/storage/pkg/regexp"',
                          'storageRegexp "refdriver/regexpshim"')
        write_text(os.path.join(pkg, name), src)
    write_text(os.path.join(pkg, "typetag_driver.go"), TYPETAG_GO)
    write_text(os.path.join(workdir, "regexpshim", "regexp.go"), REGEXPSHIM_GO)
    write_text(os.path.join(workdir, "main.go"), GO_MAIN)
    write_text(os.path.join(workdir, "go.mod"), GO_MOD)
    tidy = pl.run(["go", "mod", "tidy"], cwd=workdir)
    if not tidy.ok:
        raise RuntimeError("go mod tidy failed: " + tidy.stderr)
    binary = os.path.join(workdir, "refdriver")
    built = pl.run(["go", "build", "-o", binary, "."], cwd=workdir)
    if not built.ok:
        raise RuntimeError("go build failed: " + built.stderr)
    return binary


def check(ref_dir, count):
    rnd = random.Random(20261125)
    inputs = corpus(os.path.join(ref_dir, "image", "docker", "reference")
                    if os.path.isdir(os.path.join(ref_dir, "image")) else ref_dir,
                    rnd, count)
    workdir = tempfile.mkdtemp()
    try:
        binary = build_driver(os.path.join(ref_dir, "image", "docker", "reference")
                              if os.path.isdir(os.path.join(ref_dir, "image")) else ref_dir,
                              workdir)
        data = "".join(s + "\n" for s in inputs).encode("utf-8")
        proc = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        go_out, _ = proc.communicate(data)
    finally:
        shutil.rmtree(workdir)
    go_lines = go_out.decode("utf-8").split("\n")
    bad = 0
    for i, s in enumerate(inputs):
        want = driver_fields(s)
        if go_lines[i] != want:
            bad += 1
            if bad <= 5:
                print("mismatch for %r\n  go: %s\n  py: %s" % (s, go_lines[i], want))
    print("inputs checked: %d, mismatches: %d" % (len(inputs), bad))
    return 0 if bad == 0 else 1


def main(argv=None):
    argv = sys.argv if argv is None else argv
    ref_dir = os.environ.get("PODMAN_GO_REFERENCE", "")
    if not ref_dir or not os.path.isdir(os.path.join(ref_dir, "image", "docker", "reference")):
        print("set PODMAN_GO_REFERENCE to a container-libs checkout", file=sys.stderr)
        return 2
    if not pl.run(["go", "version"]).ok:
        print("go toolchain not found on PATH", file=sys.stderr)
        return 2
    count = 20000
    if len(argv) > 1:
        try:
            count = int(argv[1])
        except ValueError:
            print("usage: python check_reference_port.py [count]", file=sys.stderr)
            return 2
    return check(ref_dir, count)


if __name__ == "__main__":
    sys.exit(main())
