# -*- coding: utf-8 -*-
"""podmanlib - the Python rewrite of Go code from the podman-container-tools projects.

This is a rewrite, not a binding: nothing here imports or runs the Go code.
The Go identifiers are kept on purpose, so each Python function can be read
next to the Go function it came from. Python 2.7 and 3.x compatible.

Ported so far
  Part 1  golang.org/x/mod/semver       vendored in podman and container-libs
  Part 2  go.mod reader                 own code, Go syntax subset
  Part 3  github.com/opencontainers/go-digest v1.0.0 (validation parts)
  Part 4  container-libs image/docker/reference     (image name parsing)
  Part 6  image manifests: media types, digest, the platform of this machine,
          choosing an image from an OCI index or a Docker manifest list
  Part 7  command line helpers: flags without argparse, exit status, not-implemented
  Part 5  machine connections: pkg/machine/connection and common/pkg/config
          connections.go (ssh:// connections of a VM, podman-connections.json)

Not ported yet: FamiliarMatch (needs Go's path.Match), the Field encoding
helper, and everything else in podman, container-libs (common, image
transports and copy, storage), skopeo, buildah and podman-machine-os.

Design patterns used
  Factory      getBestReferenceType() picks the reference type that fits the parts
  Composition  Go struct embedding (namedRepository) becomes a base class
  Interfaces   Named, Tagged, NamedTagged, CanonicalReference, Digested are base classes;
               Go type assertions become isinstance() checks
  Null object  Go's nil references become None where Go returns nil
"""

from __future__ import absolute_import, division, print_function

import base64
import errno
import fcntl
import functools
import hashlib
import io
import json
import numbers
import os
import platform as hostplatform
import re
import subprocess
import sys
import tempfile
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
        self.go_version = ""           # the "go" directive, e.g. "1.26.0"
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
        elif verb == "go" and len(fields) >= 2:
            mod.go_version = fields[1]
        elif verb in ("require", "replace"):
            _apply_directive(mod, verb, fields[1:])
    return mod


# ===========================================================================
# Part 3: port of github.com/opencontainers/go-digest v1.0.0
#         (digest.go and algorithm.go, the parts the reference package needs)
# ===========================================================================

_RE_FLAGS = getattr(re, "ASCII", 0)  # Go's \w and character classes are ASCII-only


def _blen(s):
    """Go len(): the number of bytes, not characters."""
    if isinstance(s, bytes):
        return len(s)
    return len(s.encode("utf-8"))


def _g(group):
    """Go returns "" for a capture group that did not participate."""
    return "" if group is None else group


class DigestError(ValueError):
    """go-digest error. str() is the exact Go message."""


class RefError(ValueError):
    """Reference error. str() is the exact Go message."""


DIGEST_ERR_INVALID_FORMAT = "invalid checksum digest format"
DIGEST_ERR_INVALID_LENGTH = "invalid checksum digest length"
DIGEST_ERR_UNSUPPORTED = "unsupported digest algorithm"

SHA256 = "sha256"
SHA384 = "sha384"
SHA512 = "sha512"

# Go: algorithms (crypto.Hash.Size() in bytes; the hex length is twice this).
_ALGORITHM_SIZE = {SHA256: 32, SHA384: 48, SHA512: 64}

# Go: anchoredEncodedRegexps. \Z is Go's $ (Python's $ also matches before a final newline).
_ANCHORED_ENCODED = {
    SHA256: re.compile(r"^[a-f0-9]{64}\Z", _RE_FLAGS),
    SHA384: re.compile(r"^[a-f0-9]{96}\Z", _RE_FLAGS),
    SHA512: re.compile(r"^[a-f0-9]{128}\Z", _RE_FLAGS),
}

# Go: DigestRegexpAnchored. Tells "unsupported algorithm" from "invalid format".
DigestRegexpAnchored = re.compile(
    r"^[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[a-zA-Z0-9=_-]+\Z", _RE_FLAGS)


def algorithm_available(alg):
    """Go: Algorithm.Available(). Only sha256, sha384 and sha512 are known."""
    return alg in _ALGORITHM_SIZE


def algorithm_validate(alg, encoded):
    """Go: Algorithm.Validate(encoded)."""
    regex = _ANCHORED_ENCODED.get(alg)
    if regex is None:
        raise DigestError(DIGEST_ERR_UNSUPPORTED)
    if _ALGORITHM_SIZE[alg] * 2 != _blen(encoded):
        raise DigestError(DIGEST_ERR_INVALID_LENGTH)
    if regex.match(encoded) is not None:
        return
    raise DigestError(DIGEST_ERR_INVALID_FORMAT)


def digest_parse(s):
    """Go: digest.Parse(s). Returns s when valid, raises DigestError otherwise."""
    i = s.find(":")
    if i <= 0 or i + 1 == len(s):
        raise DigestError(DIGEST_ERR_INVALID_FORMAT)
    alg, encoded = s[:i], s[i + 1:]
    if not algorithm_available(alg):
        if DigestRegexpAnchored.match(s) is None:
            raise DigestError(DIGEST_ERR_INVALID_FORMAT)
        raise DigestError(DIGEST_ERR_UNSUPPORTED)
    algorithm_validate(alg, encoded)
    return s


# ===========================================================================
# Part 4: port of github.com/containers/container-libs image/docker/reference
#         (reference.go, normalize.go, regexp.go, helpers.go, regexp-additions.go)
# ===========================================================================

NAME_TOTAL_LENGTH_MAX = 255
DEFAULT_DOMAIN = "docker.io"
DEFAULT_TAG = "latest"
LEGACY_DEFAULT_DOMAIN = "index.docker.io"
OFFICIAL_REPO_NAME = "library"

# Error texts, exactly as the Go package reports them.
ERR_REFERENCE_INVALID_FORMAT = "invalid reference format"
ERR_TAG_INVALID_FORMAT = "invalid tag format"
ERR_REF_DIGEST_INVALID_FORMAT = "invalid digest format"
ERR_NAME_CONTAINS_UPPERCASE = "repository name must be lowercase"
ERR_NAME_EMPTY = "repository name must have at least one component"
ERR_NAME_TOO_LONG = "repository name must not be more than 255 characters"
ERR_NAME_NOT_CANONICAL = "repository name must be canonical"
ERR_NORMALIZE_UPPERCASE = "invalid reference format: repository name must be lowercase"


