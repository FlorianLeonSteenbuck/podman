#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Check the Python port (podmanlib, Part 1) against the Go original.

Copies repos/podman/vendor/golang.org/x/mod/semver/semver.go into a temporary
Go module, builds a small driver next to it, feeds the same random and
hand-picked inputs to both implementations and compares every result.

    python check_semver_port.py [count]     default count: 20000

Needs the go toolchain on PATH. Without it the script says so and exits 0.
"""
from __future__ import print_function

import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

import podmanlib

GO_MOD = "module semvercheck\n\ngo 1.21\n"

GO_DRIVER = r'''package main

// Driver for the original semver.go. Mode "pairs": stdin lines "v<TAB>w".
// Mode "sort": stdin lines "v", printed back sorted.

import (
	"bufio"
	"fmt"
	"os"
	"strings"

	"semvercheck/semver"
)

func b2i(b bool) int {
	if b {
		return 1
	}
	return 0
}

func main() {
	mode := "pairs"
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	w := bufio.NewWriter(os.Stdout)
	defer w.Flush()
	if mode == "sort" {
		var list []string
		for sc.Scan() {
			list = append(list, sc.Text())
		}
		semver.Sort(list)
		for _, s := range list {
			fmt.Fprintln(w, s)
		}
		return
	}
	for sc.Scan() {
		f := strings.SplitN(sc.Text(), "\t", 2)
		v := f[0]
		x := ""
		if len(f) == 2 {
			x = f[1]
		}
		fmt.Fprintf(w, "%d\t%s\t%s\t%s\t%s\t%s\t%d\t%s\n",
			b2i(semver.IsValid(v)), semver.Canonical(v), semver.Major(v),
			semver.MajorMinor(v), semver.Prerelease(v), semver.Build(v),
			semver.Compare(v, x), semver.Max(v, x))
	}
}
'''

TOKENS = ["v", "V", "0", "1", "2", "9", "00", "01", "10", ".", "-", "+", "-0",
          "alpha", "beta", "rc", "x", "A", "z", "+build", "meta", "_", " "]
SHAPES = ["v%d", "v%d.%d", "v%d.%d.%d", "v%d.%d.%d-%s", "v%d.%d.%d+%s",
          "v%d.%d.%d-%s.%s", "v%d.%d.%d-%s+%s"]
PRERELEASE = ["alpha", "beta", "rc.1", "0", "00", "01", "1", "2", "10", "x-y",
              "0.3", "-", "a.b.c"]
EDGE = ["", "v", "v1", "v1.2", "v1.2.3", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2.3-",
        "v1.2.3-alpha", "v1.2.3-alpha.1", "v1.2.3-alpha.01", "v1.2.3+", "v1.2.3+build",
        "v1.2.3-pre+meta", "v1.0.0-alpha.beta", "v1.0.0-beta.11", "v1.0.0-beta.2",
        "v1.0.0-rc.1", "v1.0.0", "v0.0.0", "V1.0.0", "1.0.0", "v1..2", "v1.2.3-a..b",
        "v1.2.3-a-b.c", "v0.70.1-0.20261116084638-6da6e80dc141", "v6.2.1", "v6.2.0-rc1"]


def semver_go_path():
    return os.path.join(podmanlib.ROOT, "repos", "podman", "vendor",
                        "golang.org", "x", "mod", "semver", "semver.go")


def build_driver(workdir):
    pkg = os.path.join(workdir, "semver")
    os.mkdir(pkg)
    shutil.copy(semver_go_path(), os.path.join(pkg, "semver.go"))
    with open(os.path.join(workdir, "go.mod"), "w") as fh:
        fh.write(GO_MOD)
    with open(os.path.join(workdir, "main.go"), "w") as fh:
        fh.write(GO_DRIVER)
    binary = os.path.join(workdir, "semvercheck")
    res = podmanlib.run(["go", "build", "-o", binary, "."], cwd=workdir)
    if not res.ok:
        raise RuntimeError("go build failed: " + res.stderr)
    return binary


def random_shape(rnd):
    parts = []
    for piece in re.split(r"(%[ds])", rnd.choice(SHAPES)):
        if piece == "%d":
            if rnd.random() < 0.6:
                parts.append(str(rnd.choice([0, 1, 2, 3, 10, 20])))
            else:
                parts.append(rnd.choice(PRERELEASE))
        elif piece == "%s":
            parts.append(rnd.choice(PRERELEASE))
        else:
            parts.append(piece)
    return "".join(parts)


def random_version(rnd):
    r = rnd.random()
    if r < 0.45:
        return random_shape(rnd)
    if r < 0.75:
        s = random_shape(rnd)
        return s[:rnd.randint(0, len(s))]
    return "".join(rnd.choice(TOKENS) for _ in range(rnd.randint(0, 7)))


def py_line(a, b):
    return "\t".join([
        "%d" % (1 if podmanlib.IsValid(a) else 0),
        podmanlib.Canonical(a), podmanlib.Major(a), podmanlib.MajorMinor(a),
        podmanlib.Prerelease(a), podmanlib.Build(a),
        "%d" % podmanlib.Compare(a, b), podmanlib.Max(a, b)])


def check(count):
    if not podmanlib.run(["go", "version"]).ok:
        print("go toolchain not found on PATH; nothing checked.")
        return 0
    rnd = random.Random(20261125)
    versions = EDGE + [random_version(rnd) for _ in range(count // 3)]
    pairs = [(rnd.choice(versions), rnd.choice(versions)) for _ in range(count)]
    pairs += [(a, b) for a in EDGE for b in EDGE]

    workdir = tempfile.mkdtemp()
    try:
        binary = build_driver(workdir)
        data = "".join("%s\t%s\n" % (a, b) for a, b in pairs).encode("utf-8")
        proc = subprocess.Popen([binary, "pairs"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE)
        go_out, _ = proc.communicate(data)
        go_lines = go_out.decode("utf-8").splitlines()
        bad = 0
        for i, (a, b) in enumerate(pairs):
            want = py_line(a, b)
            if go_lines[i] != want:
                bad += 1
                if bad <= 5:
                    print("mismatch for %r, %r\n  go: %r\n  py: %r" % (a, b, go_lines[i], want))

        proc = subprocess.Popen([binary, "sort"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE)
        go_sorted, _ = proc.communicate(("\n".join(versions) + "\n").encode("utf-8"))
        go_sorted = go_sorted.decode("utf-8").splitlines()
        py_sorted = list(versions)
        podmanlib.Sort(py_sorted)
        sort_ok = go_sorted == py_sorted
    finally:
        shutil.rmtree(workdir)

    print("pairs checked: %d, mismatches: %d" % (len(pairs), bad))
    print("sort: %d strings, identical order: %s" % (len(versions), sort_ok))
    return 0 if bad == 0 and sort_ok else 1


def main(argv=None):
    argv = sys.argv if argv is None else argv
    count = 20000
    if len(argv) > 1:
        try:
            count = int(argv[1])
        except ValueError:
            print("usage: python check_semver_port.py [count]", file=sys.stderr)
            return 2
    return check(count)


if __name__ == "__main__":
    sys.exit(main())
