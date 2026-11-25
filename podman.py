#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""podman, as far as this Python rewrite goes. Main-based; the logic is in podmanlib.

Local commands (no engine needed):
    podman.py version
    podman.py image parse REF
    podman.py manifest guess|digest FILE
    podman.py manifest choose FILE [--arch A] [--os O] [--variant V] [--prefer-gzip BOOL]
    podman.py machine connection list|add-pair|remove ...
    podman.py system connection add [--default] NAME URI | rm NAME | list

Remote commands: they talk to a podman service over a unix socket, the way
`podman --remote` does. The endpoint comes from, in this order:
    --url URL, -c/--connection NAME, $CONTAINER_HOST, or --remote with the default connection.
    ps [-a] [--format F] | images [--format F] | pull [-q] REF | image exists NAME
    inspect NAME [--format F] | start|stop|restart NAME... | rm [-f] NAME...
    rmi [-f] NAME... | logs NAME | version [--format F]

Everything else is known by name and exits with status 3 (not implemented).
Exit status: 0 ok, 1 operation failed, 2 usage error, 3 not implemented.
"""
from __future__ import print_function

import io
import json
import os
import sys

import image_reference
import machine_connections
import podmanlib as pl

PROGRAM = "podman"
VERSION = "0.1.0-rewrite"

LOCAL = {
    "image parse": "image parse",
    "manifest guess": "manifest guess",
    "manifest digest": "manifest digest",
    "manifest choose": "manifest choose",
    "machine connection": "machine connection",
    "system connection": "system connection",
}

REMOTE = ("ps", "images", "pull", "image exists", "inspect", "start", "stop", "restart",
          "rm", "rmi", "logs", "version", "run", "pod create", "pod ps", "pod start",
          "pod stop", "pod rm")

NOT_IMPLEMENTED = {
    "create": "tier 2, run a container",
    "exec": "tier 4, later",
    "build": "out of scope: image build is buildah",
    "machine init": "not in the roadmap yet: machine start",
    "machine start": "not in the roadmap yet: machine start",
    "system service": "not in the roadmap yet: the machine daemon",
}

GLOBAL_VALUE_FLAGS = ("--connection", "-c", "--url")


# --- global flags and the endpoint ------------------------------------------------


def split_global(args):
    """Leading global flags. Returns (options, the rest of the arguments)."""
    options = {"remote": False, "connection": None, "url": None}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--remote", "-r"):
            options["remote"] = True
        elif arg in GLOBAL_VALUE_FLAGS:
            if i + 1 >= len(args):
                raise pl.CliUsageError("%s needs a value" % arg)
            i += 1
            key = "connection" if arg in ("--connection", "-c") else "url"
            options[key] = args[i]
        elif arg.startswith("--connection=") or arg.startswith("--url="):
            key, _, value = arg[2:].partition("=")
            options[key] = value
        else:
            break
        i += 1
    return options, args[i:]


def connection_uri(name):
    cfg = pl.read_connection_conf(pl.connections_config_path())
    conns = cfg.connections or {}
    if name not in conns:
        raise pl.MachineConnectionError('unable to find connection named "%s"' % name)
    return conns[name].get("URI", "")


def resolve_endpoint(options):
    """The endpoint URI for remote commands, or None for the local engine."""
    if options["url"]:
        return options["url"]
    if options["connection"]:
        return connection_uri(options["connection"])
    if os.environ.get("CONTAINER_HOST"):
        return os.environ["CONTAINER_HOST"]
    if options["remote"]:
        cfg = pl.read_connection_conf(pl.connections_config_path())
        if not cfg.default:
            raise pl.CliUsageError("--remote needs CONTAINER_HOST, -c NAME or a default connection")
        return connection_uri(cfg.default)
    return None


# --- local commands ----------------------------------------------------------------


def cmd_version_local(args):
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
    sys.stdout.write((pl.GuessMIMEType(blob) or "unknown") + "\n")
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


def cmd_system_connection(args):
    if args[:1] == ["add"]:
        options, rest = pl.take_flags(args[1:], {"--default": "bool"})
        if len(rest) != 2:
            raise pl.CliUsageError("usage: podman system connection add [--default] NAME URI")
        name, uri = rest

        def add(cfg):
            if cfg.connections is None:
                cfg.set_connections({})
            cfg.connections[name] = pl.make_destination(uri)
            if options.get("--default"):
                cfg.default = name
        pl.EditConnectionConfig(add)
        return pl.EXIT_OK
    if args[:1] == ["rm"]:
        if len(args) != 2:
            raise pl.CliUsageError("usage: podman system connection rm NAME")
        name = args[1]

        def remove(cfg):
            if not cfg.has_connection(name):
                raise pl.MachineConnectionError('unable to find connection named "%s"' % name)
            del cfg.connections[name]
            if cfg.default == name:
                cfg.default = ""
        pl.EditConnectionConfig(remove)
        return pl.EXIT_OK
    if args[:1] in (["list"], ["ls"]) or not args:
        cfg = pl.read_connection_conf(pl.connections_config_path())
        rows = []
        for name in sorted(cfg.connections or {}):
            dest = cfg.connections[name]
            rows.append([name, dest.get("URI", ""), "*" if name == cfg.default else ""])
        sys.stdout.write(pl.format_table(["NAME", "URI", "DEFAULT"], rows) if rows else "")
        return pl.EXIT_OK
    raise pl.CliUsageError("usage: podman system connection {add|rm|list} ...")


LOCAL_FUNCTIONS = {
    "image parse": cmd_image_parse,
    "manifest guess": cmd_manifest_guess,
    "manifest digest": cmd_manifest_digest,
    "manifest choose": cmd_manifest_choose,
    "machine connection": cmd_machine_connection,
    "system connection": cmd_system_connection,
}


# --- remote commands (podman --remote) ---------------------------------------------


def normalize_short(args, mapping):
    """Turn short flags such as -a into their long form, so take_flags can parse them."""
    return [mapping.get(arg, arg) for arg in args]


def status_text(item):
    """Podman's status column: "Up 2 minutes", "Exited (0) 3 seconds ago", or the state."""
    state = item.get("State", "")
    if state == "running":
        return "Up " + pl.time_ago(pl.epoch_of(item.get("StartedAt")))[:-4] if item.get("StartedAt") else "Up"
    if item.get("Exited") or state == "exited":
        return "Exited (%s) %s" % (item.get("ExitCode", 0), pl.time_ago(pl.epoch_of(item.get("ExitedAt"))))
    return item.get("Status", "") or state