# Go: regexp.go helpers. Each returns a pattern string, as in the Go code.
def _literal(s):
    return re.escape(s)


def _expression(*res):
    return "".join(res)


def _group(*res):
    return "(?:" + _expression(*res) + ")"


def _optional(*res):
    return _group(*res) + "?"


def _repeated(*res):
    return _group(*res) + "+"


def _capture(*res):
    return "(" + _expression(*res) + ")"


def _anchored(*res):
    return "^" + _expression(*res) + r"\Z"


# Go: regexp.go patterns.
_alphaNumeric = r"[a-z0-9]+"
# Go: [-]* can match empty. Python's re backtracks exponentially on that
# (alnum+ (sep alnum+)* with an empty sep), so -+ is used. The language is the
# same: two adjacent alnum runs with an empty separator form one alnum run.
_separator = r"(?:[._]|__|-+)"
_domainComponent = r"(?:[a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])"
_tag = r"[\w][\w.-]{0,127}"
_digestPat = (r"[A-Za-z][A-Za-z0-9]*(?:[-_+.][A-Za-z][A-Za-z0-9]*)*"
              r"[:][0-9A-Fa-f]{32,}")
_identifier = r"([a-f0-9]{64})"
_shortIdentifier = r"([a-f0-9]{6,64})"

_nameComponent = _expression(
    _alphaNumeric, _optional(_repeated(_separator, _alphaNumeric)))
_domain = _expression(
    _domainComponent,
    _optional(_repeated(_literal("."), _domainComponent)),
    _optional(_literal(":"), r"[0-9]+"))
_namePat = _expression(
    _optional(_domain, _literal("/")),
    _nameComponent,
    _optional(_repeated(_literal("/"), _nameComponent)))
_anchoredName = _anchored(
    _optional(_capture(_domain), _literal("/")),
    _capture(_nameComponent, _optional(_repeated(_literal("/"), _nameComponent))))
_referencePat = _anchored(
    _capture(_namePat),
    _optional(_literal(":"), _capture(_tag)),
    _optional(_literal("@"), _capture(_digestPat)))

DomainRegexp = re.compile(_domain, _RE_FLAGS)
TagRegexp = re.compile(_tag, _RE_FLAGS)
DigestRegexp = re.compile(_digestPat, _RE_FLAGS)
NameRegexp = re.compile(_namePat, _RE_FLAGS)
ReferenceRegexp = re.compile(_referencePat, _RE_FLAGS)
IdentifierRegexp = re.compile(_identifier, _RE_FLAGS)
ShortIdentifierRegexp = re.compile(_shortIdentifier, _RE_FLAGS)
anchoredTagRegexp = re.compile(_anchored(_tag), _RE_FLAGS)
anchoredDigestRegexp = re.compile(_anchored(_digestPat), _RE_FLAGS)
anchoredNameRegexp = re.compile(_anchoredName, _RE_FLAGS)
anchoredIdentifierRegexp = re.compile(_anchored(_identifier), _RE_FLAGS)


# Go interfaces. Go's type assertions (ref.(Named)) become isinstance checks.
class Reference(object):
    def String(self):
        raise NotImplementedError


class Named(Reference):
    def Name(self):
        raise NotImplementedError


class Tagged(Reference):
    def Tag(self):
        raise NotImplementedError


class NamedTagged(Named, Tagged):
    pass


class Digested(Reference):
    def Digest(self):
        raise NotImplementedError


class CanonicalReference(Named, Digested):
    pass


class NamedRepository(Named):
    """Go: namedRepository. A named reference with domain and path."""

    def Domain(self):
        raise NotImplementedError

    def Path(self):
        raise NotImplementedError


class NormalizedNamed(Named):
    """Go: normalizedNamed. A name that can be shown in its familiar form."""

    def Familiar(self):
        raise NotImplementedError


class repository(NamedRepository, NormalizedNamed):
    def __init__(self, domain="", path=""):
        self.domain = domain
        self.path = path

    def String(self):
        return self.Name()

    def Name(self):
        if self.domain == "":
            return self.path
        return self.domain + "/" + self.path

    def Domain(self):
        return self.domain

    def Path(self):
        return self.path

    def Familiar(self):
        return familiarizeName(self)


class reference(NamedRepository, NamedTagged, CanonicalReference, NormalizedNamed):
    def __init__(self, domain, path, tag, digest):
        self.domain = domain
        self.path = path
        self.tag = tag
        self.digest = digest

    def String(self):
        return self.Name() + ":" + self.tag + "@" + self.digest

    def Name(self):
        return repository(self.domain, self.path).Name()

    def Domain(self):
        return self.domain

    def Path(self):
        return self.path

    def Tag(self):
        return self.tag

    def Digest(self):
        return self.digest

    def Familiar(self):
        fam = familiarizeName(self)
        return reference(fam.domain, fam.path, self.tag, self.digest)


class taggedReference(NamedRepository, NamedTagged, NormalizedNamed):
    def __init__(self, domain, path, tag):
        self.domain = domain
        self.path = path
        self.tag = tag

    def String(self):
        return self.Name() + ":" + self.tag

    def Name(self):
        return repository(self.domain, self.path).Name()

    def Domain(self):
        return self.domain

    def Path(self):
        return self.path

    def Tag(self):
        return self.tag

    def Familiar(self):
        fam = familiarizeName(self)
        return taggedReference(fam.domain, fam.path, self.tag)


class canonicalReference(NamedRepository, CanonicalReference, NormalizedNamed):
    def __init__(self, domain, path, digest):
        self.domain = domain
        self.path = path
        self.digest = digest

    def String(self):
        return self.Name() + "@" + self.digest

    def Name(self):
        return repository(self.domain, self.path).Name()

    def Domain(self):
        return self.domain

    def Path(self):
        return self.path

    def Digest(self):
        return self.digest

    def Familiar(self):
        fam = familiarizeName(self)
        return canonicalReference(fam.domain, fam.path, self.digest)


class digestReference(Digested):
    def __init__(self, digest):
        self.digest = digest

    def String(self):
        return self.digest

    def Digest(self):
        return self.digest


