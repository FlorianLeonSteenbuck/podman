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

Not yet ported inside the parts above: `FamiliarMatch` (needs Go's `path.Match`)
and the `Field` encoding helper of `image/docker/reference`. Opening the ssh
connection itself (`common/pkg/ssh`) is not ported.

## Files

| File                          | What it is |
|-------------------------------|------------|
| `podmanlib.py`                | The library: all ported code, in five parts |
| `machine_connections.py`      | Main-based CLI: list, add and remove the machine connections in the same file podman uses |
| `image_reference.py`          | Main-based CLI: parses image references and prints the parts |
| `check_semver_port.py`        | Checks the semver port against the Go original |
| `check_reference_port.py`     | Checks the reference and digest ports against the Go original |
| `check_machine_connections.py`| Checks the machine connection code against the Go original |
| `test_podmanlib.py`           | Unit tests |
| `docs/ROADMAP.md`            | What is left to run containers, in order |

`podmanlib.py` has five parts:

1. **semver**: a line-by-line port of `golang.org/x/mod/semver/semver.go`.
2. **go.mod reader**: `module`, `go`, `require` and `replace`, single-line and block form.
3. **go-digest**: `digest.Parse` and the algorithm validation, from go-digest v1.0.0.
4. **reference**: `image/docker/reference` (`Parse`, `ParseNormalizedNamed`, `ParseDockerRef`,
   `ParseNamed`, `ParseAnyReference`, `WithName`, `WithTag`, `WithDigest`, `TrimNamed`, familiar names).
5. **machine connections**: the ssh connections of a machine (`name` and `name-root`), the
   default connection, the `ssh://` URL (Go's `url.URL.String()` escaping), and the
   `podman-connections.json` file with its lock.

Go identifiers are kept on purpose, so each function can be read next to the
Go function it came from. Go's `$` is written as `\Z` in the patterns, because
Python's `$` also matches before a final newline.

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
PODMAN_GO_PODMAN=/path/to/podman python3 check_machine_connections.py [scenarios]
```

Exit status of the checks: 0 when every line is equal, 1 on mismatches, 2 when
the checkout or the `go` binary is missing.

## Verification

Each check runs the Python port and the original Go code on the same inputs and
compares every output line.

| Check                        | Inputs                                                       | Result |
|------------------------------|--------------------------------------------------------------|--------|
| `check_semver_port.py`       | 20,841 version pairs, plus sorting of 6,695 strings          | 0 mismatches |
| `check_reference_port.py`    | 20,410 inputs: string literals from the package tests plus mutations | 0 mismatches |
| `check_machine_connections.py` | 11,741 operations in random sequences (adds, pairs, defaults, removals, URL escaping), with the file compared after each step | 0 mismatches |

For the machine connections the Go side is `connection.go` and `add.go` from
podman, unchanged except for two import paths. The configuration types are stubs
with the same JSON tags as container-libs.

One bug was found by the reference check. The Go separator `[-]*` can match an
empty string. In Python's backtracking regex engine this made parsing exponential:
a 14-character name took 25 seconds. The port uses `-+` instead. This is the same
language, because two adjacent alphanumeric runs with an empty separator form one
run. Go's RE2 engine is linear, so it never had the problem.

## Deviations from Go

- **Map order.** When the default connection is removed and no machine default
  exists, Go picks the first connection in map order, which is random. The port
  picks the first name in sorted order. The check does not generate those cases
  (`removal_is_ambiguous`).
- **Nil map.** `UpdateConnectionPairPort` panics in Go when the connections map is
  nil. The port starts an empty map.
- **Unknown keys.** Keys the port does not use are kept in the file, so a file
  written here loses nothing podman needs.
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
f-strings, no type annotations, no `ctypes`. It was run and tested on Python 3.13 only.
