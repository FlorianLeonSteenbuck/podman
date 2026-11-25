# -*- coding: utf-8 -*-
"""podmanlib - the one library behind the podman environment scripts.

The main-based scripts next to this file (sync_repos.py, repo_status.py,
go_deps.py, make_all.py) hold no logic of their own: they pick a command
and call into this module. Python 2.7 and 3.x compatible. No argparse:
the command comes from sys.argv, options come from environment variables
(PODMAN_ENV_REPOS, PODMAN_ENV_FORMAT).

Design patterns
  Singleton  PodmanEnvironment.instance()
  Factory    RepoFactory builds RepoSpec objects, make_formatter() builds formatters
  Facade     PodmanEnvironment hides git, make and go.mod handling
  Command    Operation subclasses, executed by an Invoker
  Template   Operation.run() is the fixed skeleton, subclasses fill the hooks
  Observer   EventBus informs ConsoleObserver / RecordingObserver
  Strategy   Formatter subclasses: TableFormatter, JsonFormatter, DotFormatter

Part 1 is a line-by-line port of golang.org/x/mod/semver (semver.go), the copy
vendored in repos/podman/vendor/golang.org/x/mod/semver/. Go identifiers are
kept on purpose so the two files can be read side by side.
Part 2 reads go.mod files (module, require, replace).
Part 3 is the environment: catalogue, git and make operations, module graph.
"""

from __future__ import absolute_import, division, print_function

import functools
import io
import json
import os
import subprocess
import sys
from collections import OrderedDict


# ===========================================================================
# Part 1: port of golang.org/x/mod/semver/semver.go
# ===========================================================================


class _Parsed(object):
    """Go: type parsed struct."""

    def __init__(self):
        self.major = ""
        self.minor = ""
        self.patch = ""
        self.short = ""
        self.prerelease = ""
        self.build = ""


def IsValid(v):
    """Go: IsValid reports whether v is a valid semantic version string."""
    _, ok = parse(v)
    return ok


def Canonical(v):
    """Go: Canonical returns the canonical formatting of the semantic version v."""
    p, ok = parse(v)
    if not ok:
        return ""
    if p.build != "":
        return v[:len(v) - len(p.build)]
    if p.short != "":
        return v + p.short
    return v


def Major(v):
    """Go: Major returns the major version prefix, Major("v2.1.0") == "v2"."""
    pv, ok = parse(v)
    if not ok:
        return ""
    return v[:1 + len(pv.major)]


def MajorMinor(v):
    """Go: MajorMinor returns the major.minor prefix, MajorMinor("v2.1.0") == "v2.1"."""
    pv, ok = parse(v)
    if not ok:
        return ""
    i = 1 + len(pv.major)
    j = i + 1 + len(pv.minor)
    if j <= len(v) and v[i] == "." and v[i + 1:j] == pv.minor:
        return v[:j]
    return v[:i] + "." + pv.minor


def Prerelease(v):
    """Go: Prerelease returns the prerelease suffix, Prerelease("v2.1.0-pre+meta") == "-pre"."""
    pv, ok = parse(v)
    if not ok:
        return ""
    return pv.prerelease


def Build(v):
    """Go: Build returns the build suffix, Build("v2.1.0+meta") == "+meta"."""
    pv, ok = parse(v)
    if not ok:
        return ""
    return pv.build


def Compare(v, w):
    """Go: Compare returns 0 if v == w, -1 if v < w, +1 if v > w.

    An invalid semantic version string is less than a valid one.
    All invalid semantic version strings compare equal to each other.
    """
    pv, ok1 = parse(v)
    pw, ok2 = parse(w)
    if not ok1 and not ok2:
        return 0
    if not ok1:
        return -1
    if not ok2:
        return +1
    c = compareInt(pv.major, pw.major)
    if c != 0:
        return c
    c = compareInt(pv.minor, pw.minor)
    if c != 0:
        return c
    c = compareInt(pv.patch, pw.patch)
    if c != 0:
        return c
    return comparePrerelease(pv.prerelease, pw.prerelease)


def Max(v, w):
    """Go: Max canonicalizes both arguments and returns the greater one (deprecated in Go)."""
    v = Canonical(v)
    w = Canonical(w)
    if Compare(v, w) > 0:
        return v
    return w