def familiarizeName(named):
    """Go: familiarizeName. Drops the default domain and the library/ prefix."""
    repo = repository(named.Domain(), named.Path())
    if repo.domain == DEFAULT_DOMAIN:
        repo.domain = ""
        parts = repo.path.split("/")
        if len(parts) == 2 and parts[0] == OFFICIAL_REPO_NAME:
            repo.path = parts[1]
    return repo


def IsNameOnly(ref):
    """Go: IsNameOnly. True when ref carries neither a tag nor a digest."""
    if isinstance(ref, NamedTagged):
        return False
    if isinstance(ref, CanonicalReference):
        return False
    return True


def FamiliarName(ref):
    if isinstance(ref, NormalizedNamed):
        return ref.Familiar().Name()
    return ref.Name()


def FamiliarString(ref):
    if isinstance(ref, NormalizedNamed):
        return ref.Familiar().String()
    return ref.String()


def IsFullIdentifier(s):
    """Go: regexp-additions.go. True if s fully matches IdentifierRegexp."""
    return anchoredIdentifierRegexp.search(s) is not None


def splitDomain(name):
    match = anchoredNameRegexp.search(name)
    if match is None:
        return "", name
    return _g(match.group(1)), _g(match.group(2))


def Domain(named):
    if isinstance(named, NamedRepository):
        return named.Domain()
    domain, _ = splitDomain(named.Name())
    return domain


def Path(named):
    if isinstance(named, NamedRepository):
        return named.Path()
    _, path = splitDomain(named.Name())
    return path


def SplitHostname(named):
    """Go: SplitHostname (deprecated in Go, kept for the port)."""
    if isinstance(named, NamedRepository):
        return named.Domain(), named.Path()
    return splitDomain(named.Name())


def getBestReferenceType(ref):
    """Go: getBestReferenceType. A factory: picks the type that fits the parts."""
    if ref.Name() == "":
        if ref.digest != "":
            return digestReference(ref.digest)
        return None
    if ref.tag == "":
        if ref.digest != "":
            return canonicalReference(ref.domain, ref.path, ref.digest)
        return repository(ref.domain, ref.path)
    if ref.digest == "":
        return taggedReference(ref.domain, ref.path, ref.tag)
    return ref


def Parse(s):
    """Go: Parse. Syntactically valid reference or RefError (DigestError for a bad digest)."""
    matches = ReferenceRegexp.search(s)
    if matches is None:
        if s == "":
            raise RefError(ERR_NAME_EMPTY)
        if ReferenceRegexp.search(s.lower()) is not None:
            raise RefError(ERR_NAME_CONTAINS_UPPERCASE)
        raise RefError(ERR_REFERENCE_INVALID_FORMAT)

    name = _g(matches.group(1))
    tag = _g(matches.group(2))
    dgst = _g(matches.group(3))
    if _blen(name) > NAME_TOTAL_LENGTH_MAX:
        raise RefError(ERR_NAME_TOO_LONG)

    nameMatch = anchoredNameRegexp.search(name)
    if nameMatch is not None:
        repo = repository(_g(nameMatch.group(1)), _g(nameMatch.group(2)))
    else:
        repo = repository("", name)

    ref = reference(repo.domain, repo.path, tag, "")
    if dgst != "":
        ref.digest = digest_parse(dgst)

    r = getBestReferenceType(ref)
    if r is None:
        raise RefError(ERR_NAME_EMPTY)
    return r


def ParseNormalizedNamed(s):
    """Go: ParseNormalizedNamed. Turns a familiar name into a fully qualified one."""
    if anchoredIdentifierRegexp.search(s) is not None:
        raise RefError("invalid repository name (%s), cannot specify 64-byte hexadecimal strings" % s)
    domain, remainder = splitDockerDomain(s)
    tagSep = remainder.find(":")
    if tagSep > -1:
        remoteName = remainder[:tagSep]
    else:
        remoteName = remainder
    if remoteName.lower() != remoteName:
        raise RefError(ERR_NORMALIZE_UPPERCASE)

    ref = Parse(domain + "/" + remainder)
    if not isinstance(ref, Named):
        raise RefError("reference %s has no name" % ref.String())
    return ref


def ParseDockerRef(ref):
    """Go: ParseDockerRef. Like ParseNormalizedNamed; tag and digest give the digest only."""
    named = ParseNormalizedNamed(ref)
    if isinstance(named, NamedTagged) and isinstance(named, CanonicalReference):
        newNamed = WithName(named.Name())
        return WithDigest(newNamed, named.Digest())
    return TagNameOnly(named)


def splitDockerDomain(name):
    """Go: splitDockerDomain. The name must already be validated."""
    i = name.find("/")
    if i == -1 or (not any(ch in ".:" for ch in name[:i]) and name[:i] != "localhost"):
        domain, remainder = DEFAULT_DOMAIN, name
    else:
        domain, remainder = name[:i], name[i + 1:]
    if domain == LEGACY_DEFAULT_DOMAIN:
        domain = DEFAULT_DOMAIN
    if domain == DEFAULT_DOMAIN and "/" not in remainder:
        remainder = OFFICIAL_REPO_NAME + "/" + remainder
    return domain, remainder


def TagNameOnly(ref):
    """Go: TagNameOnly. Adds the default tag when ref is a bare name."""
    if IsNameOnly(ref):
        return WithTag(ref, DEFAULT_TAG)
    return ref


def ParseAnyReference(ref):
    """Go: ParseAnyReference (deprecated in Go: 64 hex characters are read as sha256)."""
    if anchoredIdentifierRegexp.search(ref) is not None:
        return digestReference("sha256:" + ref)
    try:
        digest_parse(ref)
    except DigestError:
        return ParseNormalizedNamed(ref)
    return digestReference(ref)


def ParseNamed(s):
    """Go: ParseNamed. Like ParseNormalizedNamed, but s must already be canonical."""
    named = ParseNormalizedNamed(s)
    if named.String() != s:
        raise RefError(ERR_NAME_NOT_CANONICAL)
    return named


def WithName(name):
    if _blen(name) > NAME_TOTAL_LENGTH_MAX:
        raise RefError(ERR_NAME_TOO_LONG)
    match = anchoredNameRegexp.search(name)
    if match is None:
        raise RefError(ERR_REFERENCE_INVALID_FORMAT)
    return repository(_g(match.group(1)), _g(match.group(2)))


