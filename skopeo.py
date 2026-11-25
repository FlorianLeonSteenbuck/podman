#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""skopeo, as far as this Python rewrite goes. Main-based; logic in podmanlib.

    python3 skopeo.py version
    python3 skopeo.py copy|inspect|sync|delete|login ...   (not implemented yet, exit 3)

Image references are parsed by podman.py (podman image parse REF), which uses
the same code as skopeo's reference handling.
"""
from __future__ import print_function

import sys

import podmanlib as pl

PROGRAM = "skopeo"
VERSION = "0.1.0-rewrite"
NOT_IMPLEMENTED = {
    "copy": "tier 1, image copy (1.8)",
    "inspect": "tier 1, registry client (1.3)",
    "sync": "tier 1, image copy (1.8)",
    "delete": "tier 1, registry client (1.3)",
    "login": "tier 1, registry client (1.3)",
}


def main(argv=None):
    argv = sys.argv if argv is None else argv
    args = argv[1:]
    if not args or args[0] in ("help", "-h", "--help"):
        sys.stdout.write("usage: skopeo.py version | %s\n" % " | ".join(sorted(NOT_IMPLEMENTED)))
        return pl.EXIT_OK
    if args[0] == "version":
        sys.stdout.write("%s version %s (python rewrite)\n" % (PROGRAM, VERSION))
        return pl.EXIT_OK
    if args[0] in NOT_IMPLEMENTED:
        return pl.not_implemented(PROGRAM, args[0], NOT_IMPLEMENTED[args[0]])
    sys.stderr.write("unknown command %r\n" % args[0])
    return pl.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
