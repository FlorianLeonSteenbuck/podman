#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Parse image references the way container-libs does and print the parts.

    python image_reference.py alpine:3.20 Ubuntu localhost:5000/team/app@sha256:<64 hex>

Main-based; the logic is in podmanlib (Part 4, image/docker/reference).
"""
from __future__ import print_function

import sys

import podmanlib


def describe(text):
    try:
        named = podmanlib.ParseNormalizedNamed(text)
        docker = podmanlib.ParseDockerRef(text)
    except ValueError as exc:
        return ["error: " + str(exc)]
    return [
        "normalized: " + named.String(),
        "familiar:   " + podmanlib.FamiliarString(named),
        "docker ref: " + docker.String(),
        "domain:     " + podmanlib.Domain(named),
        "path:       " + podmanlib.Path(named),
    ]


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) < 2:
        print("usage: python image_reference.py <reference> [<reference> ...]", file=sys.stderr)
        return 2
    for text in argv[1:]:
        print("== " + text)
        for line in describe(text):
            print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