def WithTag(name, tag):
    if anchoredTagRegexp.search(tag) is None:
        raise RefError(ERR_TAG_INVALID_FORMAT)
    if isinstance(name, NamedRepository):
        domain, path = name.Domain(), name.Path()
    else:
        domain, path = "", name.Name()
    if isinstance(name, CanonicalReference):
        return reference(domain, path, tag, name.Digest())
    return taggedReference(domain, path, tag)


def WithDigest(name, dgst):
    if anchoredDigestRegexp.search(dgst) is None:
        raise RefError(ERR_REF_DIGEST_INVALID_FORMAT)
    if isinstance(name, NamedRepository):
        domain, path = name.Domain(), name.Path()
    else:
        domain, path = "", name.Name()
    if isinstance(name, Tagged):
        return reference(domain, path, name.Tag(), dgst)
    return canonicalReference(domain, path, dgst)


def TrimNamed(ref):
    """Go: TrimNamed. Removes any tag or digest from the name."""
    domain, path = SplitHostname(ref)
    return repository(domain, path)


# ===========================================================================
# Part 5: machine connections. Go: podman pkg/machine/connection (connection.go,
#         add.go) and container-libs common/pkg/config/connections.go.
#         These are the ssh:// connections of a machine (name and name-root),
#         the default connection, and the podman-connections.json file.
# ===========================================================================

LOCALHOST_IP = "127.0.0.1"                       # Go: LocalhostIP
DEFAULT_MACHINE_NAME = "podman-machine-default"  # Go: define.DefaultMachineName
CONNECTIONS_FILE = "podman-connections.json"     # Go: connectionsFile
CONFIG_DIR_NAME = "containers"                   # Go: _configPathName (storage/pkg/configfile)


class MachineConnectionError(ValueError):
    """Error of the connection code. str() is the exact Go message."""


# --- Go net/url: URL.String() for scheme, user, host and path ---------------
#
# Go's shouldEscape() is a bit table. Its rules are written out here per mode.
# The cross-check (check_machine_connections.py) compares the results with
# net/url for random input, including non-ASCII bytes.

_ENC_PATH = "path"
_ENC_HOST = "host"
_ENC_USER_PASSWORD = "userPassword"


def _should_escape(c, mode):
    """Go: shouldEscape(c, mode) for one byte value c."""
    if (0x61 <= c <= 0x7A) or (0x41 <= c <= 0x5A) or (0x30 <= c <= 0x39):
        return False                       # unreserved: alphanumeric
    ch = chr(c)
    if mode == _ENC_HOST and ch in "!$&'()*+,;=:[]<>\"":
        return False                       # sub-delims and separators allowed in a host
    if ch in "-_.~":
        return False                       # unreserved: marks
    if ch in "$&+,/:;=?@":
        if mode == _ENC_PATH:
            return ch == "?"
        if mode == _ENC_USER_PASSWORD:
            return ch in "@/?:"
        return True
    return True                            # everything else, including bytes >= 0x80


def _go_escape(s, mode):
    """Go: escape(s, mode). Non-ASCII text is escaped as its UTF-8 bytes."""
    data = s if isinstance(s, bytes) else s.encode("utf-8")
    out = []
    for b in bytearray(data):
        if _should_escape(b, mode):
            out.append("%%%02X" % b)
        else:
            out.append(chr(b))
    return "".join(out)


def _go_url_string(scheme, user, host, path):
    """Go: (*url.URL).String() for a URL with scheme, user, host and path only.

    user is None for a URL without userinfo. makeSSHURL always passes one.
    """
    out = [scheme + ":"]
    if host or path or user is not None:
        out.append("//")
    if user is not None:
        out.append(_go_escape(user, _ENC_USER_PASSWORD) + "@")
    if host:
        out.append(_go_escape(host, _ENC_HOST))
    escaped_path = "*" if path == "*" else _go_escape(path, _ENC_PATH)
    if escaped_path and escaped_path[0] != "/" and host:
        out.append("/")
    out.append(escaped_path)
    return "".join(out)


def join_host_port(host, port):
    """Go: net.JoinHostPort. IPv6 literals are put in brackets."""
    if ":" in host:
        return "[" + host + "]:" + port
    return host + ":" + port


def make_ssh_url(host, path, port, user_name):
    """Go: makeSSHURL. Returns the URL string (Go's url.URL.String())."""
    if len(port) > 0:
        hostname = join_host_port(host, port)
    else:
        hostname = host
    return _go_url_string("ssh", user_name, hostname, path)


def createConnections(name, uid, port, remote_username):
    """Go: createConnections. The rootless and the rootful connection of a machine."""
    uri = make_ssh_url(LOCALHOST_IP, "/run/user/%d/podman/podman.sock" % uid,
                       str(port), remote_username)
    uri_root = make_ssh_url(LOCALHOST_IP, "/run/podman/podman.sock", str(port), "root")
    return [(name, uri), (name + "-root", uri_root)]


# --- The connections file as a JSON object ----------------------------------


def make_destination(uri, identity="", is_machine=False):
    """Go: config.Destination with the fields the connection code sets.

    Empty Identity and false IsMachine are left out, as Go's omitempty does.
    """
    dest = {"URI": uri}
    if identity:
        dest["Identity"] = identity
    if is_machine:
        dest["IsMachine"] = True
    return dest


def canonical_json(obj):
    """JSON with sorted keys and no spaces, the form the cross-check compares.

    Non-ASCII text stays as UTF-8, and <, >, & and the two line separators
    are escaped as backslash-u sequences, as Go's encoder does.
    """
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                    ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(ch, esc)
    return text