class ByVersion(list):
    """Go: type ByVersion []string, implements sort.Interface."""

    def Len(self):
        return len(self)

    def Swap(self, i, j):
        self[i], self[j] = self[j], self[i]

    def Less(self, i, j):
        return compareVersion(self[i], self[j]) < 0


def Sort(lst):
    """Go: Sort sorts a list of semantic versions in place (Compare, then string order)."""
    lst.sort(key=functools.cmp_to_key(compareVersion))


def compareVersion(a, b):
    cmp_ = Compare(a, b)
    if cmp_ != 0:
        return cmp_
    return _strings_compare(a, b)


def _strings_compare(a, b):
    """Go: strings.Compare."""
    return (a > b) - (a < b)


def parse(v):
    """Go: parse returns the parsed form of a semantic version string.

    Returns (p, ok). Go uses named results; here they are a tuple.
    """
    p = _Parsed()
    ok = False
    if v == "" or v[0] != "v":
        return p, ok
    p.major, v, ok = parseInt(v[1:])
    if not ok:
        return p, ok
    if v == "":
        p.minor = "0"
        p.patch = "0"
        p.short = ".0.0"
        return p, True
    if v[0] != ".":
        return p, False
    p.minor, v, ok = parseInt(v[1:])
    if not ok:
        return p, ok
    if v == "":
        p.patch = "0"
        p.short = ".0"
        return p, True
    if v[0] != ".":
        return p, False
    p.patch, v, ok = parseInt(v[1:])
    if not ok:
        return p, ok
    if len(v) > 0 and v[0] == "-":
        p.prerelease, v, ok = parsePrerelease(v)
        if not ok:
            return p, ok
    if len(v) > 0 and v[0] == "+":
        p.build, v, ok = parseBuild(v)
        if not ok:
            return p, ok
    if v != "":
        return p, False
    return p, True


def parseInt(v):
    """Go: parseInt returns (t, rest, ok)."""
    if v == "":
        return "", "", False
    if v[0] < "0" or "9" < v[0]:
        return "", "", False
    i = 1
    while i < len(v) and "0" <= v[i] <= "9":
        i += 1
    if v[0] == "0" and i != 1:
        return "", "", False
    return v[:i], v[i:], True


def parsePrerelease(v):
    """Go: parsePrerelease returns (t, rest, ok) for a "-pre" suffix."""
    # "A pre-release version MAY be denoted by appending a hyphen and
    # a series of dot separated identifiers immediately following the patch version.
    # Identifiers MUST comprise only ASCII alphanumerics and hyphen [0-9A-Za-z-].
    # Identifiers MUST NOT be empty. Numeric identifiers MUST NOT include leading zeroes."
    if v == "" or v[0] != "-":
        return "", "", False
    i = 1
    start = 1
    while i < len(v) and v[i] != "+":
        if not isIdentChar(v[i]) and v[i] != ".":
            return "", "", False
        if v[i] == ".":
            if start == i or isBadNum(v[start:i]):
                return "", "", False
            start = i + 1
        i += 1
    if start == i or isBadNum(v[start:i]):
        return "", "", False
    return v[:i], v[i:], True


def parseBuild(v):
    """Go: parseBuild returns (t, rest, ok) for a "+build" suffix."""
    if v == "" or v[0] != "+":
        return "", "", False
    i = 1
    start = 1
    while i < len(v):
        if not isIdentChar(v[i]) and v[i] != ".":
            return "", "", False
        if v[i] == ".":
            if start == i:
                return "", "", False
            start = i + 1
        i += 1
    if start == i:
        return "", "", False
    return v[:i], v[i:], True


def isIdentChar(c):
    """Go: isIdentChar(c byte) - ASCII alphanumerics and hyphen."""
    return "A" <= c <= "Z" or "a" <= c <= "z" or "0" <= c <= "9" or c == "-"


def isBadNum(v):
    """Go: isBadNum - all digits, longer than one, leading zero."""
    i = 0
    while i < len(v) and "0" <= v[i] <= "9":
        i += 1
    return i == len(v) and i > 1 and v[0] == "0"


def isNum(v):
    """Go: isNum - all characters are digits."""
    i = 0
    while i < len(v) and "0" <= v[i] <= "9":
        i += 1
    return i == len(v)


def compareInt(x, y):
    """Go: compareInt compares two digit strings numerically."""
    if x == y:
        return 0
    if len(x) < len(y):
        return -1
    if len(x) > len(y):
        return +1
    if x < y:
        return -1
    return +1


