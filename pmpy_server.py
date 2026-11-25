#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pmpy_server: a small container engine on a unix socket. Main-based; logic in podmanlib (part 10).

    pmpy_server.py serve SOCKET                  serve the engine API on SOCKET
    pmpy_server.py image-import NAME ROOTFS      make a rootfs directory an image
    pmpy_server.py images                        list the images

Options: --home DIR     state and rootfs store (default $PMPY_HOME, /var/lib/pmpy for root,
                        else ~/.local/share/pmpy)
         --runtime PATH OCI runtime (default: crun, then runc)

What it does not do yet: pull, networking, ports, pods, restart, attach. Those answer 501.
Exit status: 0 ok, 1 error, 2 usage.
"""
from __future__ import print_function

import os
import sys

import podmanlib as pl


def default_home():
    if os.environ.get("PMPY_HOME"):
        return os.environ["PMPY_HOME"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return "/var/lib/pmpy"
    return os.path.join(os.path.expanduser("~"), ".local", "share", "pmpy")


def main(argv=None):
    argv = sys.argv if argv is None else argv
    try:
        options, args = pl.take_flags(argv[1:], {"--home": "value", "--runtime": "value"})
    except pl.CliUsageError as exc:
        sys.stderr.write("%s\n" % exc)
        return pl.EXIT_USAGE
    home = options.get("--home") or default_home()
    if not args:
        sys.stderr.write(__doc__.split("\n\n")[0] + "\n")
        return pl.EXIT_USAGE
    command, rest = args[0], args[1:]
    try:
        engine = pl.EngineServer(home, runtime_path=options.get("--runtime") or None)
        if command == "image-import" and len(rest) == 2:
            ref = engine.image_import(rest[0], rest[1])
            sys.stdout.write(ref + "\n")
            return pl.EXIT_OK
        if command == "images" and not rest:
            for ref, image in sorted(engine.state["images"].items()):
                sys.stdout.write("%s\t%s\n" % (ref, image["Id"][:12]))
            return pl.EXIT_OK
        if command == "serve" and len(rest) == 1:
            server = pl.EngineUnixServer(os.path.abspath(rest[0]), engine)
            sys.stderr.write("pmpy engine listening on %s (home %s)\n" % (rest[0], home))
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return pl.EXIT_OK
    except pl.ServerError as exc:
        sys.stderr.write("Error: %s\n" % exc.message)
        return pl.EXIT_ERROR
    except ValueError as exc:
        sys.stderr.write("Error: %s\n" % exc)
        return pl.EXIT_ERROR
    sys.stderr.write("usage: pmpy_server.py serve SOCKET | image-import NAME ROOTFS | images\n")
    return pl.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