class ConnectionsFile(object):
    """Go: config.ConnectionsFile, kept as its JSON object.

    Keys this port does not use (for example TLSCert of a destination, or the
    farm settings) are kept, so a file written here loses nothing podman needs.
    connections is None where Go's map is nil, that is, when the key is absent.
    """

    def __init__(self, raw=None):
        self.raw = raw if isinstance(raw, dict) else {}

    def _conn(self):
        conn = self.raw.get("Connection")
        if not isinstance(conn, dict):
            conn = {}
            self.raw["Connection"] = conn
        return conn

    @property
    def default(self):
        return self._conn().get("Default") or ""

    @default.setter
    def default(self, name):
        self._conn()["Default"] = name

    @property
    def connections(self):
        return self._conn().get("Connections")

    def set_connections(self, mapping):
        self._conn()["Connections"] = mapping

    def has_connection(self, name):
        conns = self.connections
        return conns is not None and name in conns

    def ensure_farm(self):
        """Go: EditConnectionConfig makes Farm.List non-nil before the callback."""
        farm = self.raw.get("Farm")
        if not isinstance(farm, dict):
            farm = {}
            self.raw["Farm"] = farm
        if not isinstance(farm.get("List"), dict):
            farm["List"] = {}

    def to_text(self):
        """The file content as Go writes it: omitempty fields left out, one newline at the end."""
        conn = dict(self._conn())
        if not conn.get("Default"):
            conn.pop("Default", None)
        if not conn.get("Connections"):
            conn.pop("Connections", None)
        farm = dict(self.raw.get("Farm") or {})
        if not farm.get("Default"):
            farm.pop("Default", None)
        if not farm.get("List"):
            farm.pop("List", None)
        out = dict(self.raw)
        out["Connection"] = conn
        out["Farm"] = farm
        return canonical_json(out) + "\n"


def connections_from_text(text, path="podman-connections.json"):
    """Go: readConnectionConf after the file was read. Empty text is a parse error, as in Go."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise MachineConnectionError('parse "%s": %s' % (path, exc))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise MachineConnectionError('parse "%s": not a JSON object' % path)
    return ConnectionsFile(raw)


def edit_connections_text(text, callback):
    """Go: EditConnectionConfig without the file. Returns the new text.

    If the callback raises, nothing is returned, so the caller keeps the old
    text. Go works the same way: it writes only when the callback succeeds.
    """
    cfg = connections_from_text(text)
    cfg.ensure_farm()
    callback(cfg)
    return cfg.to_text()


# --- Go: the functions that change the connections (callback bodies) -------


def addConnection(cfg, cons, identity, is_default):
    """Go: addConnection. cons is a list of (name, uri) pairs."""
    if len(identity) < 1:
        raise MachineConnectionError("identity must be defined")
    for i, (name, uri) in enumerate(cons):
        if cfg.has_connection(name):
            raise MachineConnectionError('cannot overwrite connection "%s"' % name)
        dst = make_destination(uri, identity, True)
        if is_default and i == 0:
            cfg.default = name
        if cfg.connections is None:
            cfg.set_connections({name: dst})
            cfg.default = name
        else:
            cfg.connections[name] = dst


def AddSSHConnectionsToPodmanSocket(cfg, uid, port, identity_path, name,
                                    remote_username, rootful, is_default,
                                    ignition_path=""):
    """Go: AddSSHConnectionsToPodmanSocket. Returns False where Go adds nothing."""
    if len(ignition_path) > 0:
        return False
    cons = createConnections(name, uid, port, remote_username)
    # The first connection of an empty file becomes the default, so order by rootful.
    if rootful:
        cons[0], cons[1] = cons[1], cons[0]
    addConnection(cfg, cons, identity_path, is_default)
    return True


def UpdateConnectionPairPort(cfg, name, port, uid, remote_username, identity_path):
    """Go: UpdateConnectionPairPort. Replaces both connections of a machine.

    Go writes into a nil map here and panics; this port starts an empty map.
    """
    cons = createConnections(name, uid, port, remote_username)
    if cfg.connections is None:
        cfg.set_connections({})
    for con_name, uri in cons:
        cfg.connections[con_name] = make_destination(uri, identity_path, True)


def updateConnection(cfg, rootful, name, rootful_name):
    """Go: updateConnection. Moves the default between a rootless and a rootful name."""
    if name == cfg.default and rootful:
        cfg.default = rootful_name
    elif rootful_name == cfg.default and not rootful:
        cfg.default = name


def UpdateConnectionIfDefault(cfg, rootful, name, rootful_name):
    updateConnection(cfg, rootful, name, rootful_name)


def removal_is_ambiguous(cfg, names):
    """True if setNewDefaultConnection would pick a connection in Go's map order.

    Go picks the first map entry, which is random. This port picks the first
    name in sorted order. Callers that compare with Go should skip these cases.
    """
    conns = cfg.connections or {}
    seen = set()
    for name in names:
        if name in seen or name not in conns:
            return False  # an error happens before the choice is made
        seen.add(name)
    remaining = [n for n in conns if n not in seen]
    return DEFAULT_MACHINE_NAME not in remaining and len(remaining) > 1


def setNewDefaultConnection(cfg, names):
    """Go: setNewDefaultConnection. Returns (destination, service name).

    The destination is None where Go returns its zero value.
    """
    for name in names:
        if cfg.has_connection(name):
            del cfg.connections[name]
        else:
            raise MachineConnectionError('unable to find connection named "%s"' % name)
        if cfg.default == name:
            cfg.default = ""
    conns = cfg.connections or {}
    if DEFAULT_MACHINE_NAME in conns:
        cfg.default = DEFAULT_MACHINE_NAME
        return conns[DEFAULT_MACHINE_NAME], DEFAULT_MACHINE_NAME
    for con in sorted(conns):
        cfg.default = con
        return conns[con], con
    return None, ""


def RemoveConnections(cfg, machines, names):
    """Go: RemoveConnections. machines maps a machine name to its rootful flag."""
    dest, service = setNewDefaultConnection(cfg, names)
    if dest is not None and dest.get("IsMachine") and service in machines:
        updateConnection(cfg, machines[service], service, service + "-root")


# --- Go: the read side -------------------------------------------------------


def GetConnection(cfg, name, default, active_service="", service_destinations=None):
    """Go: GetConnection. service_destinations stands for containers.conf destinations."""
    service_destinations = service_destinations or {}
    default_con = cfg.default or active_service
    is_default = default
    if default:
        if default_con == "":
            raise MachineConnectionError("no default connection found")
        name = default_con
    else:
        is_default = default_con == name
    conns = cfg.connections
    if conns is not None and name in conns:
        return {"name": name, "destination": conns[name],
                "default": is_default, "read_write": True}
    if name in service_destinations:
        return {"name": name, "destination": service_destinations[name],
                "default": is_default, "read_write": False}
    raise MachineConnectionError('connection "%s" not found' % name)


def GetAllConnections(cfg, active_service="", service_destinations=None):
    """Go: GetAllConnections. Sorted by name here; Go iterates a map."""
    service_destinations = service_destinations or {}
    default_con = cfg.default or active_service
    conns = cfg.connections or {}
    result = []
    for name in sorted(conns):
        result.append({"name": name, "destination": conns[name],
                       "default": default_con == name, "read_write": True})
    for name in sorted(service_destinations):
        if name in conns:
            continue  # the connections file overrides containers.conf
        result.append({"name": name, "destination": service_destinations[name],
                       "default": default_con == name, "read_write": False})
    return result


# --- Go: the file (lock, read, write) ----------------------------------------


def connections_config_path():
    """Go: connectionsConfigFile. PODMAN_CONNECTIONS_CONF wins, then XDG, then $HOME."""
    if "PODMAN_CONNECTIONS_CONF" in os.environ:
        return os.environ["PODMAN_CONNECTIONS_CONF"]
    if "XDG_CONFIG_HOME" in os.environ:
        base = os.environ["XDG_CONFIG_HOME"]
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, CONFIG_DIR_NAME, CONNECTIONS_FILE)


def read_connection_conf(path):
    """Go: readConnectionConf. A missing file is an empty config."""
    try:
        with io.open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (IOError, OSError) as exc:
        if exc.errno == errno.ENOENT:
            return ConnectionsFile()
        raise
    return connections_from_text(text, path)


def write_connection_conf(path, cfg):
    """Go: writeConnectionConf. Writes a temporary file and renames it over the old one."""
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".podman-connections-")
    os.close(fd)
    try:
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(cfg.to_text())
        os.chmod(tmp, 0o644)
        os.rename(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def EditConnectionConfig(callback, path=None):
    """Go: EditConnectionConfig. Locks, reads, calls callback, writes on success.

    The lock is the same kind Go's storage lockfile takes (POSIX record lock on
    <file>.lock), so the two can exclude each other.
    """
    path = path or connections_config_path()
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory)
    lock_fh = io.open(path + ".lock", "a")
    try:
        fcntl.lockf(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            cfg = read_connection_conf(path)
            cfg.ensure_farm()
            callback(cfg)
            write_connection_conf(path, cfg)
        finally:
            fcntl.lockf(lock_fh.fileno(), fcntl.LOCK_UN)
    finally:
        lock_fh.close()


# ===========================================================================
# Part 6: image manifests. Go: container-libs image/internal/manifest
#         (manifest.go, oci_index.go, docker_schema2_list.go) and
#         image/internal/pkg/platform (platform_matcher.go).
#         Media types, digests, the platform of this machine, and choosing the
#         image from an OCI index or a Docker manifest list.
# ===========================================================================

DOCKER_V2_SCHEMA1_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v1+json"
DOCKER_V2_SCHEMA1_SIGNED_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v1+prettyjws"
DOCKER_V2_SCHEMA2_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_V2_SCHEMA2_CONFIG_MEDIA_TYPE = "application/vnd.docker.container.image.v1+json"
DOCKER_V2_LIST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.list.v2+json"
OCI1_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI1_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI1_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI1_INSTANCE_ANNOTATION_COMPRESSION_ZSTD = "io.github.containers.compression.zstd"

# Go: types.OptionalBool (iota order)
OPTIONAL_BOOL_UNDEFINED = 0
OPTIONAL_BOOL_TRUE = 1
OPTIONAL_BOOL_FALSE = 2

MAX_INT64 = 2 ** 63 - 1       # Go: math.MaxInt on 64-bit platforms
MIN_INT64 = -(2 ** 63)

# Go: platform_matcher.go
COMPATIBILITY = {"arm": ["v8", "v7", "v6", "v5"], "arm64": ["v8"]}
GOARCH_BY_MACHINE = {
    "x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
    "armv7l": "arm", "armv6l": "arm", "i686": "386", "i386": "386",
    "ppc64le": "ppc64le", "s390x": "s390x", "riscv64": "riscv64",
}
GOOS_BY_SYS_PLATFORM = {"linux": "linux", "darwin": "darwin", "win32": "windows",
                        "freebsd": "freebsd"}

_STRING_TYPES = (type(u""), type(""))  # text in Python 2 and 3


class ManifestError(ValueError):
    """Error of the manifest code. str() is the Go message."""


class _JSONTypeError(ValueError):
    """A JSON value that Go's json.Unmarshal would refuse for the Go type."""


