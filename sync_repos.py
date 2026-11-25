#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Sync the upstream repos under repos/. Main-based; the logic is in podmanlib.

    python sync_repos.py pin      check out the commits this repo records
    python sync_repos.py latest   fetch upstream and move each repo to origin/HEAD

PODMAN_ENV_REPOS=podman,skopeo limits the set of repos.
Exit status is 1 if any repo failed.
"""
from __future__ import print_function

import sys

import podmanlib

MODES = {
    "pin": lambda env: env.pin(),
    "latest": lambda env: env.latest(),
}


def main(argv=None):
    argv = sys.argv if argv is None else argv
    mode = podmanlib.command_name(argv, MODES)
    if mode is None:
        print("usage: python sync_repos.py {pin|latest}", file=sys.stderr)
        return 2
    env = podmanlib.PodmanEnvironment.instance()
    env.bus.attach(podmanlib.ConsoleObserver())
    outcomes = MODES[mode](env)
    if mode == "latest":
        print("Moved repos show as 'moved' in repo_status.py. To pin the new "
              "versions, run: git add repos/<name> && git commit")
    return 1 if podmanlib.any_failed(outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
