#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Check the Python port of the machine connections against the Go code.

The Go side is pkg/machine/connection (connection.go and add.go, copied
unchanged except for two import paths) running against small stubs of
container-libs common/pkg/config and podman define. The stubs keep the JSON
tags of the real types. A random sequence of operations is run through both
sides. Every result line, including the connections file after each step, is
compared.

Operations that Go would resolve in map order (see removal_is_ambiguous) are
not generated, so the results are deterministic.

    PODMAN_GO_PODMAN=/path/to/podman python check_machine_connections.py [scenarios]

Exit status: 0 all lines equal, 1 mismatches, 2 missing go or checkout.
"""
from __future__ import print_function

import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

import podmanlib as pl

GO_MOD = "module conndriver\n\ngo 1.24\n"

CONFIG_GO = r'''package config

// Stubs with the JSON and TOML tags of container-libs common/pkg/config (driver only).

import (
	"encoding/json"
	"fmt"
)

type ConnectionConfig struct {
	Default     string                 `json:",omitempty"`
	Connections map[string]Destination `json:",omitempty"`
}

type ConnectionsFile struct {
	Connection ConnectionConfig
	Farm       FarmConfig
}

type FarmConfig struct {
	Default string              `json:",omitempty" toml:"default,omitempty"`
	List    map[string][]string `json:",omitempty" toml:"list,omitempty"`
}

type Destination struct {
	URI       string `toml:"uri"`
	Identity  string `json:",omitempty" toml:"identity,omitempty"`
	IsMachine bool   `json:",omitempty" toml:"is_machine,omitempty"`
}

// Current is the connections file content.
var Current = "{}"

// EditConnectionConfig mirrors the real function: read, callback, write on success only.
func EditConnectionConfig(callback func(cfg *ConnectionsFile) error) error {
	conf := new(ConnectionsFile)
	if err := json.Unmarshal([]byte(Current), conf); err != nil {
		return fmt.Errorf("parse: %w", err)
	}
	if conf.Farm.List == nil {
		conf.Farm.List = make(map[string][]string)
	}
	if err := callback(conf); err != nil {
		return err
	}
	b, err := json.Marshal(conf)
	if err != nil {
		return err
	}
	Current = string(b)
	return nil
}
'''

DEFINE_GO = r'''package define

const DefaultMachineName = "podman-machine-default"

type InitOptions struct {
	IgnitionPath string
	Rootful      bool
	IsDefault    bool
}
'''

DRIVER_GO = r'''package connection

// RunOp applies one operation to config.Current (driver only).
// It returns "OK <state>" or "E <message>". On error the state is unchanged.

import (
	"encoding/json"

	"conndriver/config"
	"conndriver/define"
)

type op struct {
	Op          string          `json:"op"`
	Cons        [][4]string     `json:"cons"`
	Identity    string          `json:"identity"`
	Default     bool            `json:"default"`
	Name        string          `json:"name"`
	UID         int             `json:"uid"`
	Port        int             `json:"port"`
	User        string          `json:"user"`
	Rootful     bool            `json:"rootful"`
	RootfulName string          `json:"rootful_name"`
	Names       []string        `json:"names"`
	Machines    map[string]bool `json:"machines"`
	Host        string          `json:"host"`
	Path        string          `json:"path"`
	PortStr     string          `json:"port_str"`
}

func canonical() string {
	var v interface{}
	if err := json.Unmarshal([]byte(config.Current), &v); err != nil {
		panic(err)
	}
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func RunOp(line string) string {
	var o op
	if err := json.Unmarshal([]byte(line), &o); err != nil {
		return "E bad op: " + err.Error()
	}
	var err error
	switch o.Op {
	case "reset":
		config.Current = "{}"
		return "OK " + canonical()
	case "url":
		return "OK " + makeSSHURL(o.Host, o.Path, o.PortStr, o.User).String()
	case "add":
		var cons []connection
		for _, c := range o.Cons {
			cons = append(cons, connection{name: c[0], uri: makeSSHURL("127.0.0.1", c[1], c[2], c[3])})
		}
		err = addConnection(cons, o.Identity, o.Default)
	case "pair":
		err = UpdateConnectionPairPort(o.Name, o.Port, o.UID, o.User, o.Identity)
	case "ifdefault":
		err = UpdateConnectionIfDefault(o.Rootful, o.Name, o.RootfulName)
	case "remove":
		err = RemoveConnections(o.Machines, o.Names...)
	case "addssh":
		err = AddSSHConnectionsToPodmanSocket(o.UID, o.Port, o.Identity, o.Name, o.User,
			define.InitOptions{Rootful: o.Rootful, IsDefault: o.Default})
	default:
		return "E unknown op"
	}
	if err != nil {
		return "E " + err.Error()
	}
	return "OK " + canonical()
}
'''

MAIN_GO = r'''package main

// Reads one operation per line on stdin, prints one result per line.

import (
	"bufio"
	"fmt"
	"os"

	"conndriver/connection"
)

func main() {
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	w := bufio.NewWriter(os.Stdout)
	defer w.Flush()
	for sc.Scan() {
		fmt.Fprintln(w, connection.RunOp(sc.Text()))
	}
}
'''

NAMES = ["podman-machine-default", "dev", "prod", "dev-root", "prod-root", "x"]
USERS = ["core", "root", "", "us er", "a@b", "a:b", "é", "x/y?z#w", "a+b,c;d=e$f&g"]
PATHS = ["/run/user/1000/podman/podman.sock", "/run/podman/podman.sock", "/a b",
         "/x?y#z", "/é/ü", "*", "", "/a%b", "/(x)!*'"]
HOSTS = ["127.0.0.1", "::1", "[::1]", "localhost", "exa mple", "a%b", "h@st", "h/st", "é"]
PORT_STRS = ["2222", "0", "", "x y", "65535", "-1"]
IDENTITIES = ["/home/u/.ssh/id", "", "/x y", "/é"]


def find_checkout(podman_dir):
    return os.path.join(podman_dir, "pkg", "machine", "connection")


def build_driver(podman_dir, workdir):
    src = find_checkout(podman_dir)
    for sub in ("config", "define", "connection"):
        os.mkdir(os.path.join(workdir, sub))
    for name in ("connection.go", "add.go"):
        text = io.open(os.path.join(src, name), encoding="utf-8").read()
        text = text.replace('"go.podman.io/common/pkg/config"', '"conndriver/config"')
        text = text.replace('"go.podman.io/podman/v6/pkg/machine/define"', '"conndriver/define"')
        write_text(os.path.join(workdir, "connection", name), text)
    files = {
        "go.mod": GO_MOD,
        "config/config.go": CONFIG_GO,
        "define/define.go": DEFINE_GO,
        "connection/driver.go": DRIVER_GO,
        "main.go": MAIN_GO,
    }
    for rel, text in files.items():
        write_text(os.path.join(workdir, rel), text)
    built = pl.run(["go", "build", "-o", os.path.join(workdir, "conndriver"), "."], cwd=workdir)
    if not built.ok:
        raise RuntimeError("go build failed: " + built.stderr)
    return os.path.join(workdir, "conndriver")


def write_text(path, text):
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def random_op(rnd):
    kind = rnd.choice(["add", "add", "pair", "ifdefault", "remove", "remove",
                       "addssh", "addssh", "url"])
    if kind == "add":
        cons = [[rnd.choice(NAMES), rnd.choice(PATHS), rnd.choice(PORT_STRS), rnd.choice(USERS)]
                for _ in range(rnd.randint(1, 2))]
        return {"op": "add", "cons": cons, "identity": rnd.choice(IDENTITIES),
                "default": rnd.random() < 0.5}
    if kind == "pair":
        return {"op": "pair", "name": rnd.choice(NAMES), "uid": rnd.randint(0, 2000),
                "port": rnd.choice([0, 22, 2222, 65535, -1]), "user": rnd.choice(USERS),
                "identity": rnd.choice(IDENTITIES)}
    if kind == "ifdefault":
        return {"op": "ifdefault", "rootful": rnd.random() < 0.5,
                "name": rnd.choice(NAMES), "rootful_name": rnd.choice(NAMES)}
    if kind == "remove":
        names = rnd.sample(NAMES + ["nope"], rnd.randint(1, 2))
        machines = dict((n, rnd.random() < 0.5) for n in NAMES if rnd.random() < 0.5)
        return {"op": "remove", "names": names, "machines": machines}
    if kind == "addssh":
        return {"op": "addssh", "uid": rnd.randint(0, 2000), "port": rnd.choice([22, 2222]),
                "identity": rnd.choice(IDENTITIES), "name": rnd.choice(NAMES),
                "user": rnd.choice(USERS), "rootful": rnd.random() < 0.5,
                "default": rnd.random() < 0.5}
    return {"op": "url", "host": rnd.choice(HOSTS), "path": rnd.choice(PATHS),
            "port_str": rnd.choice(PORT_STRS), "user": rnd.choice(USERS)}


def py_apply(text, op):
    """The Python side of RunOp: returns (new text, result line)."""
    kind = op["op"]
    if kind == "reset":
        return "{}", "OK " + pl.canonical_json(json.loads("{}"))
    if kind == "url":
        return text, "OK " + pl.make_ssh_url(op["host"], op["path"], op["port_str"], op["user"])
    try:
        if kind == "add":
            cons = [(c[0], pl.make_ssh_url("127.0.0.1", c[1], c[2], c[3])) for c in op["cons"]]
            new = pl.edit_connections_text(
                text, lambda cfg: pl.addConnection(cfg, cons, op["identity"], op["default"]))
        elif kind == "pair":
            new = pl.edit_connections_text(
                text, lambda cfg: pl.UpdateConnectionPairPort(
                    cfg, op["name"], op["port"], op["uid"], op["user"], op["identity"]))
        elif kind == "ifdefault":
            new = pl.edit_connections_text(
                text, lambda cfg: pl.UpdateConnectionIfDefault(
                    cfg, op["rootful"], op["name"], op["rootful_name"]))
        elif kind == "remove":
            new = pl.edit_connections_text(
                text, lambda cfg: pl.RemoveConnections(cfg, op["machines"], op["names"]))
        elif kind == "addssh":
            new = pl.edit_connections_text(
                text, lambda cfg: pl.AddSSHConnectionsToPodmanSocket(
                    cfg, op["uid"], op["port"], op["identity"], op["name"], op["user"],
                    op["rootful"], op["default"]))
        else:
            raise ValueError("unknown op " + kind)
    except pl.MachineConnectionError as exc:
        return text, "E " + str(exc)
    return new, "OK " + pl.canonical_json(json.loads(new))


def generate(rnd, scenarios, ops_per_scenario):
    """Yields (op, expected line) pairs, skipping map-order-dependent removals."""
    for _ in range(scenarios):
        text = "{}"
        yield {"op": "reset"}, py_apply(text, {"op": "reset"})[1]
        for _ in range(ops_per_scenario):
            op = random_op(rnd)
            cfg = pl.connections_from_text(text)
            if op["op"] == "remove" and pl.removal_is_ambiguous(cfg, op["names"]):
                continue
            if op["op"] == "pair" and cfg.connections is None:
                continue  # Go panics on a nil map here (see UpdateConnectionPairPort)
            text, line = py_apply(text, op)
            yield op, line


def check(podman_dir, scenarios):
    rnd = random.Random(20261125)
    pairs = list(generate(rnd, scenarios, 40))
    workdir = tempfile.mkdtemp()
    try:
        binary = build_driver(podman_dir, workdir)
        data = "".join(json.dumps(op, sort_keys=True) + "\n" for op, _ in pairs).encode("utf-8")
        proc = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        go_out, _ = proc.communicate(data)
    finally:
        shutil.rmtree(workdir)
    go_lines = go_out.decode("utf-8").split("\n")
    bad = 0
    for i, (op, want) in enumerate(pairs):
        got = go_lines[i] if i < len(go_lines) else "<no output: driver stopped>"
        if got != want:
            bad += 1
            if bad <= 5:
                print("mismatch for %s\n  go: %s\n  py: %s"
                      % (json.dumps(op, sort_keys=True), got, want))
    print("operations checked: %d, mismatches: %d" % (len(pairs), bad))
    return 0 if bad == 0 else 1


def main(argv=None):
    argv = sys.argv if argv is None else argv
    podman_dir = os.environ.get("PODMAN_GO_PODMAN", "")
    if not podman_dir or not os.path.isfile(os.path.join(find_checkout(podman_dir), "connection.go")):
        print("set PODMAN_GO_PODMAN to a podman checkout", file=sys.stderr)
        return 2
    if not pl.run(["go", "version"]).ok:
        print("go toolchain not found on PATH", file=sys.stderr)
        return 2
    scenarios = 200
    if len(argv) > 1:
        try:
            scenarios = int(argv[1])
        except ValueError:
            print("usage: python check_machine_connections.py [scenarios]", file=sys.stderr)
            return 2
    return check(podman_dir, scenarios)


if __name__ == "__main__":
    sys.exit(main())