def comparePrerelease(x, y):
    """Go: comparePrerelease implements the semver.org precedence rules."""
    # "When major, minor, and patch are equal, a pre-release version has
    # lower precedence than a normal version."
    # Identifiers consisting of only digits are compared numerically and
    # identifiers with letters or hyphens are compared lexically in ASCII
    # sort order. Numeric identifiers always have lower precedence than
    # non-numeric identifiers. A larger set of pre-release fields has a
    # higher precedence than a smaller set, if all of the preceding
    # identifiers are equal.
    if x == y:
        return 0
    if x == "":
        return +1
    if y == "":
        return -1
    while x != "" and y != "":
        x = x[1:]  # skip - or .
        y = y[1:]  # skip - or .
        dx, x = nextIdent(x)
        dy, y = nextIdent(y)
        if dx != dy:
            ix = isNum(dx)
            iy = isNum(dy)
            if ix != iy:
                if ix:
                    return -1
                else:
                    return +1
            if ix:
                if len(dx) < len(dy):
                    return -1
                if len(dx) > len(dy):
                    return +1
            if dx < dy:
                return -1
            else:
                return +1
    if x == "":
        return -1
    else:
        return +1


def nextIdent(x):
    """Go: nextIdent returns (dx, rest) - the identifier up to the next dot."""
    i = 0
    while i < len(x) and x[i] != ".":
        i += 1
    return x[:i], x[i:]


# ===========================================================================
# Part 2: go.mod reader (module / require / replace)
# ===========================================================================


class GoMod(object):
    """The parts of a go.mod file the environment cares about."""

    def __init__(self):
        self.module = ""
        self.requires = OrderedDict()  # module path -> version
        self.replaces = OrderedDict()  # module path -> replacement target


def _unquote(token):
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "`"):
        return token[1:-1]
    return token


def _apply_directive(mod, verb, fields):
    if verb == "require":
        if len(fields) >= 2:
            mod.requires[_unquote(fields[0])] = fields[1]
    elif verb == "replace":
        if "=>" in fields:
            i = fields.index("=>")
            if i >= 1 and i + 1 < len(fields):
                mod.replaces[_unquote(fields[0])] = " ".join(fields[i + 1:])


def parse_go_mod(text):
    """Read go.mod text. Handles single-line and block forms; // comments are dropped."""
    mod = GoMod()
    block = ""
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if line == "":
            continue
        if block:
            if line == ")":
                block = ""
            else:
                _apply_directive(mod, block, line.split())
            continue
        fields = line.split()
        verb = fields[0]
        if len(fields) == 2 and fields[1] == "(" and verb in ("require", "replace"):
            block = verb
        elif verb == "module" and len(fields) >= 2:
            mod.module = _unquote(fields[1])
        elif verb in ("require", "replace"):
            _apply_directive(mod, verb, fields[1:])
    return mod


# ===========================================================================
# Part 3: the environment
# ===========================================================================

ROOT = os.path.dirname(os.path.abspath(__file__))

OK = "ok"
FAIL = "fail"
SKIP = "skip"

# name, clone url, role. Order is the order of every report.
CATALOGUE = (
    ("podman", "https://github.com/podman-container-tools/podman.git",
     "container engine"),
    ("container-libs", "https://github.com/podman-container-tools/container-libs.git",
     "shared Go modules: common, image/v5, storage"),
    ("skopeo", "https://github.com/podman-container-tools/skopeo.git",
     "work with remote image registries"),
    ("buildah", "https://github.com/podman-container-tools/buildah.git",
     "build OCI images"),
    ("podman-machine-os", "https://github.com/podman-container-tools/podman-machine-os.git",
     "machine image files"),
)


# ---------------------------------------------------------------------------
# Shell helper
# ---------------------------------------------------------------------------


class CommandResult(object):
    def __init__(self, argv, returncode, stdout, stderr):
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self):
        return self.returncode == 0

    def last_error(self):
        lines = [l.strip() for l in self.stderr.splitlines() if l.strip()]
        if lines:
            return lines[-1]
        return "exit code %d" % self.returncode


def _text(data):
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