# --- JSON, the way Go decodes it into typed structs --------------------------


def _reject_constant(name):
    raise ValueError("invalid JSON constant " + name)


def _go_decode(blob):
    """Decode with the standard parser. NaN and Infinity are refused, as in Go."""
    text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else blob
    return json.loads(text, parse_constant=_reject_constant)


def _is_int64(value):
    return (isinstance(value, numbers.Integral) and not isinstance(value, bool)
            and MIN_INT64 <= value <= MAX_INT64)


def _is_base64(value):
    try:
        base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        return False
    return True


def _is_go_bytes(value):
    """Go: []byte from JSON. A base64 string, or an array of numbers from 0 to 255."""
    if isinstance(value, _STRING_TYPES):
        return _is_base64(value)
    if isinstance(value, list):
        return all(isinstance(x, numbers.Integral) and not isinstance(x, bool)
                   and 0 <= x <= 255 for x in value)
    return False


def _string_field(obj, key):
    """A string field of a decoded object. null means "" (Go leaves the zero value)."""
    value = obj.get(key)
    if value is None:
        return ""
    if not isinstance(value, _STRING_TYPES):
        raise _JSONTypeError(key)
    return value


PLATFORM_SPEC = (("architecture", "str"), ("os", "str"), ("os.version", "str"),
                 ("os.features", "strlist"), ("variant", "str"))
DESCRIPTOR_SPEC = (("mediaType", "str"), ("digest", "str"), ("size", "int64"),
                   ("urls", "strlist"), ("annotations", "strmap"), ("data", "b64"),
                   ("platform", "platform"), ("artifactType", "str"))