def ports_text(ports):
    """0.0.0.0:18081->80/tcp, as podman prints it."""
    parts = []
    for port in ports or []:
        host_ip = port.get("host_ip") or "0.0.0.0"
        parts.append("%s:%s->%s/%s" % (host_ip, port.get("host_port", ""),
                                      port.get("container_port", ""), port.get("protocol", "tcp")))
    return ", ".join(parts)


def container_view(item):
    """The fields of one container, under the names podman uses in --format."""
    names = item.get("Names") or []
    command = item.get("Command") or []
    return {
        "ID": item.get("Id", ""),
        "Image": item.get("Image", ""),
        "Command": " ".join(command) if isinstance(command, list) else str(command),
        "Created": pl.time_ago(pl.epoch_of(item.get("Created"))),
        "Status": status_text(item),
        "Ports": ports_text(item.get("Ports")),
        "Names": names[0] if names else "",
        "Pod": item.get("PodName", "") or "",
    }


def image_view(item):
    tags = item.get("RepoTags") or item.get("Names") or []
    repository, tag = "<none>", "<none>"
    if tags:
        first = tags[0]
        if ":" in first.rsplit("/", 1)[-1]:
            repository, tag = first.rsplit(":", 1)
        else:
            repository = first
    return {
        "ID": (item.get("Id") or "")[:12],
        "Repository": repository,
        "Tag": tag,
        "Created": pl.time_ago(item.get("Created")),
        "Size": pl.human_size(item.get("Size")),
    }


