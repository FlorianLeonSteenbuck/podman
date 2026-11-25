# Roadmap: what it takes to run containers in Python

Goal: the parts of podman, container-libs (common, image, storage) and skopeo
that are needed to pull an image and run, list, stop and remove a container,
rewritten in Python. Out of scope for now: image build (buildah) and image
serving (the REST service in `pkg/api` and the client in `pkg/bindings`).

Line counts are non-test Go lines in the upstream checkouts. A Python rewrite
is usually about the same size.

## Done

| Piece                                         | Go lines (approx.) | Status                 |
|-----------------------------------------------|--------------------|------------------------|
| `image/docker/reference` (name parsing)       | ~800               | ported, cross-checked  |
| go-digest validation                          | ~250               | ported, cross-checked  |
| `golang.org/x/mod/semver`                     | ~400               | ported, cross-checked  |
| go.mod reader                                 | -                  | own code               |
| Machine connections: `pkg/machine/connection`, `common/pkg/config/connections.go` (the ssh connections of a VM and their file) | ~1000 (connection.go, add.go, connections.go) | ported, cross-checked (11,741 operations). Relevant for 2.4 too: the same file. |

## Tier 1: get an image into local storage

Nothing here builds or serves images.

| #    | Piece (Go package)                          | Go lines | Notes |
|------|---------------------------------------------|----------|-------|
| 1.1  | `image/manifest`                            | 1362     | OCI and Docker manifests, indexes, platform choice. Pure logic, good first step. |
| 1.2  | `image/types`, `image/transports`           | 870      | Interfaces and transport names (`docker://`, `containers-storage:`, `oci:`, `dir:`). |
| 1.3  | `image/docker`                              | 4835     | Registry client: token auth, manifest and blob download. Testable against a local fake registry. |
| 1.4  | `storage` (top level)                       | 11378    | Layer, image and container stores (JSON metadata, lock files). Start with the parts pulls need. |
| 1.5  | `storage/pkg/archive`, `idtools`            | 6400     | Tar layer unpacking with whiteouts, ID mapping. |
| 1.6  | `storage/drivers/vfs`                       | 477      | Simplest driver: every layer is a full copy. No mount privileges needed. |
| 1.7  | `image/storage`                             | 3064     | The `containers-storage:` transport: writes pulled images into the store. |
| 1.8  | `image/copy`                                | 3473     | Pull orchestration: choose layers, copy blobs, write the image. |
| 1.9  | `common/libimage` (pull, images, rmi only)  | 7264 in total | podman's image layer on top of 1.3 to 1.8. Take only what the CLI needs; the share of the 7264 is not measured yet. |

## Tier 2: run a container

| #    | Piece (Go package)                              | Go lines | Notes |
|------|-------------------------------------------------|----------|-------|
| 2.1  | `libpod` state: `sqlite_state.go` and helpers   | ~3200    | Container and pod records. Python has `sqlite3` in the standard library, so the schema can carry over. |
| 2.2  | `libpod/container.go`, `container_config.go`    | ~2100    | Container model and its config. |
| 2.3  | `pkg/specgen` and `pkg/specgen/generate`        | ~7500    | Turns run options into an OCI runtime spec (`config.json`): mounts, namespaces, env, user, capabilities, rlimits. |
| 2.4  | `common/pkg/config`                             | 3169     | Reads `containers.conf` (defaults, runtime path, default capabilities). |
| 2.5  | `common/pkg/seccomp`                            | 2022     | Default seccomp profile, loaded as JSON. |
| 2.6  | `libpod` OCI runtime layer (`oci*.go`)          | ~2800    | Calls the OCI runtime and reads the state back. **Decision needed, see below.** |
| 2.7  | `libpod/container_internal*.go`                 | ~7400    | Create, start, stop, remove. The largest and most important part of `run`. |
| 2.8  | `libpod/container_api.go`                       | 1286     | The public lifecycle calls on top of 2.7. |
| 2.9  | `libpod` logs (`container_log*.go`, `logs/`)    | ~750     | `podman logs` and the exit code. |

## Tier 3: command line

| #    | Piece                                           | Notes |
|------|-------------------------------------------------|-------|
| 3.1  | Subset of `cmd/podman`: `pull`, `images`, `run`, `create`, `start`, `stop`, `rm`, `ps`, `logs`, `inspect` | The Cobra CLI is ~30k lines. Only the flags for these commands are needed. Options are parsed by hand from `sys.argv`, as in the rest of this repo. |
| 3.2  | `domain/entities`                               | Output structs for `ps`, `inspect`, `images`. |

## Tier 4: later

Networking (`libpod/networking_*.go`, about 1000 lines), exec and attach,
volumes, healthchecks, pods (`pod*.go`, about 1500), rootless user namespaces
(`pkg/rootless`), cgroups v2 details, events, `kube` play, checkpoint and
restore, `overlay` storage driver (`storage/drivers/overlay`, 3787 lines).

## Out of scope

- Image build: buildah, about 47k lines.
- Image serving: `pkg/api` (26k) and `pkg/bindings` (16k), the REST service and its client.
- `podman machine` and podman-machine-os: about 15k lines plus the VM image.
- Windows and FreeBSD code paths.

## Decisions needed before Tier 2

1. **OCI runtime.** Porting `runc` or `crun` itself is not realistic. Options:
   (a) call the installed `crun` or `runc` binary from Python, the way podman does; or
   (b) set up namespaces, cgroups and pivot_root directly from Python with `ctypes`.
   Recommendation: (a). Option (b) is far more work and risky.
2. **conmon.** podman uses the C program conmon to keep the container's output
   and exit code. Recommendation: replace it with a small Python supervisor
   process in the same role.
3. **Storage driver.** Recommendation: `vfs` first (step 1.6), `overlay` later.
4. **Rootful or rootless first.** Recommendation: rootful, since this sandbox runs as root.

## Suggested order

1.1, then 1.2 and 1.3 (with a fake registry test), then 1.4 to 1.6, then 1.7
and 1.8, then 1.9. That gives `pull` and `images`. After that, Tier 2, in the
order listed. Every ported piece gets a cross-check against the Go original, the
same way as `check_reference_port.py`.
