# podman – Python-Umgebung

Dieses Repo hält die fünf Podman-Projekte als Git-Submodule unter `repos/` und
eine Python-Bibliothek (`podmanlib.py`), die sie zusammenhält. Die Skripte im
Hauptverzeichnis sind reine Einstiegspunkte (`if __name__ == "__main__"`) ohne
eigene Logik. Es wird kein `argparse` verwendet: das Kommando kommt aus
`sys.argv`, Optionen kommen aus Umgebungsvariablen.

## Repos

| Pfad                    | Upstream                                              | Rolle                                      |
|-------------------------|-------------------------------------------------------|--------------------------------------------|
| `repos/podman`          | podman-container-tools/podman                         | Container-Engine                           |
| `repos/container-libs`  | podman-container-tools/container-libs                 | gemeinsame Go-Module: common, image/v5, storage |
| `repos/skopeo`          | podman-container-tools/skopeo                         | Arbeiten mit Registries                    |
| `repos/buildah`         | podman-container-tools/buildah                        | OCI-Images bauen                           |
| `repos/podman-machine-os` | podman-container-tools/podman-machine-os            | Machine-Image-Dateien                      |

Die Submodule sind vollständige Klone (rund 340 MB). Sie sind auf die Commits
gepinnt, die dieses Repo aufzeichnet.

## Klonen

```bash
git clone --recurse-submodules https://github.com/FlorianLeonSteenbuck/podman.git
```

Bereits ohne Submodule geklont:

```bash
git submodule update --init
```

## Skripte

```bash
python3 sync_repos.py pin          # Submodule auf den hier gepinnten Commit setzen
python3 sync_repos.py latest       # upstream holen, jedes Repo auf origin/HEAD
python3 repo_status.py             # Branch, Commit, Datum, geänderte Dateien, Pin-Status
python3 go_deps.py graph           # Go-Abhängigkeiten zwischen den Repos mit Versions-Drift
python3 go_deps.py modules         # alle gefundenen go.mod-Dateien
python3 make_all.py help           # make-Target in jedem Repo, das es hat
python3 test_podmanlib.py          # Unit-Tests
python3 check_semver_port.py       # Python-Port gegen das Go-Original prüfen (braucht go)
```

Umgebungsvariablen:

- `PODMAN_ENV_REPOS=podman,skopeo` beschränkt alle Skripte auf diese Repos.
- `PODMAN_ENV_FORMAT=table|json|dot` wählt das Ausgabeformat. `dot` gilt nur für `go_deps.py graph`.

Die Drift-Spalte in `go_deps.py graph` vergleicht die Version, die ein Modul
verlangt, mit dem höchsten lokalen Tag des Moduls:

- `same`: gleiche Version
- `older`: verlangt eine ältere Version als den Tag
- `newer (untagged)`: verlangt eine Pseudo-Version über dem letzten Tag
- `no tag`: kein lokaler Tag vorhanden

## Aufbau

`podmanlib.py` enthält drei Teile:

1. **Port von `golang.org/x/mod/semver`**, Funktion für Funktion aus dem
   vendorten `semver.go` übersetzt. Die Go-Bezeichner sind beibehalten, damit
   beide Dateien nebeneinander gelesen werden können.
2. **go.mod-Leser** für `module`, `require` (Einzelzeile und Block) und `replace`.
3. **Die Umgebung**: Katalog der fünf Repos, Git- und make-Operationen, Modul-Graph.

Verwendete Design Patterns:

| Pattern    | Wo                                                                 |
|------------|--------------------------------------------------------------------|
| Singleton  | `PodmanEnvironment.instance()`                                     |
| Factory    | `RepoFactory` (Repo-Objekte), `make_formatter()` (Ausgabeformate)  |
| Facade     | `PodmanEnvironment` verbirgt git, make und go.mod                  |
| Command    | `PinSubmodule`, `LatestSubmodule`, `MakeTarget`, ausgeführt von `Invoker` |
| Template   | `Operation.run()`: Start, Vorprüfung, Ausführung, Ergebnis         |
| Observer   | `EventBus` mit `ConsoleObserver` und `RecordingObserver`           |
| Strategy   | `TableFormatter`, `JsonFormatter`, `DotFormatter`                  |
| Visitor    | `GoModuleScanner` besucht jedes Repo und liest seine go.mod-Dateien |

## Semver-Port prüfen

`check_semver_port.py` kopiert das vendorte `semver.go` in ein temporäres
Go-Modul, baut einen kleinen Treiber und gibt beiden Implementierungen dieselben
Eingaben. Verglichen werden IsValid, Canonical, Major, MajorMinor, Prerelease,
Build, Compare, Max und die Sortierung. Ergebnis zum Zeitpunkt des Ports:
0 Abweichungen bei 20.841 Paaren und identische Sortierung.

## Hinweise

- Getestet mit Python 3.13. Der Code ist für Python 2.7 geschrieben (kein
  f-String, keine Typannotationen, `from __future__`-Importe), dort aber nicht
  ausgeführt.
- Die Go-Quellen der Repos bleiben unverändert. Python steuert sie über git und
  make, es ersetzt keinen Go-Code.
