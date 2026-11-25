#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run one make target in every repo that has it. Main-based; logic in podmanlib.

    python make_all.py [target]      default target: help

Repos without a Makefile or without the target are reported as skipped.
PODMAN_ENV_REPOS=podman,skopeo limits the set of repos.
Exit status is 1 if any repo failed.
"""
from __future__ import print_function

import sys

import podmanlib


def main(argv=None):
    argv = sys.argv if argv is None else argv
    target = argv[1] if len(argv) > 1 else "help"
    if target.startswith("-"):
        print("usage: python make_all.py [target]", file=sys.stderr)
        return 2
    env = podmanlib.PodmanEnvironment.instance()
    env.bus.attach(podmanlib.ConsoleObserver())
    outcomes = env.make(target)
    return 1 if podmanlib.any_failed(outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
