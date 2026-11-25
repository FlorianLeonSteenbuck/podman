# podman in Python (rewrite)

This repository is a Python rewrite of Go code from the podman-container-tools
projects. It does not contain, bind, build or run those Go projects. The Go
sources are only used as the reference that the Python code is checked against.

The full rewrite of all five projects is a multi-stage effort. podman alone is
several hundred thousand lines of Go. This repository currently holds the first
parts, listed below.

## Scope

| Project             | Status                                                                 |
|---------------------|------------------------------------------------------------------------|
| container-libs      | Ported: `golang.org/x/mod/semver` (vendored), `image/docker/reference`, go-digest validation. Not yet: `common`, image transports and copy, `storage` |
| podman              | Not started                                                            |
| skopeo              | Not started                                                            |
| buildah             | Not started                                                            |
| podman-machine-os   | Not started                                                            |

Not yet ported inside the ported parts: `FamiliarMatch` (needs a port of Go's
`path.Match`) and the `Field` encoding helper of `image/docker/reference`.

## Files

| File                     | What it is                                                        |
|--------------------------|-------------------------------------------------------------------|
| `podmanlib.py`           | The library: all ported code                                      |
| `image_reference.py`     | Main-based CLI: parses image references and prints the parts     |
| `check_semver_port.py`   | Checks the semver port against the Go original                    |
| `check_reference_port.py`| Checks the reference and digest ports against the Go original     |
| `test_podmanlib.py`      | Unit tests                                                        |

`podmanlib.py` is one library with four parts:

1. **semver**: a line-by-line port of `golang.org/x/mod/semver/semver.go`.
2. **go.mod reader**: `module`, `go`, `require` and `replace`, single-line and block form.
3. **go-digest**: `digest.Parse` and the algorithm validation, from go-digest v1.0.0.
4. **reference**: `image/docker/reference` (`Parse`, `ParseNormalizedNamed`,
   `ParseDockerRef`, `ParseNamed`, `ParseAnyReference`, `WithName`, `WithTag`,
   `WithDigest`, `TrimNamed`, familiar names).

Go identifiers are kept on purpose, so each function can be read next to the
Go function it came from. Go's `$` is written as `\Z` in the patterns, because
Python's `$` also matches before a final newline.

## Usage

```bash
python3 image_reference.py alpine:3.20 Ubuntu
python3 test_podmanlib.py
```

The checks need a checkout of container-libs and the Go toolchain. The checkout is
only read, and the Go code is built in a temporary directory. The reference
driver fetches go-digest through the Go module proxy.

```bash
PODMAN_GO_REFERENCE=/path/to/container-libs python3 check_semver_port.py
PODMAN_GO_REFERENCE=/path/to/container-libs python3 check_reference_port.py [count]
```

Exit status of the checks: 0 when every line is equal, 1 on mismatches, 2 when
the checkout or the `go` binary is missing.

## Verification

Both checks run the Python port and the original Go code on the same inputs and
compare every output line.

| Check                      | Inputs                                                    | Result            |
|----------------------------|-----------------------------------------------------------|-------------------|
| `check_semver_port.py`     | 20,841 version pairs, plus sorting of 6,695 strings        | 0 mismatches      |
| `check_reference_port.py`  | 20,410 inputs: string literals from the package tests plus mutations | 0 mismatches |

The reference check covers `Parse`, `ParseNormalizedNamed`, `ParseDockerRef`,
`ParseNamed`, `ParseAnyReference`, `TagNameOnly`, `IsNameOnly`, `TrimNamed`,
`IsFullIdentifier`, familiar names and `digest.Parse`, including the error texts.

One bug was found by the check work. The Go separator `[-]*` can match an empty
string. In Python's backtracking regex engine this made parsing exponential: a
14-character name took 25 seconds. The port uses `-+` instead. This is the same
language, because two adjacent alphanumeric runs with an empty separator form one
run. Go's RE2 engine is linear, so it never had the problem.

## Design patterns

| Pattern       | Where                                                                  |
|---------------|------------------------------------------------------------------------|
| Factory       | `getBestReferenceType()` picks the reference type that fits the parts  |
| Composition   | Go struct embedding (`namedRepository`) becomes a base class            |
| Interfaces    | `Named`, `Tagged`, `NamedTagged`, `CanonicalReference`, `Digested`; Go type assertions become `isinstance()` |

`CanonicalReference` is the Python name of Go's `reference.Canonical` interface.
The plain name is taken by the semver function `Canonical`, which keeps its Go name.

## Python versions

The code is written for Python 2.7 and 3.x: `from __future__` imports, no
f-strings, no type annotations. It was run and tested on Python 3.13 only.
