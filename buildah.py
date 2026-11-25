#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""buildah, as far as this Python rewrite goes. Main-based.

Image build is out of scope for the rewrite (see docs/ROADMAP.md), so every
command except version exits with status 3.

    python3 buildah.py version
    python3 buildah.py build|bud|from|commit|push|... (out of scope, exit 3)
"""
from __future__ import print_function

import sys

import podmanlib as pl

PROGRAM = "buildah"
VERSION = "0.1.0-rewrite"


def main(argv=None):
    argv = sys.argv if argv is None else argv
    args = argv[1:]
    if not args or args[0] in ("help", "-h", "--help"):
        sys.stdout.write("usage: buildah.py version | <any buildah command, out of scope>\n")
        return pl.EXIT_OK
    if args[0] == "version":
        sys.stdout.write("%s version %s (python rewrite)\n" % (PROGRAM, VERSION))
        return pl.EXIT_OK
    return pl.not_implemented(PROGRAM, args[0], "out of scope: image build")


if __name__ == "__main__":
    sys.exit(main())