def cmd_ps(client, args):
    options, _ = pl.take_flags(normalize_short(args, {"-a": "--all"}),
                               {"--all": "bool", "--format": "value"})
    rows = client.list_containers(all_containers=options.get("--all", False))
    views = [container_view(item) for item in rows]
    if "--format" in options:
        for view in views:
            sys.stdout.write(pl.format_template(options["--format"], view) + "\n")
    else:
        headers = ["CONTAINER ID", "IMAGE", "COMMAND", "CREATED", "STATUS", "PORTS", "NAMES"]
        table = [[v["ID"][:12], v["Image"], v["Command"], v["Created"], v["Status"],
                  v["Ports"], v["Names"]] for v in views]
        sys.stdout.write(pl.format_table(headers, table))
    return pl.EXIT_OK


def cmd_images(client, args):
    options, _ = pl.take_flags(args, {"--format": "value"})
    views = [image_view(item) for item in client.list_images()]
    if "--format" in options:
        for view in views:
            sys.stdout.write(pl.format_template(options["--format"], view) + "\n")
    else:
        headers = ["REPOSITORY", "TAG", "IMAGE ID", "CREATED", "SIZE"]
        table = [[v["Repository"], v["Tag"], v["ID"], str(v["Created"]), str(v["Size"])]
                 for v in views]
        sys.stdout.write(pl.format_table(headers, table))
    return pl.EXIT_OK


def cmd_pull(client, args):
    options, rest = pl.take_flags(normalize_short(args, {"-q": "--quiet"}),
                                  {"--quiet": "bool"})
    if len(rest) != 1:
        raise pl.CliUsageError("usage: podman pull [-q] REFERENCE")
    report = client.pull(rest[0], quiet=True)
    image_id = report.get("id") or report.get("ID") or ""
    if not image_id:
        images = report.get("images") or report.get("Images") or []
        image_id = images[-1] if images else ""
    sys.stdout.write(image_id + "\n")
    return pl.EXIT_OK


def cmd_image_exists(client, args):
    if len(args) != 1:
        raise pl.CliUsageError("usage: podman image exists NAME")
    return pl.EXIT_OK if client.image_exists(args[0]) else pl.EXIT_ERROR


def cmd_inspect(client, args):
    options, rest = pl.take_flags(args, {"--format": "value", "--type": "value"})
    if len(rest) != 1:
        raise pl.CliUsageError("usage: podman inspect [--format F] NAME")
    name = rest[0]
    wanted = options.get("--type", "")
    data = None
    if wanted in ("", "container"):
        try:
            data = client.inspect_container(name)
        except pl.EngineError as exc:
            if exc.status != 404 or wanted == "container":
                raise
    if data is None:
        try:
            data = client.inspect_image(name)
        except pl.EngineError as exc:
            if exc.status == 404:
                raise pl.EngineError(404, "no such object: %s" % name)
            raise
    if "--format" in options:
        sys.stdout.write(pl.format_template(options["--format"], data) + "\n")
    else:
        sys.stdout.write(json.dumps(data, indent=4, sort_keys=True) + "\n")
    return pl.EXIT_OK


def names_of(args, usage):
    if not args:
        raise pl.CliUsageError(usage)
    return args


def cmd_container_action(action):
    def run(client, args):
        options, names = pl.take_flags(normalize_short(args, {"-t": "--time"}),
                                       {"--time": "value"})
        for name in names_of(names, "usage: podman %s NAME..." % action):
            query = {"timeout": options["--time"]} if "--time" in options else None
            client.container_action(name, action, query)
            sys.stdout.write(name + "\n")
        return pl.EXIT_OK
    return run


