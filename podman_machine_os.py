#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""podman-machine-os, as far as this Python rewrite goes. Main-based.

The machine image is built and verified by the upstream project. Nothing of
that is ported yet, so every command except version exits with status 3.

    python3 podman_machine_os.py version
    python3 podman_machine_os.py build|verify|info ... (not implemented, exit 3)
"""
from __future__ import print_function

import sys

import podmanlib as pl

PROGRAM = "podman-machine-os"
VERSION = "0.1.0-rewrite"


def main(argv=None):
    argv = sys.argv if argv is None else argv
    args = argv[1:]
    if not args or args[0] in ("help", "-h", "--help"):
        sys.stdout.write("usage: podman_machine_os.py version | <other command, not implemented>\n")
        return pl.EXIT_OK
    if args[0] == "version":
        sys.stdout.write("%s version %s (python rewrite)\n" % (PROGRAM, VERSION))
        return pl.EXIT_OK
    return pl.not_implemented(PROGRAM, args[0], "machine image, not in the roadmap yet")


if __name__ == "__main__":
    sys.exit(main())
