# podman in Python (rewrite)

This repository is a Python rewrite of Go code from the podman-container-tools
projects. It does not contain, bind, build or run those Go projects. The Go
sources are only the reference the Python code is checked against.

The full rewrite is a multi-stage effort. [docs/ROADMAP.md](docs/ROADMAP.md)
lists the pieces needed to run containers, with line counts and the order.

## Status

| Piece (Go source)                                           | Status |
|-------------------------------------------------------------|--------|
| `golang.org/x/mod/semver` (vendored in container-libs)      | ported, cross-checked |
| `image/docker/reference` (container-libs)                   | ported, cross-checked |
| go-digest validation                                        | ported, cross-checked |
| go.mod reader                                               | own code |
| Machine connections: `pkg/machine/connection`, `common/pkg/config/connections.go` (podman) | ported, cross-checked; file format and lock as Go |
| Image manifests: `image/internal/manifest`, `image/internal/pkg/platform` (container-libs) | partly: media types, digest, platform choice, index and list choice; cross-checked |

Not yet ported:

- In the reference package: `FamiliarMatch` (needs Go's `path.Match`) and the `Field` encoding helper.
- In the manifest code: parsing of schema2 and OCI manifests and configs, and digests of schema 1 signed manifests.
- Opening the ssh connection itself (`common/pkg/ssh`), the machine daemon, and everything that starts a VM.

## Files

| File                           | What it is |
|--------------------------------|------------|
| `podmanlib.py`                 | The library: all ported code, in six parts |
| `machine_connections.py`       | Main-based CLI: list, add and remove the machine connections in the same file podman uses |
| `image_reference.py`           | Main-based CLI: parses image references and prints the parts |
| `check_semver_port.py`         | Checks the semver port against the Go original |
| `check_reference_port.py`      | Checks the reference and digest ports against the Go original |
| `check_manifest_port.py`       | Checks the manifest port against the Go original |
| `check_machine_connections.py` | Checks the machine connection code against the Go original |
| `podman.py`                    | Command line: `podman` with the commands that exist here (version, image parse, manifest guess, digest and choose, machine connection); the others exit with status 3 |
| `skopeo.py`                    | Command line: `skopeo` (version; the rest not implemented) |
| `buildah.py`                   | Command line: `buildah` (version; image build is out of scope) |
| `podman_machine_os.py`         | Command line: `podman-machine-os` (version; the rest not implemented) |
| `test_cli.py`                  | Tests of the command line programs |
| `test_engine.py`               | Tests of the remote commands against a fake libpod service on a unix socket |
| `test_podmanlib.py`            | Unit tests |
| `.github/workflows/ci.yml`     | CI: lint, unit tests on several Python versions, a Python 2.7 run, and the cross-checks |
| `docs/ROADMAP.md`              | What is left to run containers, in order |

`podmanlib.py` has six parts:

1. **semver**: a line-by-line port of `golang.org/x/mod/semver/semver.go`.
2. **go.mod reader**: `module`, `go`, `require` and `replace`, single-line and block form.
3. **go-digest**: `digest.Parse` and the algorithm validation, from go-digest v1.0.0.
4. **reference**: `image/docker/reference` (`Parse`, `ParseNormalizedNamed`, `ParseDockerRef`,
   `ParseNamed`, `ParseAnyReference`, `WithName`, `WithTag`, `WithDigest`, `TrimNamed`, familiar names).
5. **machine connections**: the ssh connections of a machine (`name` and `name-root`), the
   default connection, the `ssh://` URL (Go's `url.URL.String()` escaping), and the
   `podman-connections.json` file with its lock.
6. **image manifests**: `GuessMIMEType`, `NormalizedMIMEType`, `Digest`, the platform of this
   machine (`WantedPlatforms`, including the `/proc/cpuinfo` variant detection), and the
   choice of an image from an OCI index (`chooseInstance`, with gzip and zstd preference) or a
   Docker manifest list (`ChooseInstance`).

Go identifiers are kept on purpose, so each function can be read next to the
Go function it came from. Go's `$` is written as `\Z` in the patterns, because
Python's `$` also matches before a final newline.

## Command line

The four programs you listed exist as Python scripts with the same names. Commands that
are ported run for real. The others are known by name and exit with status 3 and a
pointer to the roadmap.

```bash
python3 podman.py version
python3 podman.py image parse alpine:3.20
python3 podman.py manifest choose index.json --arch arm64 --os linux
python3 podman.py machine connection list
python3 podman.py pull alpine        # not implemented yet, exit status 3
python3 skopeo.py copy a b           # not implemented yet, exit status 3
python3 buildah.py build .           # out of scope, exit status 3
```

Exit status: 0 ok, 1 operation failed, 2 usage error, 3 not implemented.

## Remote engine (podman --remote)

`podman.py` talks to a podman service over a unix socket, the way `podman --remote` does. That is how it
works with a qbox box: the box forwards its podman API as a socket, and the host client uses it.

```bash
export CONTAINER_HOST=unix:///path/to/podman.sock     # or: -c NAME (a system connection), --url URL
python3 podman.py ps -a
python3 podman.py images --format '{{.Repository}}:{{.Tag}}'
python3 podman.py pull alpine:3.20
python3 podman.py run --rm -e A=1 -p 8080:80 -v /data:/srv:ro docker.io/library/alpine uname -m
python3 podman.py logs NAME
python3 podman.py system connection add --default qbox-inkb unix:///path/to/sock
```

`run` creates the container, starts it, waits for it to exit, prints its output and removes it with `--rm`.

Tested live against podman 4.9.3 in a qbox aarch64 box, through the box's forwarded socket and
`qbox connect` / `qbox podman`: version, ps, images, pull, image exists, run (with -d, -e, -p, -v, -w,
--rm and exit codes), logs, inspect, stop, rm, and the error paths. Podman 4.9 needs the API version in
the path (`/v4.9.0/libpod/...`); the client reads it from `/version`.
Interactive runs (`-i`, `-t`) and attaching to a running container are not supported yet; use `-d` for
services. The API is the one of the podman service in the box, so the container itself runs there.

## Usage

```bash
python3 image_reference.py alpine:3.20 Ubuntu
python3 machine_connections.py list
PODMAN_CONNECTIONS_CONF=/tmp/conns.json PODMAN_DEFAULT=1 \
  python3 machine_connections.py add-pair podman-machine-default 501 62022 core ~/.ssh/id_ed25519
python3 test_podmanlib.py
```

`machine_connections.py` reads and writes the file podman reads and writes. It uses
`PODMAN_CONNECTIONS_CONF`, then `$XDG_CONFIG_HOME/containers`, then `~/.config/containers`.
It takes the same POSIX lock as Go's storage lockfile (`<file>.lock`), so the two
exclude each other. Environment options: `PODMAN_ROOTFUL=1` and `PODMAN_DEFAULT=1`
for `add-pair`, `PODMAN_MACHINES=name=0,other=1` for `remove`.

The checks need a checkout of the upstream repository and the Go toolchain. The
checkout is only read. The Go code is built in a temporary directory.

```bash
PODMAN_GO_REFERENCE=/path/to/container-libs python3 check_semver_port.py
PODMAN_GO_REFERENCE=/path/to/container-libs python3 check_reference_port.py [count]
PODMAN_GO_REFERENCE=/path/to/container-libs python3 check_manifest_port.py [count]
PODMAN_GO_PODMAN=/path/to/podman python3 check_machine_connections.py [scenarios]
```

Exit status of the checks: 0 when every line is equal, 1 on mismatches, 2 when
the checkout or the `go` binary is missing.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

- **lint**: pyflakes on all Python files.
- **unit**: the unit tests and the command line tests on Python 3.10, 3.12 and 3.13, plus smoke runs.
- **python27**: the unit tests on Python 2.7. This job is informational (`continue-on-error`).
- **crosscheck**: clones the upstream repositories shallowly, then runs the four checks
  against the Go original.

## Verification

Each check runs the Python port and the original Go code on the same inputs and
compares every output line.

| Check                          | Inputs                                                       | Result |
|--------------------------------|--------------------------------------------------------------|--------|
| `check_semver_port.py`         | 20,841 version pairs, plus sorting of 6,695 strings          | 0 mismatches |
| `check_reference_port.py`      | 20,410 inputs: string literals from the package tests plus mutations | 0 mismatches |
| `check_manifest_port.py`       | 20,000 operations: media type detection on generated JSON with wrong field types, normalization, platform lists, index and list choice | 0 mismatches |
| `check_machine_connections.py` | 11,741 operations in random sequences (adds, pairs, defaults, removals, URL escaping), with the file compared after each step | 0 mismatches |

For the manifest code, the Go functions are copied verbatim out of the checkout
(`GuessMIMEType`, `NormalizedMIMEType`, `chooseInstance`, `ChooseInstance`, the
platform matcher file). Only the types they use are stubs. The machine connection
check works the same way with `connection.go` and `add.go` from podman.

Two bugs were found by these checks and fixed:

- The reference check: Go's separator `[-]*` can match an empty string. In Python's
  backtracking regex engine this made parsing exponential: a 14-character name took
  25 seconds. The port uses `-+` instead. This is the same language, because two adjacent
  alphanumeric runs with an empty separator form one run.
- The manifest check: Go decodes a `[]byte` field from a base64 string or from an array
  of numbers from 0 to 255. The port accepted only the string form.

## Deviations from Go

- **Map order.** When the default connection is removed and no machine default
  exists, Go picks the first connection in map order, which is random. The port
  picks the first name in sorted order. The check does not generate those cases
  (`removal_is_ambiguous`).
- **Nil map.** `UpdateConnectionPairPort` panics in Go when the connections map is
  nil. The port starts an empty map.
- **Unknown keys.** Keys the port does not use are kept in the file, so a file
  written here loses nothing podman needs.
- **Schema 1.** Digests of schema 1 signed manifests raise `NotImplementedError`.
- **Not tested against real podman.** The file format matches Go's JSON tags, and
  the lock is the same kind. Reading a file written here with the real podman
  binary has not been run.

## Design patterns

| Pattern       | Where |
|---------------|-------|
| Factory       | `getBestReferenceType()` picks the reference type that fits the parts |
| Composition   | Go struct embedding (`namedRepository`) becomes a base class |
| Interfaces    | `Named`, `Tagged`, `NamedTagged`, `CanonicalReference`, `Digested`; Go type assertions become `isinstance()` |
| Rollback      | `edit_connections_text()` returns the new text only if the callback succeeds, as Go writes only then |

`CanonicalReference` is the Python name of Go's `reference.Canonical` interface.
The plain name is taken by the semver function `Canonical`, which keeps its Go name.

## Python versions

The code is written for Python 2.7 and 3.x: `from __future__` imports, no
f-strings, no type annotations, no `ctypes`. Python 3.10 to 3.13 are tested in CI.
Python 2.7 is run in CI as an informational job; it was not run locally.