def cmd_rm(client, args):
    options, names = pl.take_flags(normalize_short(args, {"-f": "--force"}),
                                   {"--force": "bool"})
    for name in names_of(names, "usage: podman rm [-f] NAME..."):
        client.remove_container(name, force=options.get("--force", False))
        sys.stdout.write(name + "\n")
    return pl.EXIT_OK


def cmd_rmi(client, args):
    options, names = pl.take_flags(normalize_short(args, {"-f": "--force"}),
                                   {"--force": "bool"})
    for name in names_of(names, "usage: podman rmi [-f] NAME..."):
        client.remove_image(name, force=options.get("--force", False))
        sys.stdout.write(name + "\n")
    return pl.EXIT_OK


def write_stream(chunks):
    """(stream, bytes) pairs to the terminal: stdout for 1, stderr for 2. The bytes are
    written as they are, so a multi-byte character split across two chunks survives."""
    for stream, payload in chunks:
        sys.stdout.flush()
        sys.stderr.flush()
        target = sys.stderr if stream == 2 else sys.stdout
        target = getattr(target, "buffer", target)
        target.write(payload)
        target.flush()


def write_frames(data):
    """Stdout frames go to stdout, stderr frames to stderr, as podman logs does."""
    write_stream(pl.demux_frames(data))


def cmd_logs(client, args):
    options, names = pl.take_flags(normalize_short(args, {"-f": "--follow"}),
                                   {"--follow": "bool"})
    if len(names) != 1:
        raise pl.CliUsageError("usage: podman logs [-f] NAME")
    if options.get("--follow", False):
        write_stream(client.stream_logs(names[0], follow=True))
    else:
        write_frames(client.container_logs(names[0]))
    return pl.EXIT_OK


def cmd_version_remote(client, args):
    options, _ = pl.take_flags(args, {"--format": "value"})
    info = client.version() or {}
    view = {"Client": {"Version": VERSION}, "Server": {"Version": pl.engine_version(info)}}
    if "--format" in options:
        sys.stdout.write(pl.format_template(options["--format"], view) + "\n")
    else:
        sys.stdout.write("Client:\n  Version: %s\nServer:\n  Version: %s\n"
                         % (view["Client"]["Version"], view["Server"]["Version"]))
    return pl.EXIT_OK


def parse_run(args):
    """podman run [-d] [--rm] [--name N] [-e K=V] [-p H:C] [-v H:C[:ro]] [-w DIR]
    [--pod P] [--restart POLICY] [--network host|none] IMAGE [CMD...]"""
    spec = {"detach": False, "rm": False, "name": None, "env": {}, "ports": [],
            "volumes": [], "workdir": None, "pod": None, "restart": None, "network": None}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg in ("-d", "--detach"):
            spec["detach"] = True
        elif arg == "--rm":
            spec["rm"] = True
        elif arg in ("--name", "-e", "--env", "-p", "--publish", "-v", "--volume", "-w", "--workdir",
                     "--pod", "--restart", "--network"):
            if i + 1 >= len(args):
                raise pl.CliUsageError("%s needs a value" % arg)
            i += 1
            value = args[i]
            if arg == "--name":
                spec["name"] = value
            elif arg == "--pod":
                spec["pod"] = value
            elif arg == "--restart":
                spec["restart"] = value
            elif arg == "--network":
                spec["network"] = value
            elif arg in ("-e", "--env"):
                if "=" in value:
                    key, _, val = value.partition("=")
                    spec["env"][key] = val
                else:
                    spec["env"][value] = os.environ.get(value, "")
            elif arg in ("-p", "--publish"):
                spec["ports"].append(publish_entry(value))
            elif arg in ("-v", "--volume"):
                spec["volumes"].append(volume_entry(value))
            else:
                spec["workdir"] = value
        else:
            raise pl.CliUsageError("unsupported run option %s" % arg)
        i += 1
    if i >= len(args):
        raise pl.CliUsageError("usage: podman run [options] IMAGE [COMMAND...]")
    spec["image"] = args[i]
    spec["command"] = args[i + 1:]
    return spec


