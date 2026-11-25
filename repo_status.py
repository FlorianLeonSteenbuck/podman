#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Show branch, commit, date, dirty file count and pin state of every repo.

    python repo_status.py
    PODMAN_ENV_FORMAT=json python repo_status.py

Formats: table (default), json. Main-based; the logic is in podmanlib.
"""
from __future__ import print_function

import sys

import podmanlib


def main(argv=None):
    env = podmanlib.PodmanEnvironment.instance()
    fmt = podmanlib.output_format()
    if fmt == "dot":  # DOT is for graphs only
        fmt = "table"
    rows = env.status()
    print(podmanlib.make_formatter(fmt).render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
