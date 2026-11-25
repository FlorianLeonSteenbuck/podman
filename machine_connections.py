#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""List and change the ssh connections of machines in podman-connections.json.

It reads and writes the same file as podman (PODMAN_CONNECTIONS_CONF, or
$XDG_CONFIG_HOME/containers, or ~/.config/containers) and takes the same lock.
Main-based; the logic is in podmanlib (Part 5).

    python3 machine_connections.py list
    python3 machine_connections.py add-pair <name> <uid> <port> <user> <identity>
    python3 machine_connections.py remove <name> [<name> ...]

Environment:
  PODMAN_ROOTFUL=1          add-pair: put the rootful connection first
  PODMAN_DEFAULT=1          add-pair: make the new connection the default
  PODMAN_MACHINES=a=0,b=1   remove: machines and their rootful flag (0 or 1)

Exit status: 0 ok, 1 connection error, 2 usage error.
"""
from __future__ import print_function

import os
import sys

import podmanlib as pl


def env_flag(name):
    return os.environ.get(name, "") in ("1", "true", "yes")


def parse_machines(text):
    machines = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, flag = item.partition("=")
        machines[name.strip()] = flag.strip() in ("1", "true", "yes")
    return machines


def cmd_list(argv):
    rows = []
    cfg = pl.read_connection_conf(pl.connections_config_path())
    for con in pl.GetAllConnections(cfg):
        dest = con["destination"]
        rows.append((con["name"], "*" if con["default"] else "",
                     "rw" if con["read_write"] else "ro", dest.get("URI", ""),
                     dest.get("Identity", "")))
    if not rows:
        print("no connections")
        return 0
    headers = ("NAME", "DEFAULT", "MODE", "URI", "IDENTITY")
    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for row in rows:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)).rstrip())
    return 0


def cmd_add_pair(argv):
    usage = "usage: machine_connections.py add-pair <name> <uid> <port> <user> <identity>"
    if len(argv) != 7:
        print(usage, file=sys.stderr)
        return 2
    try:
        uid, port = int(argv[3]), int(argv[4])
    except ValueError:
        print(usage + "  (uid and port must be numbers)", file=sys.stderr)
        return 2
    name, user, identity = argv[2], argv[5], argv[6]
    pl.EditConnectionConfig(lambda cfg: pl.AddSSHConnectionsToPodmanSocket(
        cfg, uid, port, identity, name, user, env_flag("PODMAN_ROOTFUL"),
        env_flag("PODMAN_DEFAULT")))
    print("added %s and %s-root" % (name, name))
    return 0


def cmd_remove(argv):
    if len(argv) < 3:
        print("usage: machine_connections.py remove <name> [<name> ...]", file=sys.stderr)
        return 2
    names = argv[2:]
    machines = parse_machines(os.environ.get("PODMAN_MACHINES", ""))
    pl.EditConnectionConfig(lambda cfg: pl.RemoveConnections(cfg, machines, names))
    print("removed " + ", ".join(names))
    return 0


COMMANDS = {
    "list": cmd_list,
    "add-pair": cmd_add_pair,
    "remove": cmd_remove,
}


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print("usage: machine_connections.py {list|add-pair|remove} ...", file=sys.stderr)
        return 2
    try:
        return COMMANDS[argv[1]](argv)
    except pl.MachineConnectionError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