def publish_entry(value):
    """H:C, IP:H:C, each with an optional /protocol; the API's portmappings format."""
    protocol = "tcp"
    if "/" in value:
        value, _, protocol = value.rpartition("/")
    parts = value.split(":")
    if len(parts) == 3:
        host_ip, host_port, container_port = parts
    elif len(parts) == 2:
        host_ip, host_port, container_port = "", parts[0], parts[1]
    else:
        raise pl.CliUsageError("bad -p value %r" % value)
    entry = {"host_port": int(host_port), "container_port": int(container_port),
             "protocol": protocol}
    if host_ip:
        entry["host_ip"] = host_ip
    return entry


def volume_entry(value):
    """H:C or H:C:ro|rw, as a bind mount."""
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise pl.CliUsageError("bad -v value %r" % value)
    options = ["rw"] if len(parts) == 2 else [parts[2]]
    return {"type": "bind", "source": parts[0], "destination": parts[1], "options": options}


def cmd_run(client, args):
    spec = parse_run(args)
    body = {"image": spec["image"], "command": spec["command"] or None,
            "name": spec["name"], "env": spec["env"] or None,
            "portmappings": spec["ports"] or None, "mounts": spec["volumes"] or None,
            "work_dir": spec["workdir"], "pod": spec["pod"], "network": spec["network"],
            "restart_policy": spec["restart"]}
    body = dict((k, v) for k, v in body.items() if v is not None)
    if not client.image_exists(spec["image"]):
        client.pull(spec["image"])  # pull=missing: as podman does by default
    container = client.create_container(body)
    client.container_action(container, "start")
    if spec["detach"]:
        sys.stdout.write(container + "\n")
        return pl.EXIT_OK
    # attached: the output comes while the container runs, then its exit status
    write_stream(client.stream_logs(container, follow=True))
    code = client.wait_container(container, "stopped")
    if spec["rm"]:
        client.remove_container(container, force=True)
    return code


def parse_pod_create(args):
    """podman pod create [--name N] [-p H:C]..."""
    spec = {"name": None, "ports": []}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--name", "-p", "--publish"):
            if i + 1 >= len(args):
                raise pl.CliUsageError("%s needs a value" % arg)
            i += 1
            if arg == "--name":
                spec["name"] = args[i]
            else:
                spec["ports"].append(publish_entry(args[i]))
        else:
            raise pl.CliUsageError("unsupported pod create option %s" % arg)
        i += 1
    return spec


def cmd_pod_create(client, args):
    spec = parse_pod_create(args)
    body = {"name": spec["name"], "portmappings": spec["ports"] or None}
    body = dict((k, v) for k, v in body.items() if v is not None)
    sys.stdout.write(client.create_pod(body) + "\n")
    return pl.EXIT_OK


def pod_view(item):
    """The fields of one pod, under the names podman uses in --format."""
    containers = item.get("Containers") or []
    return {
        "ID": item.get("Id", ""),
        "Name": item.get("Name", ""),
        "Status": item.get("Status", ""),
        "Created": pl.time_ago(pl.epoch_of(item.get("Created"))),
        "NumberOfContainers": str(len(containers)),
        "Ports": ports_text(item.get("Ports")),
    }


def cmd_pod_ps(client, args):
    options, _ = pl.take_flags(args, {"--format": "value"})
    views = [pod_view(item) for item in client.list_pods()]
    if "--format" in options:
        for view in views:
            sys.stdout.write(pl.format_template(options["--format"], view) + "\n")
    else:
        headers = ["POD ID", "NAME", "STATUS", "CREATED", "# OF CONTAINERS", "PORTS"]
        table = [[v["ID"][:12], v["Name"], v["Status"], v["Created"],
                  v["NumberOfContainers"], v["Ports"]] for v in views]
        sys.stdout.write(pl.format_table(headers, table))
    return pl.EXIT_OK


