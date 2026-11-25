#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run one command in a root filesystem with an OCI runtime (crun first, runc second).

    python3 oci_run.py ROOTFS COMMAND [ARGS...]

Builds a bundle (config.json and a link to ROOTFS) in a temporary directory,
runs the command through the runtime, prints its output and removes the bundle.
The runtime can be chosen with PMPY_RUNTIME=/path/to/crun.
Exit status: the command's status, 1 for a runtime or setup error, 2 for usage.
"""
from __future__ import print_function

import os
import shutil
import sys
import tempfile
import uuid

import podmanlib as pl


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) < 3:
        sys.stderr.write("usage: oci_run.py ROOTFS COMMAND [ARGS...]\n")
        return pl.EXIT_USAGE
    rootfs = os.path.abspath(argv[1])
    command = argv[2:]
    if not os.path.isdir(rootfs):
        sys.stderr.write("no root filesystem at %s\n" % rootfs)
        return pl.EXIT_ERROR
    try:
        runtime = pl.OCIRuntime(os.environ.get("PMPY_RUNTIME") or None)
    except pl.OCIRuntimeError as exc:
        sys.stderr.write("%s\n" % exc)
        return pl.EXIT_ERROR
    bundle = tempfile.mkdtemp(prefix="pmpy-bundle-")
    container_id = "pmpy-" + uuid.uuid4().hex[:12]
    try:
        os.symlink(rootfs, os.path.join(bundle, "rootfs"))
        pl.write_bundle(bundle, pl.make_spec(command))
        code, out, err = runtime.run(container_id, bundle)
        sys.stdout.write(out)
        sys.stdout.flush()
        sys.stderr.write(err)
        return code
    finally:
        shutil.rmtree(bundle, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