def _typed_object(value, spec):
    """Check the fields of a JSON object against the Go field kinds in spec.

    null is accepted everywhere, as Go does. Unknown keys are ignored.
    """
    if value is None:
        return
    if not isinstance(value, dict):
        raise _JSONTypeError("object expected")
    for key, kind in spec:
        field = value.get(key)
        if field is None:
            continue
        if kind == "platform":
            _typed_object(field, PLATFORM_SPEC)
        elif kind == "str" and not isinstance(field, _STRING_TYPES):
            raise _JSONTypeError(key)
        elif kind == "int64" and not _is_int64(field):
            raise _JSONTypeError(key)
        elif kind == "strlist":
            if not isinstance(field, list):
                raise _JSONTypeError(key)
            if not all(x is None or isinstance(x, _STRING_TYPES) for x in field):
                raise _JSONTypeError(key)
        elif kind == "strmap":
            if not isinstance(field, dict):
                raise _JSONTypeError(key)
            if not all(x is None or isinstance(x, _STRING_TYPES) for x in field.values()):
                raise _JSONTypeError(key)
        elif kind == "b64" and not _is_go_bytes(field):
            raise _JSONTypeError(key)


def _descriptor_list(value):
    """Go: []imgspecv1.Descriptor. Returns the list; raises if a field has the wrong type."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _JSONTypeError("array expected")
    for item in value:
        _typed_object(item, DESCRIPTOR_SPEC)
    return value


# --- Media types -------------------------------------------------------------


def NormalizedMIMEType(value):
    """Go: NormalizedMIMEType. Unknown values are treated as schema 1 signed."""
    if value == "application/json":
        return DOCKER_V2_SCHEMA1_SIGNED_MEDIA_TYPE
    if value in (DOCKER_V2_SCHEMA1_MEDIA_TYPE, DOCKER_V2_SCHEMA1_SIGNED_MEDIA_TYPE,
                 OCI1_MANIFEST_MEDIA_TYPE, OCI1_INDEX_MEDIA_TYPE,
                 DOCKER_V2_SCHEMA2_MEDIA_TYPE, DOCKER_V2_LIST_MEDIA_TYPE):
        return value
    return DOCKER_V2_SCHEMA1_SIGNED_MEDIA_TYPE


def _guess_schema2(meta):
    """Go: the schemaVersion 2 branch of GuessMIMEType."""
    config = meta.get("config")
    if config is None:
        config_media = ""
    elif isinstance(config, dict):
        config_media = _string_field(config, "mediaType")
    else:
        raise _JSONTypeError("config")
    if config_media == OCI1_CONFIG_MEDIA_TYPE:
        return OCI1_MANIFEST_MEDIA_TYPE
    if config_media == DOCKER_V2_SCHEMA2_CONFIG_MEDIA_TYPE:
        return DOCKER_V2_SCHEMA2_MEDIA_TYPE
    if len(_descriptor_list(meta.get("manifests"))) != 0:
        if config_media == "":
            return OCI1_INDEX_MEDIA_TYPE
        return config_media
    return OCI1_MANIFEST_MEDIA_TYPE


def GuessMIMEType(manifest):
    """Go: GuessMIMEType. Returns "" where Go's json.Unmarshal would fail.

    Not ported: the schema 1 check on the signature block beyond "signatures != nil".
    """
    try:
        meta = _go_decode(manifest)
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            return ""
        media_type = _string_field(meta, "mediaType")
        schema_version = meta.get("schemaVersion")
        if schema_version is None:
            schema_version = 0
        elif not _is_int64(schema_version):
            return ""
        if media_type in (DOCKER_V2_SCHEMA2_MEDIA_TYPE, DOCKER_V2_LIST_MEDIA_TYPE,
                          OCI1_MANIFEST_MEDIA_TYPE, OCI1_INDEX_MEDIA_TYPE):
            return media_type
        if schema_version == 1:
            if meta.get("signatures") is not None:
                return DOCKER_V2_SCHEMA1_SIGNED_MEDIA_TYPE
            return DOCKER_V2_SCHEMA1_MEDIA_TYPE
        if schema_version == 2:
            return _guess_schema2(meta)
    except ValueError:
        return ""
    return ""


def Digest(manifest_blob):
    """Go: Digest (canonical sha256 of the blob). Schema 1 signed blobs are not ported."""
    if GuessMIMEType(manifest_blob) == DOCKER_V2_SCHEMA1_SIGNED_MEDIA_TYPE:
        raise NotImplementedError("schema 1 signed manifests are not ported")
    data = manifest_blob if isinstance(manifest_blob, bytes) else manifest_blob.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def MatchesDigest(manifest_blob, expected_digest):
    return Digest(manifest_blob) == expected_digest


# --- The platform of this machine (platform_matcher.go) ----------------------


def getCPUInfo(pattern):
    """Go: getCPUInfo. Reads /proc/cpuinfo and returns the value of the first match."""
    try:
        with io.open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\r\n").split(":")
                if len(parts) > 1 and parts[0].strip().lower() == pattern.lower():
                    return parts[1].strip()
    except (IOError, OSError) as exc:
        raise ManifestError(str(exc))
    raise ManifestError("getCPUInfo for pattern: %s not found" % pattern)


def getCPUVariantFromArch(arch):
    if arch == "arm64":
        return "v8"
    if arch == "arm":
        return "v7"
    return ""


def getCPUVariantLinuxArm():
    try:
        variant = getCPUInfo("Cpu architecture")
    except ManifestError:
        return ""
    low = variant.lower()
    if low in ("8", "aarch64"):
        return "v8"
    if low in ("7m", "?(12)", "?(13)", "?(14)", "?(15)", "?(16)", "?(17)"):
        return "v7"
    if low == "7":
        try:
            model = getCPUInfo("model name")
        except ManifestError:
            return ""
        if model.lower().startswith("armv6-compatible"):
            return "v6"
        return "v7"
    if low in ("6", "6tej"):
        return "v6"
    if low in ("5", "5t", "5te", "5tej"):
        return "v5"
    if low in ("4", "4t"):
        return "v4"
    if low == "3":
        return "v3"
    return ""


def getCPUVariant(goos, goarch):
    if goos != "linux":
        return getCPUVariantFromArch(goarch)
    if goarch in ("arm", "arm64"):
        return getCPUVariantLinuxArm()
    return ""


def host_goarch():
    machine = hostplatform.machine()
    return GOARCH_BY_MACHINE.get(machine, machine)


def host_goos():
    return GOOS_BY_SYS_PLATFORM.get(sys.platform, sys.platform)


def WantedPlatforms(ctx=None):
    """Go: platform.WantedPlatforms. ctx is a dict with the Go field names, or None.

    Without ArchitectureChoice the host architecture and variant are used.
    Returns a list of dicts with the keys os, architecture and variant.
    """
    ctx = ctx or {}
    wanted_arch = host_goarch()
    wanted_variant = ""
    if ctx.get("ArchitectureChoice"):
        wanted_arch = ctx["ArchitectureChoice"]
    else:
        wanted_variant = getCPUVariant(host_goos(), host_goarch())
    if ctx.get("VariantChoice"):
        wanted_variant = ctx["VariantChoice"]
    wanted_os = host_goos()
    if ctx.get("OSChoice"):
        wanted_os = ctx["OSChoice"]

    variants = []
    if wanted_variant != "":
        order = COMPATIBILITY.get(wanted_arch)
        if order is not None and wanted_variant in order:
            variants = order[order.index(wanted_variant):]
        if not variants:
            variants = [wanted_variant]
        variants.append("")
    else:
        variants.append("")
        if wanted_arch in COMPATIBILITY:
            variants.extend(COMPATIBILITY[wanted_arch])
    return [{"os": wanted_os, "architecture": wanted_arch, "variant": v} for v in variants]


def _platform_key(p):
    """The three fields that decide a match (Go compares Architecture, OS and Variant)."""
    p = p or {}
    return {"os": p.get("os") or "", "architecture": p.get("architecture") or "",
            "variant": p.get("variant") or ""}


def MatchesPlatform(image, wanted):
    return _platform_key(image) == _platform_key(wanted)


# --- Choosing the image for this machine -------------------------------------


def instanceIsZstd(descriptor):
    annotations = descriptor.get("annotations") or {}
    return annotations.get(OCI1_INSTANCE_ANNOTATION_COMPRESSION_ZSTD) == "true"


MAX_INT = MAX_INT64


def _candidate_is_preferred_over(ic, other, prefer_gzip):
    """Go: instanceCandidate.isPreferredOver."""
    if ic["platform_index"] != other["platform_index"]:
        return ic["platform_index"] < other["platform_index"]
    if ic["is_zstd"] != other["is_zstd"]:
        if prefer_gzip != OPTIONAL_BOOL_TRUE:
            return ic["is_zstd"]
        return not ic["is_zstd"]
    if ic["manifest_position"] != other["manifest_position"]:
        return ic["manifest_position"] < other["manifest_position"]
    raise RuntimeError("internal error: invalid comparison between two candidates")


def OCI1IndexChooseInstance(manifests, ctx=None, prefer_gzip=OPTIONAL_BOOL_FALSE):
    """Go: (*OCI1IndexPublic).ChooseInstance and chooseInstance.

    manifests is a list of dicts with the keys digest, platform (or None) and
    annotations. Returns the digest of the best image for the wanted platforms.
    """
    wanted = WantedPlatforms(ctx)
    best = None
    for position, d in enumerate(manifests):
        candidate = {"platform_index": MAX_INT, "manifest_position": position,
                     "is_zstd": instanceIsZstd(d), "digest": d.get("digest") or ""}
        platform_dict = d.get("platform")
        if platform_dict is not None:
            index = -1
            for i, w in enumerate(wanted):
                if MatchesPlatform(platform_dict, w):
                    index = i
                    break
            if index == -1:
                continue
            candidate["platform_index"] = index
        if best is None or _candidate_is_preferred_over(candidate, best, prefer_gzip):
            best = candidate
    if best is not None:
        return best["digest"]
    first = wanted[0]
    raise ManifestError('no image found in image index for architecture "%s", '
                        'variant "%s", OS "%s"'
                        % (first["architecture"], first["variant"], first["os"]))


def Schema2ListChooseInstance(manifests, ctx=None):
    """Go: (*Schema2ListPublic).ChooseInstance.

    manifests is a list of dicts with the keys digest and platform (a dict
    with architecture, os, variant; None counts as an empty platform).
    """
    wanted = WantedPlatforms(ctx)
    for w in wanted:
        for d in manifests:
            if MatchesPlatform(d.get("platform") or {}, w):
                return d.get("digest") or ""
    first = wanted[0]
    raise ManifestError('no image found in manifest list for architecture "%s", '
                        'variant "%s", OS "%s"'
                        % (first["architecture"], first["variant"], first["os"]))


# ===========================================================================
# Part 7: command line helpers. No argparse: flags are parsed here, by hand.
# ===========================================================================

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3


class CliUsageError(ValueError):
    """A command line that cannot be used. The message says what is wrong."""


def take_flags(argv, spec):
    """Parse --name, --name value, --name=value and boolean flags.

    spec maps each flag to "value" (takes an argument) or "bool". Returns
    (options, positionals). Unknown flags and missing values are usage errors.
    """
    options = {}
    positionals = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            name, eq, inline = arg.partition("=")
            if name not in spec:
                raise CliUsageError("unknown flag %s" % name)
            if spec[name] == "bool":
                if not eq:
                    options[name] = True
                elif inline.lower() in ("true", "1"):
                    options[name] = True
                elif inline.lower() in ("false", "0"):
                    options[name] = False
                else:
                    raise CliUsageError("%s takes true or false" % name)
            else:
                if eq:
                    options[name] = inline
                else:
                    if i + 1 >= len(argv):
                        raise CliUsageError("%s needs a value" % name)
                    i += 1
                    options[name] = argv[i]
        else:
            positionals.append(arg)
        i += 1
    return options, positionals


def not_implemented(program, command, roadmap=""):
    """Tell the user that a command is not ported yet. Returns the exit status."""
    message = "%s %s: not implemented in this rewrite yet" % (program, command)
    if roadmap:
        message += " (see docs/ROADMAP.md: %s)" % roadmap
    sys.stderr.write(message + "\n")
    return EXIT_NOT_IMPLEMENTED


# ===========================================================================
# Shell helper for the check scripts (not part of the rewrite itself)
# ===========================================================================


class CommandResult(object):
    def __init__(self, argv, returncode, stdout, stderr):
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self):
        return self.returncode == 0


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