def cmd_pod_action(action):
    def run(client, args):
        options, names = pl.take_flags(normalize_short(args, {"-t": "--time"}),
                                       {"--time": "value"})
        for name in names_of(names, "usage: podman pod %s NAME..." % action):
            query = {"timeout": options["--time"]} if "--time" in options else None
            client.pod_action(name, action, query)
            sys.stdout.write(name + "\n")
        return pl.EXIT_OK
    return run


def cmd_pod_rm(client, args):
    options, names = pl.take_flags(normalize_short(args, {"-f": "--force"}),
                                   {"--force": "bool"})
    for name in names_of(names, "usage: podman pod rm [-f] NAME..."):
        client.remove_pod(name, force=options.get("--force", False))
        sys.stdout.write(name + "\n")
    return pl.EXIT_OK


REMOTE_FUNCTIONS = {
    "run": cmd_run,
    "ps": cmd_ps,
    "images": cmd_images,
    "pull": cmd_pull,
    "image exists": cmd_image_exists,
    "inspect": cmd_inspect,
    "start": cmd_container_action("start"),
    "stop": cmd_container_action("stop"),
    "restart": cmd_container_action("restart"),
    "rm": cmd_rm,
    "rmi": cmd_rmi,
    "logs": cmd_logs,
    "version": cmd_version_remote,
    "pod create": cmd_pod_create,
    "pod ps": cmd_pod_ps,
    "pod start": cmd_pod_action("start"),
    "pod stop": cmd_pod_action("stop"),
    "pod rm": cmd_pod_rm,
}


# --- dispatch ---------------------------------------------------------------------


def usage():
    lines = ["usage: podman.py [--remote | -c NAME | --url URL] <command> [args]", "",
             "local:"]
    lines += ["  " + name for name in sorted(LOCAL_FUNCTIONS)]
    lines += ["", "remote (needs CONTAINER_HOST, -c, --url or --remote):"]
    lines += ["  " + name for name in sorted(REMOTE_FUNCTIONS)]
    lines += ["", "not implemented yet (exit status 3):"]
    lines += ["  " + name for name in sorted(NOT_IMPLEMENTED)]
    return "\n".join(lines) + "\n"


def command_key(args):
    if args[0] in ("image", "manifest", "machine", "system", "pod"):
        return " ".join(args[:2]), args[2:]
    return args[0], args[1:]


def run_command(args):
    options, rest = split_global(args)
    if not rest or rest[0] in ("help", "-h", "--help"):
        sys.stdout.write(usage())
        return pl.EXIT_OK
    key, cmd_args = command_key(rest)
    if key in LOCAL_FUNCTIONS:
        return LOCAL_FUNCTIONS[key](cmd_args)
    if key in REMOTE_FUNCTIONS or key == "version":
        endpoint = resolve_endpoint(options)
        if key == "version" and endpoint is None:
            return cmd_version_local(cmd_args)
        if endpoint is None:
            return pl.not_implemented(PROGRAM, key,
                                      "the local engine; use CONTAINER_HOST, -c NAME or --url")
        client = pl.EngineClient(pl.socket_path_of(endpoint))
        return REMOTE_FUNCTIONS[key](client, cmd_args)
    if key in NOT_IMPLEMENTED:
        return pl.not_implemented(PROGRAM, key, NOT_IMPLEMENTED[key])
    raise pl.CliUsageError("unknown command %r" % key)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    try:
        return run_command(argv[1:])
    except pl.CliUsageError as exc:
        sys.stderr.write("%s\n" % exc)
        return pl.EXIT_USAGE
    except pl.EngineError as exc:
        sys.stderr.write("Error: %s\n" % exc.message)
        return pl.EXIT_ERROR
    except (pl.MachineConnectionError, pl.ManifestError, ValueError) as exc:
        sys.stderr.write("Error: %s\n" % exc)
        return pl.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