def run(argv, cwd=None):
    """Run a program and capture its output. Never raises for a failing program."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return CommandResult(argv, 127, "", str(exc))
    out, err = proc.communicate()
    return CommandResult(argv, proc.returncode, _text(out), _text(err))


def git(cwd, *args):
    return run(["git"] + list(args), cwd=cwd)


# ---------------------------------------------------------------------------
# Factory: repo specs
# ---------------------------------------------------------------------------


class RepoSpec(object):
    def __init__(self, name, url, role, repos_dir):
        self.name = name
        self.url = url
        self.role = role
        self.path = os.path.join(repos_dir, name)
        self.relpath = "repos/" + name  # as git sees it from the top repo

    def is_checked_out(self):
        return os.path.exists(os.path.join(self.path, ".git"))


class RepoFactory(object):
    def __init__(self, repos_dir):
        self.repos_dir = repos_dir

    def build(self, catalogue=CATALOGUE, selection=None):
        known = set(name for name, _, _ in catalogue)
        if selection:
            unknown = [n for n in selection if n not in known]
            if unknown:
                raise ValueError("unknown repo(s): %s" % ", ".join(unknown))
        return [RepoSpec(name, url, role, self.repos_dir)
                for name, url, role in catalogue
                if not selection or name in selection]


def selected_names():
    """PODMAN_ENV_REPOS=podman,skopeo limits the repos; unset means all."""
    raw = os.environ.get("PODMAN_ENV_REPOS", "").strip()
    if not raw:
        return None
    return [n.strip() for n in raw.split(",") if n.strip()]


def command_name(argv, commands):
    """Return the first positional argument if it is a known command, else None."""
    if len(argv) < 2:
        return None
    if argv[1] in commands:
        return argv[1]
    return None


# ---------------------------------------------------------------------------
# Observer: event bus
# ---------------------------------------------------------------------------


class Observer(object):
    def update(self, event, payload):
        pass


class EventBus(object):
    def __init__(self):
        self._observers = []

    def attach(self, observer):
        self._observers.append(observer)

    def notify(self, event, **payload):
        for observer in self._observers:
            observer.update(event, payload)


class ConsoleObserver(Observer):
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    def update(self, event, payload):
        if event in (OK, FAIL, SKIP):
            line = "[%-4s] %-14s %-18s %s" % (event, payload["op"], payload["repo"],
                                              payload.get("detail", ""))
            self.stream.write(line.rstrip() + "\n")


class RecordingObserver(Observer):
    def __init__(self):
        self.events = []

    def update(self, event, payload):
        self.events.append((event, payload))


# ---------------------------------------------------------------------------
# Command + Template Method: operations on repos
# ---------------------------------------------------------------------------


class Outcome(object):
    def __init__(self, op, repo, status, detail=""):
        self.op = op
        self.repo = repo
        self.status = status
        self.detail = detail


class Operation(object):
    """Command: one unit of work on one repo.

    run() is the Template Method: start event, precheck, perform, result event.
    Subclasses override precheck() and perform() only.
    """

    name = "operation"

    def __init__(self, repo, root=ROOT):
        self.repo = repo
        self.root = root

    def precheck(self):
        """Return (True, "") to go ahead, or (False, reason) to skip."""
        return True, ""

    def perform(self):
        """Return (status, detail)."""
        raise NotImplementedError

    def run(self, bus):
        bus.notify("start", op=self.name, repo=self.repo.name, detail="")
        ok, reason = self.precheck()
        if ok:
            status, detail = self.perform()
        else:
            status, detail = SKIP, reason
        bus.notify(status, op=self.name, repo=self.repo.name, detail=detail)
        return Outcome(self.name, self.repo.name, status, detail)


class PinSubmodule(Operation):
    """Check out the commit this repo records for the submodule."""

    name = "pin"

    def perform(self):
        res = git(self.root, "submodule", "update", "--init", "--", self.repo.relpath)
        if res.ok:
            return OK, "recorded commit checked out"
        return FAIL, res.last_error()


class LatestSubmodule(Operation):
    """Fetch upstream and move the submodule to origin/HEAD (detached)."""

    name = "latest"

    def precheck(self):
        if not self.repo.is_checked_out():
            return False, "not checked out (run pin first)"
        return True, ""

    def perform(self):
        fetched = git(self.repo.path, "fetch", "--tags", "--prune", "origin")
        if not fetched.ok:
            return FAIL, fetched.last_error()
        moved = git(self.repo.path, "checkout", "--detach", "origin/HEAD")
        if not moved.ok:
            return FAIL, moved.last_error()
        sha = git(self.repo.path, "rev-parse", "--short", "HEAD").stdout.strip()
        return OK, "now at %s" % sha


class MakeTarget(Operation):
    """Run `make <target>` in the repo, if it has a Makefile with that target."""

    def __init__(self, repo, target, root=ROOT):
        Operation.__init__(self, repo, root)
        self.target = target
        self.name = "make:" + target

    def precheck(self):
        if not self.repo.is_checked_out():
            return False, "not checked out"
        if not os.path.exists(os.path.join(self.repo.path, "Makefile")):
            return False, "no Makefile"
        if not run(["make", "-n", self.target], cwd=self.repo.path).ok:
            return False, "no target '%s'" % self.target
        return True, ""

    def perform(self):
        res = run(["make", self.target], cwd=self.repo.path)
        if res.ok:
            return OK, "done"
        return FAIL, res.last_error()


class Invoker(object):
    """Runs Commands, keeps a history."""

    def __init__(self, bus):
        self.bus = bus
        self.history = []

    def execute(self, op):
        outcome = op.run(self.bus)
        self.history.append(outcome)
        return outcome

    def execute_all(self, ops):
        return [self.execute(op) for op in ops]


def any_failed(outcomes):
    return any(o.status == FAIL for o in outcomes)


# ---------------------------------------------------------------------------
# Strategy + Factory: output formats
# ---------------------------------------------------------------------------


class Formatter(object):
    def render(self, rows):
        raise NotImplementedError


class TableFormatter(Formatter):
    def render(self, rows):
        if not rows:
            return ""
        columns = list(rows[0].keys())
        widths = {}
        for c in columns:
            widths[c] = max([len(c)] + [len(str(r.get(c, ""))) for r in rows])
        lines = ["  ".join(c.ljust(widths[c]) for c in columns)]
        lines.append("  ".join("-" * widths[c] for c in columns))
        for r in rows:
            lines.append("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))
        return "\n".join(lines)


class JsonFormatter(Formatter):
    def render(self, rows):
        return json.dumps(rows, indent=2)


class DotFormatter(Formatter):
    """Graphviz DOT. Rows need src, dst, module and drift keys."""

    def render(self, rows):
        lines = ["digraph go_modules {", "  rankdir=LR;"]
        for r in rows:
            label = "%s\\n%s" % (r["module"], r["drift"])
            lines.append('  "%s" -> "%s" [label="%s"];' % (r["src"], r["dst"], label))
        lines.append("}")
        return "\n".join(lines)


FORMATTERS = {
    "table": TableFormatter,
    "json": JsonFormatter,
    "dot": DotFormatter,
}


def make_formatter(name):
    """Factory: formatter by name. Unknown names fall back to table."""
    return FORMATTERS.get(name, TableFormatter)()


def output_format():
    return os.environ.get("PODMAN_ENV_FORMAT", "table").strip().lower()


# ---------------------------------------------------------------------------
# Status: git state of each repo and of the submodule pins
# ---------------------------------------------------------------------------

SUBMODULE_WORDS = {" ": "in sync", "+": "moved", "-": "not init", "U": "conflict"}


def submodule_marks(root):
    """Map repos/<name> -> state word, from `git submodule status` in the top repo."""
    marks = {}
    for line in git(root, "submodule", "status").stdout.splitlines():
        if not line:
            continue
        fields = line[1:].split()
        if len(fields) >= 2:
            marks[fields[1]] = SUBMODULE_WORDS.get(line[0], "unknown")
    return marks


def repo_status(repo, marks):
    row = OrderedDict()
    row["repo"] = repo.name
    row["submodule"] = marks.get(repo.relpath, "not in index")
    if not repo.is_checked_out():
        row["branch"] = "-"
        row["commit"] = "-"
        row["date"] = "-"
        row["dirty"] = "-"
        return row
    branch = git(repo.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    row["branch"] = "detached" if branch == "HEAD" else branch
    row["commit"] = git(repo.path, "rev-parse", "--short", "HEAD").stdout.strip()
    row["date"] = git(repo.path, "log", "-1", "--format=%cs").stdout.strip()
    changes = [l for l in git(repo.path, "status", "--porcelain").stdout.splitlines() if l.strip()]
    row["dirty"] = str(len(changes))
    return row


# ---------------------------------------------------------------------------
# Visitor: collect go.mod files
# ---------------------------------------------------------------------------


class ModuleInfo(object):
    def __init__(self, repo, rel_dir, mod):
        self.repo = repo
        self.rel_dir = rel_dir  # "." for the repo root
        self.mod = mod


class GoModuleScanner(object):
    """Visitor: walks one repo and reads every go.mod outside vendor/."""

    SKIP_DIRS = frozenset(["vendor", ".git", "testdata"])

    def visit(self, repo):
        found = []
        if not repo.is_checked_out():
            return found
        for dirpath, dirnames, filenames in os.walk(repo.path):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            if "go.mod" not in filenames:
                continue
            with io.open(os.path.join(dirpath, "go.mod"), encoding="utf-8",
                         errors="replace") as fh:
                mod = parse_go_mod(fh.read())
            rel = os.path.relpath(dirpath, repo.path).replace(os.sep, "/")
            found.append(ModuleInfo(repo, rel, mod))
        return found


def local_version(repo, rel_dir):
    """Highest semver tag of the module in rel_dir ("" if none).

    Module tags are "v1.2.3" in the root and "<dir>/v1.2.3" in a subdirectory.
    """
    prefix = "" if rel_dir == "." else rel_dir + "/"
    res = git(repo.path, "tag", "--list", prefix + "v*")
    versions = []
    for tag in res.stdout.splitlines():
        tag = tag.strip()
        if not tag.startswith(prefix):
            continue
        v = tag[len(prefix):]
        if "/" not in v and IsValid(v):
            versions.append(v)
    if not versions:
        return ""
    Sort(versions)
    return versions[-1]


def drift_of(required, local):
    """Compare the version a module requires with the highest local tag."""
    if not IsValid(required):
        return "invalid"
    if local == "":
        return "no tag"
    c = Compare(required, local)
    if c == 0:
        return "same"
    if c < 0:
        return "older"
    return "newer (untagged)"


class ModuleGraph(object):
    """Directed graph of cross-repo go module requirements."""

    def __init__(self, modules):
        self.modules = modules
        self.owner = OrderedDict()
        for info in modules:
            if info.mod.module:
                self.owner[info.mod.module] = info

    def edges(self):
        rows = []
        local_cache = {}
        for info in self.modules:
            for req, version in info.mod.requires.items():
                target = self.owner.get(req)
                if target is None or target.repo.name == info.repo.name:
                    continue
                key = (target.repo.name, target.rel_dir)
                if key not in local_cache:
                    local_cache[key] = local_version(target.repo, target.rel_dir)
                local = local_cache[key]
                row = OrderedDict()
                row["src"] = info.repo.name
                row["dst"] = target.repo.name
                row["module"] = req
                row["required"] = version
                row["local"] = local
                row["drift"] = drift_of(version, local)
                rows.append(row)
        rows.sort(key=lambda r: (r["src"], r["dst"], r["module"]))
        return rows


# ---------------------------------------------------------------------------
# Facade: the one object the scripts talk to
# ---------------------------------------------------------------------------


class PodmanEnvironment(object):
    _instance = None

    @classmethod
    def instance(cls):
        """Singleton: one environment per process, selection from PODMAN_ENV_REPOS."""
        if cls._instance is None:
            try:
                cls._instance = cls(selection=selected_names())
            except ValueError as exc:
                raise SystemExit("PODMAN_ENV_REPOS: %s" % exc)
        return cls._instance

    def __init__(self, root=ROOT, catalogue=CATALOGUE, selection=None, bus=None):
        self.root = root
        self.repos_dir = os.path.join(root, "repos")
        self.repos = RepoFactory(self.repos_dir).build(catalogue, selection)
        self.bus = bus or EventBus()
        self.invoker = Invoker(self.bus)

    def pin(self):
        return self.invoker.execute_all(
            [PinSubmodule(r, self.root) for r in self.repos])

    def latest(self):
        return self.invoker.execute_all(
            [LatestSubmodule(r, self.root) for r in self.repos])

    def make(self, target):
        return self.invoker.execute_all(
            [MakeTarget(r, target, self.root) for r in self.repos])

    def status(self):
        marks = submodule_marks(self.root)
        return [repo_status(r, marks) for r in self.repos]

    def go_graph(self):
        scanner = GoModuleScanner()
        modules = []
        for repo in self.repos:
            modules.extend(scanner.visit(repo))
        return ModuleGraph(modules)
