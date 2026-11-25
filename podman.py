#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""podman, as far as this Python rewrite goes. Main-based; the logic is in podmanlib.

Implemented:
    python3 podman.py version
    python3 podman.py image parse REF
    python3 podman.py manifest guess FILE
    python3 podman.py manifest digest FILE
    python3 podman.py manifest choose FILE [--arch A] [--os O] [--variant V] [--prefer-gzip BOOL]
    python3 podman.py machine connection list
    python3 podman.py machine connection add-pair NAME UID PORT USER IDENTITY
    python3 podman.py machine connection remove NAME [NAME ...]

Every other podman command is known by name and exits with status 3, so that
scripts can tell "not ported yet" from "wrong usage" (status 2) and from a
failed operation (status 1).

Exit status: 0 ok, 1 operation failed, 2 usage error, 3 not implemented.
"""
from __future__ import print_function

import io
import json
import sys

import image_reference
import machine_connections
import podmanlib as pl

PROGRAM = "podman"
VERSION = "0.1.0-rewrite"

NOT_IMPLEMENTED = {
    "pull": "tier 1, image fetch",
    "images": "tier 1, image fetch",
    "rmi": "tier 1, image fetch",
    "run": "tier 2, run a container",
    "create": "tier 2, run a container",
    "start": "tier 2, run a container",
    "stop": "tier 2, run a container",
    "rm": "tier 2, run a container",
    "ps": "tier 2, run a container",
    "logs": "tier 2, run a container",
    "inspect": "tier 2, run a container",
    "exec": "tier 4, later",
    "build": "out of scope: image build is buildah",
    "machine init": "not in the roadmap yet: machine start",
    "machine start": "not in the roadmap yet: machine start",
    "system service": "not in the roadmap yet: the machine daemon",
}


def cmd_version(args):
    sys.stdout.write("%s version %s (python rewrite)\n" % (PROGRAM, VERSION))
    return pl.EXIT_OK


def cmd_image_parse(args):
    if len(args) != 1:
        raise pl.CliUsageError("usage: podman image parse REF")
    return image_reference.main(["image_reference.py", args[0]])


def read_blob(path):
    try:
        with io.open(path, "rb") as fh:
            return fh.read()
    except (IOError, OSError) as exc:
        sys.stderr.write("%s: %s\n" % (path, exc))
        return None


def cmd_manifest_guess(args):
    if len(args) != 1:
        raise pl.CliUsageError("usage: podman manifest guess FILE")
    blob = read_blob(args[0])
    if blob is None:
        return pl.EXIT_ERROR
    mime = pl.GuessMIMEType(blob)
    sys.stdout.write((mime or "unknown") + "\n")
    return pl.EXIT_OK


def cmd_manifest_digest(args):
    if len(args) != 1:
        raise pl.CliUsageError("usage: podman manifest digest FILE")
    blob = read_blob(args[0])
    if blob is None:
        return pl.EXIT_ERROR
    try:
        digest = pl.Digest(blob)
    except NotImplementedError as exc:
        return pl.not_implemented(PROGRAM, "manifest digest (%s)" % exc)
    sys.stdout.write(digest + "\n")
    return pl.EXIT_OK


def cmd_manifest_choose(args):
    options, positional = pl.take_flags(args, {"--arch": "value", "--os": "value",
                                               "--variant": "value", "--prefer-gzip": "bool"})
    if len(positional) != 1:
        raise pl.CliUsageError("usage: podman manifest choose FILE [--arch A] [--os O] "
                               "[--variant V] [--prefer-gzip BOOL]")
    blob = read_blob(positional[0])
    if blob is None:
        return pl.EXIT_ERROR
    ctx = {"ArchitectureChoice": options.get("--arch", ""),
           "OSChoice": options.get("--os", ""),
           "VariantChoice": options.get("--variant", "")}
    prefer = pl.OPTIONAL_BOOL_TRUE if options.get("--prefer-gzip") else pl.OPTIONAL_BOOL_FALSE
    mime = pl.GuessMIMEType(blob)
    try:
        document = json.loads(blob.decode("utf-8", "replace"))
        manifests = document.get("manifests") or []
        if mime == pl.OCI1_INDEX_MEDIA_TYPE:
            digest = pl.OCI1IndexChooseInstance(manifests, ctx, prefer)
        elif mime == pl.DOCKER_V2_LIST_MEDIA_TYPE:
            digest = pl.Schema2ListChooseInstance(manifests, ctx)
        else:
            sys.stderr.write("not an image index or manifest list (media type %s)\n"
                             % (mime or "unknown"))
            return pl.EXIT_ERROR
    except (ValueError, pl.ManifestError) as exc:
        sys.stderr.write("%s\n" % exc)
        return pl.EXIT_ERROR
    sys.stdout.write(digest + "\n")
    return pl.EXIT_OK


def cmd_machine_connection(args):
    if not args or args[0] not in ("list", "add-pair", "remove"):
        raise pl.CliUsageError("usage: podman machine connection {list|add-pair|remove} ...")
    return machine_connections.main(["machine_connections.py"] + args)


COMMANDS = {
    "version": cmd_version,
    "image parse": cmd_image_parse,
    "manifest guess": cmd_manifest_guess,
    "manifest digest": cmd_manifest_digest,
    "manifest choose": cmd_manifest_choose,
    "machine connection": cmd_machine_connection,
}


def usage():
    lines = ["usage: podman.py <command> [args]", "", "implemented:"]
    lines += ["  " + name for name in sorted(COMMANDS)]
    lines += ["", "not implemented yet (exit status 3):"]
    lines += ["  " + name for name in sorted(NOT_IMPLEMENTED)]
    return "\n".join(lines) + "\n"


def command_key(args):
    """The command name, two words for image, manifest, machine and system."""
    if args[0] in ("image", "manifest", "machine", "system"):
        return " ".join(args[:2]), args[2:]
    return args[0], args[1:]


def main(argv=None):
    argv = sys.argv if argv is None else argv
    args = argv[1:]
    if not args or args[0] in ("help", "-h", "--help"):
        sys.stdout.write(usage())
        return pl.EXIT_OK
    key, rest = command_key(args)
    try:
        if key in COMMANDS:
            return COMMANDS[key](rest)
        if key in NOT_IMPLEMENTED:
            return pl.not_implemented(PROGRAM, key, NOT_IMPLEMENTED[key])
        raise pl.CliUsageError("unknown command %r" % key)
    except pl.CliUsageError as exc:
        sys.stderr.write("%s\n" % exc)
        return pl.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
