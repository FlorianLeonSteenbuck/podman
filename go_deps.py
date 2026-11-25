#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Go module dependencies between the repos. Main-based; the logic is in podmanlib.

    python go_deps.py graph      requirements between repos, with version drift (default)
    python go_deps.py modules    every go.mod found outside vendor/

PODMAN_ENV_FORMAT=table|json|dot   (dot works for graph only)

drift: same = required version equals the highest local tag,
       older = required version is below the local tag,
       newer (untagged) = required pseudo-version is above every tag,
       no tag = the module has no local tag.
"""
from __future__ import print_function

import sys
from collections import OrderedDict

import podmanlib


def module_rows(graph):
    rows = []
    for info in graph.modules:
        row = OrderedDict()
        row["repo"] = info.repo.name
        row["dir"] = info.rel_dir
        row["module"] = info.mod.module
        row["requires"] = str(len(info.mod.requires))
        row["replaces"] = str(len(info.mod.replaces))
        rows.append(row)
    return rows


COMMANDS = {
    "graph": lambda graph: graph.edges(),
    "modules": module_rows,
}


def main(argv=None):
    argv = sys.argv if argv is None else argv
    name = "graph" if len(argv) < 2 else podmanlib.command_name(argv, COMMANDS)
    if name is None:
        print("usage: python go_deps.py [graph|modules]", file=sys.stderr)
        return 2
    graph = podmanlib.PodmanEnvironment.instance().go_graph()
    rows = COMMANDS[name](graph)
    fmt = podmanlib.output_format()
    if name == "modules" and fmt == "dot":
        fmt = "table"
    if fmt == "table" and not rows:
        print("(no rows)")
    else:
        print(podmanlib.make_formatter(fmt).render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
