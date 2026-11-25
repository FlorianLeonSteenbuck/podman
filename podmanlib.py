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
  Part 10 engine server: containers and images on a unix socket, through the OCI runtime
  Part 9  OCI runtime: crun first, runc second; spec and bundle; calls the runtime program
  Part 7  command line helpers: flags without argparse, exit status, not-implemented
  Part 8  container engine client: libpod REST API over a unix socket, output formats
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
import socket
import socketserver
import struct
import tempfile
import threading
import time
import shutil
import gzip
import tarfile
from collections import OrderedDict

try:  # Python 3
    import http.client as httpclient
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import quote as url_quote, urlencode, unquote, parse_qs
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
except ImportError:  # Python 2
    import httplib as httpclient
    from BaseHTTPServer import BaseHTTPRequestHandler
    from urllib import quote as url_quote, urlencode, unquote
    from urlparse import parse_qs
    from urllib2 import HTTPError, Request, urlopen


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
# Part 8: container engine client. Go: podman's libpod REST API (the server
#         side is pkg/api, the client side is what `podman --remote` does).
#         HTTP over a unix socket, for example the socket a qbox box forwards.
# ===========================================================================

class EngineError(RuntimeError):
    """An API error. status is the HTTP status (0 if the socket was unreachable)."""

    def __init__(self, status, message):
        RuntimeError.__init__(self, message)
        self.status = status
        self.message = message


class UnixHTTPConnection(httpclient.HTTPConnection):
    """HTTP connection over a unix domain socket (Go: the default http client with a unix dialer)."""

    def __init__(self, socket_path, timeout=60):
        httpclient.HTTPConnection.__init__(self, "localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def socket_path_of(endpoint):
    """Go: the unix:// form of a podman endpoint, reduced to the socket path."""
    if endpoint.startswith("unix://"):
        return endpoint[len("unix://"):]
    raise ValueError("only unix:// endpoints are supported here, got %s" % endpoint)


def _error_message(data, status):
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
        if isinstance(obj, dict):
            return str(obj.get("cause") or obj.get("message") or obj)
    except ValueError:
        pass
    text = data.decode("utf-8", "replace").strip()
    return text or "HTTP %d" % status


def engine_api_version(info):
    """The API version a /version or /libpod/version answer reports ("" if none)."""
    for component in info.get("Components") or []:
        if component.get("Name") == "Podman Engine":
            return (component.get("Details") or {}).get("APIVersion", "")
    return info.get("ApiVersion", "")


def engine_version(info):
    """The engine version from a version answer ("" if none)."""
    for component in info.get("Components") or []:
        if component.get("Name") == "Podman Engine":
            return component.get("Version", "")
    return info.get("Version", "")


class EngineClient(object):
    """Libpod REST client.

    Podman 4.x only routes paths with the API version in front (/v4.9.0/libpod/...).
    The version comes from /version, which is not versioned. /_ping and /version are
    used as they are.
    """

    def __init__(self, socket_path, timeout=60):
        self.socket_path = socket_path
        self.timeout = timeout
        self._prefix = None

    def prefix(self):
        if self._prefix is None:
            prefix = ""
            try:
                status, data = self.request("GET", "/version")
                if status == 200 and data:
                    api = engine_api_version(json.loads(data.decode("utf-8", "replace")))
                    if api:
                        prefix = "/v" + api
            except (ValueError, EngineError):
                pass
            self._prefix = prefix
        return self._prefix

    def request(self, method, path, query=None, body=None):
        """Returns (status, body bytes). Socket errors become EngineError with status 0."""
        if path.startswith("/libpod/"):
            path = self.prefix() + path
        if query:
            pairs = []
            for key, value in sorted(query.items()):
                if value is None:
                    continue
                if isinstance(value, bool):
                    value = "true" if value else "false"
                pairs.append((key, value))
            if pairs:
                path = path + "?" + urlencode(pairs)
        conn = UnixHTTPConnection(self.socket_path, self.timeout)
        headers = {}
        if body is not None:
            body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        except (socket.error, OSError, httpclient.HTTPException) as exc:
            raise EngineError(0, "cannot reach %s: %s" % (self.socket_path, exc))
        finally:
            conn.close()

    def call(self, method, path, query=None, expect=(200,), body=None):
        status, data = self.request(method, path, query, body)
        if status not in expect:
            raise EngineError(status, _error_message(data, status))
        return status, data

    def get_json(self, path, query=None):
        _, data = self.call("GET", path, query)
        return json.loads(data.decode("utf-8", "replace")) if data else None

    def ping(self):
        status, _ = self.request("GET", "/_ping")
        return status == 200

    def version(self):
        return self.get_json("/libpod/version")

    def list_containers(self, all_containers=False):
        return self.get_json("/libpod/containers/json", {"all": all_containers}) or []

    def list_images(self):
        return self.get_json("/libpod/images/json") or []

    def inspect_container(self, name):
        return self.get_json("/libpod/containers/%s/json" % url_quote(name, safe=""))

    def inspect_image(self, name):
        return self.get_json("/libpod/images/%s/json" % url_quote(name, safe=""))

    def image_exists(self, name):
        status, _ = self.request("GET", "/libpod/images/%s/exists" % url_quote(name, safe=""))
        return status in (200, 204)

    def pull(self, reference, quiet=True):
        """The server answers 200 even when the pull failed: the error is in the body."""
        _, data = self.call("POST", "/libpod/images/pull", {"reference": reference, "quiet": quiet})
        report = json.loads(data.decode("utf-8", "replace")) if data else {}
        if isinstance(report, dict) and report.get("error"):
            raise EngineError(500, str(report["error"]))
        return report

    def container_action(self, name, action, query=None):
        """start, stop, restart (stop and restart take timeout in the query)."""
        self.call("POST", "/libpod/containers/%s/%s" % (url_quote(name, safe=""), action),
                  query, expect=(200, 204, 304))

    def remove_container(self, name, force=False):
        self.call("DELETE", "/libpod/containers/%s" % url_quote(name, safe=""),
                  {"force": force}, expect=(200, 204))

    def remove_image(self, name, force=False):
        self.call("DELETE", "/libpod/images/%s" % url_quote(name, safe=""),
                  {"force": force}, expect=(200,))

    def create_container(self, spec):
        """Go: POST /libpod/containers/create with a SpecGenerator. Returns the new id."""
        _, data = self.call("POST", "/libpod/containers/create", None,
                            expect=(200, 201), body=spec)
        return json.loads(data.decode("utf-8", "replace"))["Id"]

    def wait_container(self, name, condition="stopped"):
        """Blocks until the condition holds; returns the exit code."""
        _, data = self.call("POST", "/libpod/containers/%s/wait" % url_quote(name, safe=""),
                            {"condition": condition}, expect=(200,))
        return int(json.loads(data.decode("utf-8", "replace")))

    def container_logs(self, name):
        _, data = self.call("GET", "/libpod/containers/%s/logs" % url_quote(name, safe=""),
                            {"stdout": True, "stderr": True})
        return data

    def stream_logs(self, name, follow=False):
        """Yields (stream, bytes) as the container writes them. With follow, the generator
        ends when the container has stopped; without it, after the output so far."""
        path = "%s/libpod/containers/%s/logs?%s" % (
            self.prefix(), url_quote(name, safe=""),
            urlencode([("stdout", "true"), ("stderr", "true"),
                       ("follow", "true" if follow else "false")]))
        conn = UnixHTTPConnection(self.socket_path, None)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            if response.status != 200:
                raise EngineError(response.status,
                                  _error_message(response.read(), response.status))
            while True:
                header = _read_exact(response, 8)
                if len(header) < 8:
                    return
                head = bytearray(header)
                size = (head[4] << 24) | (head[5] << 16) | (head[6] << 8) | head[7]
                payload = _read_exact(response, size)
                yield head[0], payload
                if len(payload) < size:
                    return
        except (socket.error, OSError, httpclient.HTTPException) as exc:
            raise EngineError(0, "cannot reach %s: %s" % (self.socket_path, exc))
        finally:
            conn.close()

    def create_pod(self, spec):
        """POST /libpod/pods/create. Returns the pod id."""
        _, data = self.call("POST", "/libpod/pods/create", None, expect=(200, 201), body=spec)
        return json.loads(data.decode("utf-8", "replace"))["Id"]

    def list_pods(self):
        return self.get_json("/libpod/pods/json") or []

    def inspect_pod(self, name):
        return self.get_json("/libpod/pods/%s/json" % url_quote(name, safe=""))

    def pod_action(self, name, action, query=None):
        """start or stop."""
        self.call("POST", "/libpod/pods/%s/%s" % (url_quote(name, safe=""), action),
                  query, expect=(200, 204, 304))

    def remove_pod(self, name, force=False):
        _, data = self.call("DELETE", "/libpod/pods/%s" % url_quote(name, safe=""),
                            {"force": force}, expect=(200,))
        return json.loads(data.decode("utf-8", "replace")) if data else {}


def _read_exact(stream, count):
    """Reads count bytes, or fewer if the stream ends first."""
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            break
        data += chunk
    return data


def demux_frames(data):
    """Log frames of a container without a tty: an 8-byte header (stream, 0, 0, 0, size as
    big-endian uint32), then the payload. Returns a list of (stream, bytes), stream 1 stdout, 2 stderr."""
    buf = bytearray(data)
    frames = []
    i = 0
    while i + 8 <= len(buf):
        size = (buf[i + 4] << 24) | (buf[i + 5] << 16) | (buf[i + 6] << 8) | buf[i + 7]
        frames.append((buf[i], bytes(buf[i + 8:i + 8 + size])))
        i += 8 + size
    return frames


def human_size(num_bytes):
    """Podman's size column: decimal units with two decimals, e.g. 9.12MB."""
    size = float(num_bytes or 0)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return ("%d%s" % (size, unit)) if unit == "B" else ("%.2f%s" % (size, unit))
        size /= 1000.0
    return "%dB" % size


def epoch_of(value):
    """Seconds since the epoch for an API timestamp: an RFC 3339 string or a number. None if unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return None if value <= -62135596800 else float(value)
    match = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$",
                     str(value))
    if match is None:
        return None
    import calendar as _calendar
    import time as _time
    stamp = _calendar.timegm(_time.strptime(match.group(1) + " " + match.group(2),
                                            "%Y-%m-%d %H:%M:%S"))
    frac = match.group(3)
    return stamp + (float("0" + frac) if frac else 0.0)


def time_ago(epoch, now=None):
    """Relative time as podman prints it: "2 minutes ago", "3 weeks ago"."""
    import time as _time
    if epoch is None:
        return ""
    now = _time.time() if now is None else now
    try:
        seconds = int(now - float(epoch))
    except (TypeError, ValueError):
        return str(epoch)
    for limit, unit in ((60, "second"), (3600, "minute"), (86400, "hour"),
                        (86400 * 7, "day"), (86400 * 30, "week"), (86400 * 365, "month")):
        if seconds < limit:
            break
    else:
        return "%d years ago" % (seconds // (86400 * 365))
    divisor = {"second": 1, "minute": 60, "hour": 3600, "day": 86400,
               "week": 86400 * 7, "month": 86400 * 30}[unit]
    count = max(seconds // divisor, 1)
    if count < 2 and unit == "second":
        return "Less than a second ago"
    return "%d %s%s ago" % (count, unit, "" if count == 1 else "s")


def format_table(headers, rows):
    """Plain columns, separated by two spaces, like podman's default output."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines) + "\n"


_TEMPLATE_FIELD = re.compile(r"\{\{\s*\.([A-Za-z0-9_.]+)\s*\}\}")


def format_template(template, obj):
    """The part of Go templates that --format uses here: {{.Field}}, {{.A.B}}, \\t and \\n.

    A missing field prints "<no value>", as Go does.
    """
    def field(match):
        value = obj
        for part in match.group(1).split("."):
            if not isinstance(value, dict) or part not in value:
                return "<no value>"
            value = value[part]
        if value is None:
            return "<no value>"
        return str(value)
    text = _TEMPLATE_FIELD.sub(field, template)
    return text.replace("\\t", "\t").replace("\\n", "\n")


# ===========================================================================
# Part 9: OCI runtime. Go: podman gives a bundle (config.json and a rootfs) to an
#         OCI runtime. The runtime is found in podman's order: crun (C) first,
#         then runc (Go). This part only calls the runtime program; it does not
#         replace it. Spec generation is minimal: pid, ipc, uts and mount
#         namespaces, the default capability set, /proc, /dev, /dev/pts, /sys.
# ===========================================================================

OCI_RUNTIME_ORDER = ("crun", "runc")  # podman's default, see common/pkg/config/default.go
OCI_SPEC_VERSION = "1.0.2"
OCI_DEFAULT_ENV = ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                   "TERM=xterm"]
OCI_DEFAULT_CAPABILITIES = [
    "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FSETID", "CAP_FOWNER", "CAP_MKNOD",
    "CAP_NET_RAW", "CAP_SETGID", "CAP_SETUID", "CAP_SETFCAP", "CAP_SETPCAP",
    "CAP_NET_BIND_SERVICE", "CAP_SYS_CHROOT", "CAP_KILL", "CAP_AUDIT_WRITE",
]


class OCIRuntimeError(RuntimeError):
    """The runtime program failed. str() has its stderr, or the status."""


def find_executable(name, search_path=None):
    """The full path of an executable on the search path, or None."""
    if search_path is None:
        search_path = os.environ.get("PATH", "")
    for directory in search_path.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_oci_runtime(search_path=None, order=OCI_RUNTIME_ORDER):
    """crun if it is installed, else runc, else None: the order podman uses."""
    for name in order:
        path = find_executable(name, search_path)
        if path is not None:
            return path
    return None


def make_spec(args, env=None, cwd="/", hostname="pmpy", capabilities=None, network="host"):
    """A minimal OCI runtime spec (config.json) for a process in ./rootfs.

    network "host" shares the host network namespace; "none" gives the process an empty
    one (loopback only)."""
    if network not in ("host", "none"):
        raise ValueError("unknown network mode %s" % network)
    caps = list(OCI_DEFAULT_CAPABILITIES if capabilities is None else capabilities)
    namespaces = [{"type": "pid"}, {"type": "ipc"}, {"type": "uts"}, {"type": "mount"}]
    if network == "none":
        namespaces.append({"type": "network"})
    return {
        "ociVersion": OCI_SPEC_VERSION,
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": list(args),
            "env": list(OCI_DEFAULT_ENV if env is None else env),
            "cwd": cwd,
            "noNewPrivileges": True,
            "capabilities": {"bounding": caps, "effective": caps, "permitted": caps,
                             "inheritable": [], "ambient": []},
        },
        "root": {"path": "rootfs", "readonly": False},
        "hostname": hostname,
        "mounts": [
            {"destination": "/proc", "type": "proc", "source": "proc"},
            {"destination": "/dev", "type": "tmpfs", "source": "tmpfs",
             "options": ["nosuid", "strictatime", "mode=755", "size=65536k"]},
            {"destination": "/dev/pts", "type": "devpts", "source": "devpts",
             "options": ["nosuid", "noexec", "newinstance", "ptmxmode=0666", "mode=0620"]},
            {"destination": "/sys", "type": "sysfs", "source": "sysfs",
             "options": ["nosuid", "noexec", "nodev", "ro"]},
        ],
        "linux": {"namespaces": namespaces},
    }


def write_bundle(bundle_dir, spec):
    """Writes config.json into the bundle directory. The rootfs must already be in place."""
    if not os.path.isdir(bundle_dir):
        os.makedirs(bundle_dir)
    with io.open(os.path.join(bundle_dir, "config.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    return bundle_dir


class OCIRuntime(object):
    """The runtime program, called the way podman calls it (create, start, state, kill, delete)."""

    def __init__(self, path=None):
        self.path = path or find_oci_runtime()
        if self.path is None:
            raise OCIRuntimeError("no OCI runtime found (install crun or runc)")

    def _call(self, args, cwd=None):
        result = run([self.path] + list(args), cwd=cwd)
        if not result.ok:
            raise OCIRuntimeError("%s %s: %s" % (os.path.basename(self.path), args[0],
                                                 result.stderr.strip() or result.returncode))
        return result

    def create(self, container_id, bundle):
        self._call(["create", "--bundle", bundle, container_id])

    def start(self, container_id):
        self._call(["start", container_id])

    def state(self, container_id):
        return json.loads(self._call(["state", container_id]).stdout)

    def kill(self, container_id, signal="TERM"):
        self._call(["kill", container_id, signal])

    def delete(self, container_id, force=False):
        args = ["delete"] + (["--force"] if force else []) + [container_id]
        self._call(args)

    def spawn(self, container_id, bundle, stdout, stderr):
        """Starts the container in the background; stdout and stderr are open files."""
        return subprocess.Popen([self.path, "run", "--bundle", bundle, container_id],
                                stdout=stdout, stderr=stderr)

    def run(self, container_id, bundle):
        """create, start and wait in one call. Returns (exit status, stdout, stderr)."""
        result = run([self.path, "run", "--bundle", bundle, container_id])
        return result.returncode, result.stdout, result.stderr


# ===========================================================================
# Part 10: an engine server. It serves the part of the libpod REST API that
#          podman.py uses, on a unix socket: containers, pods and images from a
#          local rootfs store. Containers run through the OCI runtime of part 9,
#          one supervisor thread per running container applies its restart policy,
#          and published ports are forwarded on the host by PortForwarder.
#          Networking: host (the default) and none. Pod members share the host
#          network. Not here: bridge networking, image pulls, exec, stdin, udp.
#          Everything else answers 501. The state is one JSON file. Containers that
#          were running when the server stopped are reported as exited; resume()
#          starts those again if their restart policy says so.
# ===========================================================================

ENGINE_API_VERSION = "4.9.0"
ENGINE_VERSION = "0.1.0-pmpy"
ENGINE_STOP_TIMEOUT = 10
ENGINE_NETWORKS = ("host", "none")
ENGINE_RESTART_POLICIES = ("no", "on-failure", "always", "unless-stopped")
ENGINE_BACKOFF_FIRST = 0.1  # seconds before the first restart; doubles on each restart
ENGINE_BACKOFF_MAX = 30.0
ENGINE_FOLLOW_INTERVAL = 0.05
ENGINE_CHUNK = 65536


class ServerError(Exception):
    """An error answer: HTTP status and the message podman prints."""

    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def rfc3339(epoch):
    if not epoch:
        return "0001-01-01T00:00:00Z"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def parse_query(query):
    return parse_qs(query, keep_blank_values=True)


def new_container_id():
    return hashlib.sha256(os.urandom(32)).hexdigest()


def directory_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                total += os.path.getsize(full)
    return total


def frame(stream, payload):
    """One log frame in the raw stream format: stream, 0, 0, 0, size (big-endian uint32), payload."""
    return struct.pack(">BxxxI", stream, len(payload)) + payload


def parse_restart(policy, retries=None):
    """A restart policy as podman spells it: no, always, unless-stopped, on-failure or
    on-failure:N. Returns (name, retries)."""
    text = (policy or "no").strip().lower()
    name, _, count = text.partition(":")
    if name not in ENGINE_RESTART_POLICIES:
        raise ServerError(400, "unknown restart policy %s" % text)
    if count:
        if name != "on-failure":
            raise ServerError(400, "only on-failure takes a retry count")
        retries = count
    retries = int(retries or 0)
    if retries < 0:
        raise ServerError(400, "the retry count must not be negative")
    return name, retries


def normalize_ports(items):
    """Podman's portmappings, one entry per port (a range is expanded). Only tcp is supported."""
    out = []
    for item in items or []:
        protocol = (item.get("protocol") or "tcp").lower()
        if protocol != "tcp":
            raise ServerError(501, "pmpy publishes only tcp ports, not %s" % protocol)
        count = int(item.get("range") or 1)
        container_port = int(item["container_port"])
        host_port = int(item.get("host_port") or container_port)
        if (count < 1 or container_port < 1 or host_port < 1
                or container_port + count - 1 > 65535 or host_port + count - 1 > 65535):
            raise ServerError(400, "bad port mapping %d:%d" % (host_port, container_port))
        for i in range(count):
            out.append({"container_port": container_port + i, "host_port": host_port + i,
                        "host_ip": item.get("host_ip") or "", "protocol": "tcp"})
    return out


def read_from(path, offset):
    """The output of a log file after offset, and the new offset. A file that became
    shorter (the container was started again and its log truncated) is read from the start."""
    if not os.path.exists(path):
        return offset, b""
    with io.open(path, "rb") as fh:
        fh.seek(0, 2)
        if fh.tell() < offset:
            offset = 0
        fh.seek(offset)
        payload = fh.read(ENGINE_CHUNK)
    return offset + len(payload), payload


def _copy_stream(src, dst):
    try:
        while True:
            data = src.recv(ENGINE_CHUNK)
            if not data:
                break
            dst.sendall(data)
    except socket.error:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except socket.error:
        pass


class PortForwarder(object):
    """Accepts TCP on host_ip:host_port and copies every connection to 127.0.0.1:container_port.

    This is how a published port works here. A container in host networking shares the
    host's network namespace, so its port is reachable on loopback."""

    def __init__(self, host_ip, host_port, container_port):
        self.host_port = host_port
        self.container_port = container_port
        self.closed = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((host_ip or "0.0.0.0", host_port))
        except socket.error:
            self.sock.close()
            raise
        self.sock.listen(64)
        self.sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._accept)
        self.thread.daemon = True
        self.thread.start()

    def _accept(self):
        while not self.closed:
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except socket.error:
                break
            worker = threading.Thread(target=self._pipe, args=(client,))
            worker.daemon = True
            worker.start()

    def _pipe(self, client):
        client.settimeout(None)
        try:
            target = socket.create_connection(("127.0.0.1", self.container_port), 5)
        except socket.error:
            client.close()
            return
        target.settimeout(None)
        back = threading.Thread(target=_copy_stream, args=(target, client))
        back.daemon = True
        back.start()
        _copy_stream(client, target)
        back.join()
        client.close()
        target.close()

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except socket.error:
            pass


class EngineServer(object):
    """Containers, pods and images in one JSON state file under home, run through an OCI runtime."""

    def __init__(self, home, runtime_path=None):
        self.home = os.path.abspath(home)
        self.runtime_path = runtime_path
        self._runtime = None
        self.lock = threading.RLock()
        self.done = {}      # container id -> Event, set when its supervisor has finished
        self.procs = {}     # container id -> Popen of the running OCI runtime
        self.wake = {}      # container id -> Event that cuts a restart delay short
        self.forwards = {}  # container or pod id -> [PortForwarder]
        for sub in ("bundles", "logs", "rootfs"):
            path = os.path.join(self.home, sub)
            if not os.path.isdir(path):
                os.makedirs(path)
        self.state_path = os.path.join(self.home, "state.json")
        self.state = {"containers": {}, "images": {}, "pods": {}}
        if os.path.exists(self.state_path):
            with io.open(self.state_path, encoding="utf-8") as fh:
                self.state = json.loads(fh.read())
        self.state.setdefault("pods", {})
        now = time.time()
        for c in self.state["containers"].values():
            c.setdefault("network", "host")
            c.setdefault("pod", "")
            c.setdefault("restart", {"policy": "no", "retries": 0})
            c.setdefault("restart_count", 0)
            c.setdefault("stop_requested", False)
            c.setdefault("interrupted", False)
            if c["status"] == "running":
                c.update(status="exited", exit_code=-1, finished=now, interrupted=True)
        self._save()

    # --- state file ------------------------------------------------------------

    def _save(self):
        tmp = self.state_path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.state, indent=2, sort_keys=True) + "\n")
        os.rename(tmp, self.state_path)

    def runtime(self):
        if self._runtime is None:
            try:
                self._runtime = OCIRuntime(self.runtime_path)
            except OCIRuntimeError as exc:
                raise ServerError(500, str(exc))
        return self._runtime

    # --- ports -----------------------------------------------------------------

    def _publish(self, owner, mappings):
        """Opens the host ports of a container or pod. Called with the lock held."""
        forwarders = []
        for mapping in mappings:
            try:
                forwarders.append(PortForwarder(mapping["host_ip"], mapping["host_port"],
                                                mapping["container_port"]))
            except socket.error as exc:
                for forwarder in forwarders:
                    forwarder.close()
                raise ServerError(500, "cannot publish port %d: %s" % (mapping["host_port"], exc))
        self.forwards[owner] = forwarders

    def _unpublish(self, owner):
        for forwarder in self.forwards.pop(owner, []):
            forwarder.close()

    # --- images ----------------------------------------------------------------

    def image_import(self, name, rootfs):
        """Makes rootfs (a directory) an image under the normalized name. Returns that name."""
        try:
            ref = image_key(name)
        except ValueError as exc:
            raise ServerError(400, str(exc))
        rootfs = os.path.abspath(rootfs)
        if not os.path.isdir(rootfs):
            raise ServerError(400, "no root filesystem at %s" % rootfs)
        link = os.path.join(self.home, "rootfs", hashlib.sha256(ref.encode("utf-8")).hexdigest()[:24])
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(rootfs, link)
        with self.lock:
            self.state["images"][ref] = {
                "Id": hashlib.sha256((ref + rootfs).encode("utf-8")).hexdigest(),
                "Created": time.time(), "Size": directory_size(rootfs), "rootfs": link}
            self._save()
        return ref

    def _image(self, name):
        try:
            ref = image_key(name)
        except ValueError as exc:
            raise ServerError(400, str(exc))
        image = self.state["images"].get(ref)
        if image is None:
            raise ServerError(404, "image not known")
        return ref, image

    def _blob(self, client, digest):
        """A blob from the local blob cache if it is there and intact, else from the registry."""
        path = os.path.join(self.home, "blobs", digest.replace(":", "/"))
        if os.path.exists(path):
            with io.open(path, "rb") as fh:
                data = fh.read()
            if Digest(data) == digest:
                return data
        data = client.blob(digest)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with io.open(path, "wb") as fh:
            fh.write(data)
        return data

    def image_pull(self, reference):
        """Pulls an image into the store: its layers unpacked into a rootfs directory, its
        config (environment, command, working directory) kept. Returns (key, image id)."""
        try:
            named = ParseNormalizedNamed(reference)
            key = image_key(reference)
        except ValueError as exc:
            raise ServerError(400, str(exc))
        if isinstance(named, Digested):
            selector = named.Digest()
        elif isinstance(named, Tagged):
            selector = named.Tag()
        else:
            selector = "latest"
        host = REGISTRY_HOST_ALIASES.get(named.Domain(), named.Domain())
        client = RegistryClient(host, named.Path())
        try:
            digest, media, body = client.manifest(selector)
            chosen = choose_manifest(media, body)
            if chosen is not None:
                digest, media, body = client.manifest(chosen)
            manifest = json.loads(body.decode("utf-8"))
            config_digest = manifest["config"]["digest"]
            config = json.loads(self._blob(client, config_digest).decode("utf-8"))
            image_id = config_digest[len("sha256:"):]
            rootfs = os.path.join(self.home, "images", image_id, "rootfs")
            if not os.path.isdir(rootfs):
                staging = rootfs + ".partial"
                if os.path.exists(staging):
                    shutil.rmtree(staging)
                os.makedirs(staging)
                for layer in manifest.get("layers") or []:
                    apply_layer(self._blob(client, layer["digest"]), staging)
                os.rename(staging, rootfs)
        except (RegistryError, ManifestError, NotImplementedError, ValueError, KeyError) as exc:
            raise ServerError(500, "pull %s: %s" % (reference, exc))
        cfg = config.get("config") or {}
        with self.lock:
            self.state["images"][key] = {
                "Id": image_id, "Created": time.time(), "Size": directory_size(rootfs),
                "rootfs": rootfs, "Env": cfg.get("Env") or [],
                "Cmd": cfg.get("Cmd") or [], "Entrypoint": cfg.get("Entrypoint") or [],
                "WorkingDir": cfg.get("WorkingDir") or "",
                "RepoDigests": ["%s@%s" % (named.Name(), digest)]}
            self._save()
        return key, image_id

    # --- pods ------------------------------------------------------------------

    def _pod(self, key):
        pods = self.state["pods"]
        if key in pods:
            return pods[key]
        for pod in pods.values():
            if pod["name"] == key:
                return pod
        matches = [p for pid, p in pods.items() if len(key) >= 4 and pid.startswith(key)]
        if len(matches) == 1:
            return matches[0]
        raise ServerError(404, "no pod with name or ID %s found: no such pod" % key)

    def _members(self, pod):
        return sorted((c for c in self.state["containers"].values() if c["pod"] == pod["id"]),
                      key=lambda c: c["name"])

    def _pod_name(self, c):
        pod = self.state["pods"].get(c["pod"]) if c["pod"] else None
        return pod["name"] if pod else ""

    def pod_status(self, pod):
        states = [c["status"] for c in self._members(pod)]
        if "running" in states:
            return "Running"
        if "exited" in states:
            return "Exited"
        return "Created"

    def pod_json(self, pod):
        return {
            "Id": pod["id"], "Name": pod["name"], "Status": self.pod_status(pod),
            "Created": rfc3339(pod["created"]), "InfraId": "", "Labels": None,
            "Containers": [{"Id": c["id"], "Names": [c["name"]], "Status": c["status"]}
                           for c in self._members(pod)],
            "Ports": pod["ports"], "Networks": [],
        }

    def pod_create(self, spec):
        name = spec.get("name") or ""
        network = spec.get("network") or "host"
        if network != "host":
            raise ServerError(501, "pmpy pods share the host network only")
        ports = normalize_ports(spec.get("portmappings"))
        with self.lock:
            pid = new_container_id()
            name = name or pid[:12]
            if any(p["name"] == name for p in self.state["pods"].values()):
                raise ServerError(409, "the name %s is already in use" % name)
            self.state["pods"][pid] = {"id": pid, "name": name, "created": time.time(),
                                       "ports": ports}
            self._save()
        return pid

    def pod_start(self, key):
        pod = self._pod(key)
        with self.lock:
            if pod["ports"] and pod["id"] not in self.forwards:
                self._publish(pod["id"], pod["ports"])
        for c in self._members(pod):
            if c["status"] in ("created", "exited"):
                self.start(c["id"])

    def pod_stop(self, key, timeout):
        pod = self._pod(key)
        for c in self._members(pod):
            self.stop(c["id"], timeout)
        with self.lock:
            self._unpublish(pod["id"])

    def pod_remove(self, key, force):
        pod = self._pod(key)
        members = self._members(pod)
        if not force and any(c["status"] == "running" for c in members):
            raise ServerError(409, "pod %s has running containers; use force" % pod["name"])
        for c in members:
            self.delete(c["id"], True)
        with self.lock:
            self._unpublish(pod["id"])
            del self.state["pods"][pod["id"]]
            self._save()
        return [c["id"] for c in members]

    # --- containers ------------------------------------------------------------

    def _container(self, key):
        containers = self.state["containers"]
        if key in containers:
            return containers[key]
        for c in containers.values():
            if c["name"] == key:
                return c
        matches = [c for cid, c in containers.items() if len(key) >= 4 and cid.startswith(key)]
        if len(matches) == 1:
            return matches[0]
        raise ServerError(404, "no container with name or ID %s found: no such container" % key)

    def _ports_of(self, c):
        if c["pod"] and c["pod"] in self.state["pods"]:
            return self.state["pods"][c["pod"]]["ports"]
        return c["ports"]

    def container_json(self, c):
        return {
            "Id": c["id"], "Name": c["name"], "Image": c["image"],
            "Created": rfc3339(c["created"]),
            "Config": {"Cmd": c["command"], "Env": c["env"], "WorkingDir": c["work_dir"]},
            "State": {"Status": c["status"], "Running": c["status"] == "running",
                      "ExitCode": c["exit_code"], "StartedAt": rfc3339(c["started"]),
                      "FinishedAt": rfc3339(c["finished"]), "Pid": 0},
            "Mounts": c["mounts"], "Pod": c["pod"], "RestartCount": c["restart_count"],
            "HostConfig": {"NetworkMode": c["network"],
                           "RestartPolicy": {"Name": c["restart"]["policy"],
                                             "MaximumRetryCount": c["restart"]["retries"]}},
        }

    def list_entry(self, c):
        return {
            "Id": c["id"], "Names": [c["name"]], "Image": c["image"], "Command": c["command"],
            "Created": rfc3339(c["created"]), "State": c["status"], "Status": "",
            "StartedAt": int(c["started"] or 0), "ExitedAt": int(c["finished"] or 0),
            "ExitCode": c["exit_code"], "Exited": c["status"] == "exited",
            "Ports": [dict(p, host_ip=p.get("host_ip", "")) for p in self._ports_of(c)],
            "PodName": self._pod_name(c), "Labels": None,
        }

    def create(self, spec):
        image_ref, image = self._image(spec.get("image") or "")
        command = list(spec.get("command") or [])
        if not command:
            command = list(image.get("Entrypoint") or []) + list(image.get("Cmd") or [])
        if not command:
            raise ServerError(400, "no command given and the image has none")
        network = spec.get("network") or "host"
        if network not in ENGINE_NETWORKS:
            raise ServerError(501, "pmpy does not implement network mode %s (host or none)" % network)
        ports = normalize_ports(spec.get("portmappings"))
        policy, retries = parse_restart(spec.get("restart_policy"), spec.get("restart_retries"))
        with self.lock:
            pod = self._pod(spec["pod"]) if spec.get("pod") else None
            if pod is not None:
                if network != "host":
                    raise ServerError(400, "a container in a pod uses the network of its pod")
                if ports:
                    raise ServerError(400, "publish the ports on the pod, not on a container in it")
            elif ports and network == "none":
                raise ServerError(400, "port publishing needs host networking, not none")
            cid = new_container_id()
            name = spec.get("name") or cid[:12]
            if any(c["name"] == name for c in self.state["containers"].values()):
                raise ServerError(409, "the name %s is already in use" % name)
            env = dict(item.split("=", 1) if "=" in item else (item, "")
                       for item in list(OCI_DEFAULT_ENV) + list(image.get("Env") or []))
            env.update(spec.get("env") or {})
            env_list = ["%s=%s" % (k, v) for k, v in sorted(env.items())]
            work_dir = spec.get("work_dir") or image.get("WorkingDir") or "/"
            mounts = []
            for mount in spec.get("mounts") or []:
                source = os.path.abspath(mount.get("source", ""))
                if not os.path.exists(source):
                    raise ServerError(400, "mount source %s does not exist" % source)
                mounts.append({"destination": mount["destination"], "type": "bind",
                               "source": source, "options": mount.get("options") or ["rw"]})
            bundle = os.path.join(self.home, "bundles", cid)
            os.makedirs(bundle)
            # each container gets its own copy of the image (vfs: no shared layers yet)
            shutil.copytree(image["rootfs"], os.path.join(bundle, "rootfs"), symlinks=True)
            oci = make_spec(command, env=env_list, cwd=work_dir, hostname=cid[:12], network=network)
            oci["mounts"].extend(mounts)
            write_bundle(bundle, oci)
            self.state["containers"][cid] = {
                "id": cid, "name": name, "image": image_ref, "command": command,
                "env": env_list, "work_dir": work_dir, "created": time.time(), "started": 0,
                "finished": 0, "status": "created", "exit_code": 0,
                "ports": [] if pod is not None else ports, "mounts": mounts, "bundle": bundle,
                "network": network, "pod": pod["id"] if pod is not None else "",
                "restart": {"policy": policy, "retries": retries}, "restart_count": 0,
                "stop_requested": False, "interrupted": False}
            self._save()
        return cid, []

    def start(self, key):
        c = self._container(key)
        with self.lock:
            if c["status"] == "running":
                return
            if c["status"] not in ("created", "exited"):
                raise ServerError(409, "container %s cannot be started" % c["name"])
            if c["ports"] and not c["pod"]:
                self._publish(c["id"], c["ports"])
            c.update(status="running", started=time.time(), finished=0, restart_count=0,
                     stop_requested=False, interrupted=False)
            event = threading.Event()
            self.done[c["id"]] = event
            self._save()
        thread = threading.Thread(target=self._run, args=(c["id"], event))
        thread.daemon = True
        thread.start()

    def _should_restart(self, c, code):
        policy = c["restart"]["policy"]
        if policy in ("always", "unless-stopped"):
            return True
        if policy == "on-failure":
            retries = c["restart"]["retries"]
            return code != 0 and (retries == 0 or c["restart_count"] < retries)
        return False

    def _finish(self, c):
        """The container has stopped for good. Called with the lock held."""
        c["status"] = "exited"
        if not c["pod"]:
            self._unpublish(c["id"])

    def _run(self, cid, event):
        """The supervisor of one container: runs the OCI runtime and restarts it as its policy says."""
        while True:
            with self.lock:
                c = self.state["containers"].get(cid)
                if c is None:
                    break
                bundle = c["bundle"]
            try:
                self.runtime().delete(cid, force=True)  # left over from an earlier run, if any
            except (OCIRuntimeError, ServerError):
                pass
            code = self._run_once(cid, bundle)
            with self.lock:
                c["exit_code"] = code
                c["finished"] = time.time()
                again = not c["stop_requested"] and self._should_restart(c, code)
                if again:
                    c["restart_count"] += 1
                    delay = min(ENGINE_BACKOFF_FIRST * (2 ** (c["restart_count"] - 1)),
                                ENGINE_BACKOFF_MAX)
                    wake = threading.Event()
                    self.wake[cid] = wake
                else:
                    self._finish(c)
                self._save()
            if not again:
                break
            wake.wait(delay)
            with self.lock:
                self.wake.pop(cid, None)
                if c["stop_requested"]:
                    self._finish(c)
                    self._save()
                    break
        event.set()

    def _run_once(self, cid, bundle):
        """One run of the OCI runtime, its output going to the log files. Returns the exit status."""
        logs = os.path.join(self.home, "logs")
        out = io.open(os.path.join(logs, cid + ".stdout"), "wb")
        err = io.open(os.path.join(logs, cid + ".stderr"), "wb")
        try:
            try:
                proc = self.runtime().spawn(cid, bundle, out, err)
            except (OSError, ServerError) as exc:
                err.write(("pmpy: cannot start the OCI runtime: %s\n" % exc).encode("utf-8"))
                return 127
            with self.lock:
                self.procs[cid] = proc
            code = proc.wait()
            with self.lock:
                self.procs.pop(cid, None)
        finally:
            out.close()
            err.close()
        if code < 0:  # killed by a signal: the shell convention, 128 + signal
            code = 128 - code
        return code

    def stop(self, key, timeout):
        c = self._container(key)
        cid = c["id"]
        with self.lock:
            if c["status"] != "running":
                return
            c["stop_requested"] = True
            self._save()
            event = self.done.get(cid)
            wake = self.wake.get(cid)
        if wake is not None:
            wake.set()
        if event is None:
            return
        try:
            self.runtime().kill(cid, "TERM")
        except (OCIRuntimeError, ServerError):
            pass  # already gone, or waiting for a restart
        if not event.wait(timeout):
            try:
                self.runtime().kill(cid, "KILL")
            except (OCIRuntimeError, ServerError):
                pass
            event.wait(ENGINE_STOP_TIMEOUT)

    def wait(self, key):
        c = self._container(key)
        event = self.done.get(c["id"])
        if c["status"] == "running" and event is not None:
            event.wait()
        return c["exit_code"]

    def logs(self, key, follow=False):
        """The output so far as frames. With follow, a generator of frames that ends when the
        container has stopped. Unknown containers are an error here, before any output."""
        c = self._container(key)
        if follow:
            return self._follow(c["id"])
        out = []
        for stream, suffix in ((1, ".stdout"), (2, ".stderr")):
            _, payload = read_from(os.path.join(self.home, "logs", c["id"] + suffix), 0)
            if payload:
                out.append(frame(stream, payload))
        return b"".join(out)

    def _follow(self, cid):
        paths = {1: os.path.join(self.home, "logs", cid + ".stdout"),
                 2: os.path.join(self.home, "logs", cid + ".stderr")}
        offsets = {1: 0, 2: 0}
        while True:
            event = self.done.get(cid)
            finished = event is None or event.is_set()
            sent = False
            for stream in (1, 2):
                offsets[stream], payload = read_from(paths[stream], offsets[stream])
                if payload:
                    sent = True
                    yield frame(stream, payload)
            if finished and not sent:
                return
            if not sent:
                time.sleep(ENGINE_FOLLOW_INTERVAL)

    def delete(self, key, force):
        c = self._container(key)
        if c["status"] == "running":
            if not force:
                raise ServerError(409, "container %s is running; use force" % c["name"])
            self.stop(c["id"], 0)
        with self.lock:
            self._unpublish(c["id"])
            shutil.rmtree(c["bundle"], ignore_errors=True)
            for suffix in (".stdout", ".stderr"):
                path = os.path.join(self.home, "logs", c["id"] + suffix)
                if os.path.exists(path):
                    os.remove(path)
            del self.state["containers"][c["id"]]
            self._save()

    def resume(self):
        """After a restart of the server: starts again the containers that were running and
        whose restart policy is always or unless-stopped. Returns (name, error) for failures."""
        failed = []
        with self.lock:
            again = [c for c in self.state["containers"].values()
                     if c["interrupted"] and c["restart"]["policy"] in ("always", "unless-stopped")
                     and not c["stop_requested"]]
            for c in self.state["containers"].values():
                c["interrupted"] = False
            self._save()
        for c in again:
            try:
                self.start(c["id"])
            except ServerError as exc:
                failed.append((c["name"], exc.message))
        return failed

    # --- HTTP answers ----------------------------------------------------------

    def handle(self, method, raw_path, body):
        """Returns (status, content type, body): bytes, or for a followed log an iterator of
        byte chunks that the HTTP handler sends as they come."""
        path, _, query = raw_path.partition("?")
        path = re.sub(r"^/v[0-9][0-9.]*(?=/)", "", path)
        try:
            for route_method, pattern, handler in self.routes():
                if route_method != method:
                    continue
                match = re.match(pattern, path)
                if match is not None:
                    return handler(match.groups(), parse_query(query), body)
            if path.startswith("/libpod/"):
                raise ServerError(501, "pmpy does not implement %s %s yet" % (method, path))
            return 404, "text/plain", b"Not Found"
        except ServerError as exc:
            data = json.dumps({"cause": exc.message, "message": exc.message,
                               "response": exc.status}).encode("utf-8")
            return exc.status, "application/json", data
        except OCIRuntimeError as exc:
            data = json.dumps({"cause": str(exc), "message": str(exc),
                               "response": 500}).encode("utf-8")
            return 500, "application/json", data
        except (ValueError, KeyError, TypeError) as exc:
            data = json.dumps({"cause": str(exc), "message": str(exc),
                               "response": 400}).encode("utf-8")
            return 400, "application/json", data

    def routes(self):
        return [
            ("GET", r"^/_ping$", self.h_ping),
            ("HEAD", r"^/_ping$", self.h_ping),
            ("GET", r"^/(libpod/)?version$", self.h_version),
            ("GET", r"^/libpod/containers/json$", self.h_list_containers),
            ("POST", r"^/libpod/containers/create$", self.h_create),
            ("GET", r"^/libpod/containers/([^/]+)/json$", self.h_inspect_container),
            ("POST", r"^/libpod/containers/([^/]+)/start$", self.h_start),
            ("POST", r"^/libpod/containers/([^/]+)/stop$", self.h_stop),
            ("POST", r"^/libpod/containers/([^/]+)/wait$", self.h_wait),
            ("GET", r"^/libpod/containers/([^/]+)/logs$", self.h_logs),
            ("DELETE", r"^/libpod/containers/([^/]+)$", self.h_delete),
            ("POST", r"^/libpod/pods/create$", self.h_pod_create),
            ("GET", r"^/libpod/pods/json$", self.h_pod_list),
            ("GET", r"^/libpod/pods/([^/]+)/exists$", self.h_pod_exists),
            ("GET", r"^/libpod/pods/([^/]+)/json$", self.h_pod_inspect),
            ("POST", r"^/libpod/pods/([^/]+)/start$", self.h_pod_start),
            ("POST", r"^/libpod/pods/([^/]+)/stop$", self.h_pod_stop),
            ("DELETE", r"^/libpod/pods/([^/]+)$", self.h_pod_delete),
            ("GET", r"^/libpod/images/json$", self.h_list_images),
            ("GET", r"^/libpod/images/([^/]+)/exists$", self.h_image_exists),
            ("GET", r"^/libpod/images/([^/]+)/json$", self.h_inspect_image),
            ("DELETE", r"^/libpod/images/([^/]+)$", self.h_delete_image),
            ("POST", r"^/libpod/images/pull$", self.h_pull),
        ]

    def _json(self, obj, status=200):
        return status, "application/json", json.dumps(obj).encode("utf-8")

    def _key(self, groups):
        return unquote(groups[0])

    def _flag(self, query, name):
        return name in query and query[name][0] in ("true", "1")

    def h_ping(self, groups, query, body):
        return 200, "text/plain", b"OK"

    def h_version(self, groups, query, body):
        return self._json({
            "Platform": {"Name": "linux/%s" % host_goarch()},
            "Components": [{"Name": "Podman Engine", "Version": ENGINE_VERSION,
                            "Details": {"APIVersion": ENGINE_API_VERSION,
                                        "MinAPIVersion": ENGINE_API_VERSION}}]})

    def h_list_containers(self, groups, query, body):
        everything = self._flag(query, "all")
        with self.lock:
            rows = [self.list_entry(c) for c in self.state["containers"].values()
                    if everything or c["status"] == "running"]
        return self._json(sorted(rows, key=lambda r: r["Names"][0]))

    def h_create(self, groups, query, body):
        spec = json.loads(body.decode("utf-8")) if body else {}
        cid, warnings = self.create(spec)
        return self._json({"Id": cid, "Warnings": warnings}, 201)

    def h_inspect_container(self, groups, query, body):
        return self._json(self.container_json(self._container(self._key(groups))))

    def h_start(self, groups, query, body):
        self.start(self._key(groups))
        return 204, "text/plain", b""

    def h_stop(self, groups, query, body):
        timeout = int(query["timeout"][0]) if "timeout" in query else ENGINE_STOP_TIMEOUT
        self.stop(self._key(groups), timeout)
        return 204, "text/plain", b""

    def h_wait(self, groups, query, body):
        return self._json(self.wait(self._key(groups)))

    def h_logs(self, groups, query, body):
        return 200, "application/vnd.docker.raw-stream", self.logs(self._key(groups),
                                                                   self._flag(query, "follow"))

    def h_delete(self, groups, query, body):
        self.delete(self._key(groups), self._flag(query, "force"))
        return 204, "text/plain", b""

    def h_pod_create(self, groups, query, body):
        spec = json.loads(body.decode("utf-8")) if body else {}
        return self._json({"Id": self.pod_create(spec)}, 201)

    def h_pod_list(self, groups, query, body):
        with self.lock:
            rows = [self.pod_json(p) for p in self.state["pods"].values()]
        return self._json(sorted(rows, key=lambda r: r["Name"]))

    def h_pod_exists(self, groups, query, body):
        try:
            self._pod(self._key(groups))
        except ServerError:
            return 404, "text/plain", b""
        return 204, "text/plain", b""

    def h_pod_inspect(self, groups, query, body):
        return self._json(self.pod_json(self._pod(self._key(groups))))

    def h_pod_start(self, groups, query, body):
        self.pod_start(self._key(groups))
        return 204, "text/plain", b""

    def h_pod_stop(self, groups, query, body):
        timeout = int(query["timeout"][0]) if "timeout" in query else ENGINE_STOP_TIMEOUT
        self.pod_stop(self._key(groups), timeout)
        return 204, "text/plain", b""

    def h_pod_delete(self, groups, query, body):
        removed = self.pod_remove(self._key(groups), self._flag(query, "force"))
        return self._json({"Id": self._key(groups), "Err": None, "RemovedContainers": removed})

    def h_list_images(self, groups, query, body):
        rows = []
        for ref, image in sorted(self.state["images"].items()):
            rows.append({"Id": image["Id"], "RepoTags": [ref], "Names": [ref],
                         "Created": int(image["Created"]), "Size": image["Size"]})
        return self._json(rows)

    def h_image_exists(self, groups, query, body):
        try:
            self._image(unquote(groups[0]))
        except ServerError:
            return 404, "text/plain", b""
        return 204, "text/plain", b""

    def h_inspect_image(self, groups, query, body):
        ref, image = self._image(unquote(groups[0]))
        return self._json({"Id": image["Id"], "RepoTags": [ref], "Created": rfc3339(image["Created"]),
                           "Size": image["Size"], "Architecture": host_goarch(), "Os": "linux"})

    def h_delete_image(self, groups, query, body):
        ref, image = self._image(unquote(groups[0]))
        with self.lock:
            del self.state["images"][ref]
            shared = any(other["Id"] == image["Id"] for other in self.state["images"].values())
            if not shared:
                if os.path.islink(image["rootfs"]):
                    os.remove(image["rootfs"])
                elif os.path.isdir(image["rootfs"]):
                    shutil.rmtree(os.path.dirname(image["rootfs"]), ignore_errors=True)
            self._save()
        return self._json([{"Untagged": ref}])

    def h_pull(self, groups, query, body):
        reference = query["reference"][0] if "reference" in query else ""
        if not reference:
            raise ServerError(400, "pull needs a reference")
        key, image_id = self.image_pull(reference)
        return self._json({"id": image_id, "images": [key]})


class EngineHTTPHandler(BaseHTTPRequestHandler):
    """HTTP/1.0 over the unix socket; the answer comes from EngineServer.handle."""

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, content_type, data = self.server.engine.handle(self.command, self.path, body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("API-Version", ENGINE_API_VERSION)
        if isinstance(data, bytes):
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD" and data:
                self.wfile.write(data)
            return
        # a stream (followed logs): no length; the connection closes when the stream ends
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for chunk in data:
                self.wfile.write(chunk)
                self.wfile.flush()
        except socket.error:
            pass  # the client went away

    do_GET = _answer
    do_POST = _answer
    do_DELETE = _answer
    do_HEAD = _answer

    def address_string(self):
        return "unix"

    def log_message(self, fmt, *args):
        pass


class EngineUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path, engine):
        if os.path.exists(socket_path):
            os.remove(socket_path)
        socketserver.UnixStreamServer.__init__(self, socket_path, EngineHTTPHandler)
        os.chmod(socket_path, 0o660)
        self.engine = engine


# ===========================================================================
# Part 11: pulling images from a registry (roadmap 1.3 and 1.5). RegistryClient
#          speaks the registry v2 API with bearer tokens, which docker.io and most
#          registries use. A manifest list or an OCI index is resolved to this
#          machine's platform with the functions of part 6. Blobs are checked
#          against their digest. Layers are applied in order to a rootfs directory,
#          with whiteouts; symlinks on the way to an entry are followed inside the
#          rootfs only, as a chroot would. Not here: zstd layers, schema 1, basic
#          auth, mirrors, credentials, insecure registries, and layers that are
#          streamed to disk (they are read into memory).
# ===========================================================================

REGISTRY_ACCEPT = ", ".join([OCI1_INDEX_MEDIA_TYPE, DOCKER_V2_LIST_MEDIA_TYPE,
                             OCI1_MANIFEST_MEDIA_TYPE, DOCKER_V2_SCHEMA2_MEDIA_TYPE])
REGISTRY_HOST_ALIASES = {"docker.io": "registry-1.docker.io"}
REGISTRY_TIMEOUT = 60
LAYER_MAX_SYMLINKS = 40


class RegistryError(RuntimeError):
    """A registry or pull failed. The message says what the registry answered."""


def image_key(name):
    """The name an image is stored under: the normalized reference, with :latest if it has
    neither a tag nor a digest."""
    named = ParseNormalizedNamed(name)
    key = named.String()
    if not isinstance(named, (Tagged, Digested)):
        key += ":latest"
    return key


def parse_challenge(value):
    """WWW-Authenticate: Bearer realm="...",service="...". Returns (scheme, params)."""
    scheme, _, rest = value.strip().partition(" ")
    params = {}
    for match in re.finditer(r'([A-Za-z_]+)="([^"]*)"', rest):
        params[match.group(1)] = match.group(2)
    return scheme.lower(), params


def _lower_headers(headers):
    return dict((k.lower(), v) for k, v in headers.items())


class RegistryClient(object):
    """The registry v2 API for one repository, anonymous, with bearer tokens."""

    def __init__(self, host, repository, scheme=None, timeout=REGISTRY_TIMEOUT):
        self.host = host
        self.repository = repository
        if scheme is None:
            local = host.split(":")[0] in ("localhost", "127.0.0.1")
            scheme = "http" if local else "https"
        self.base = "%s://%s/v2/" % (scheme, host)
        self.timeout = timeout
        self.token = None

    def _request(self, url, headers):
        try:
            response = urlopen(Request(url, headers=headers), timeout=self.timeout)
            return response.getcode(), _lower_headers(response.headers), response.read()
        except HTTPError as exc:
            return exc.code, _lower_headers(exc.headers), exc.read()
        except (IOError, OSError) as exc:  # URLError is one of these
            raise RegistryError("cannot reach %s: %s" % (self.host, exc))

    def _authorize(self, challenge):
        scheme, params = parse_challenge(challenge)
        if scheme != "bearer" or "realm" not in params:
            raise RegistryError("%s wants %s authentication; only bearer tokens are supported"
                                % (self.host, scheme or "no"))
        query = [("scope", "repository:%s:pull" % self.repository)]
        if "service" in params:
            query.insert(0, ("service", params["service"]))
        status, _, body = self._request(params["realm"] + "?" + urlencode(query), {})
        if status != 200:
            raise RegistryError("token request for %s failed: HTTP %d" % (self.repository, status))
        answer = json.loads(body.decode("utf-8"))
        self.token = answer.get("token") or answer.get("access_token")
        if not self.token:
            raise RegistryError("the token service gave no token")

    def get(self, path, accept=None):
        """GET under /v2/. Returns (headers, body). A 401 asks for a token once."""
        url = self.base + path
        headers = {}
        if accept:
            headers["Accept"] = accept
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        status, hdrs, body = self._request(url, headers)
        if status == 401 and self.token is None:
            self._authorize(hdrs.get("www-authenticate", ""))
            headers["Authorization"] = "Bearer " + self.token
            status, hdrs, body = self._request(url, headers)
        if status == 401:
            raise RegistryError("%s: access denied for %s" % (self.host, path))
        if status == 404:
            raise RegistryError("%s: %s not found" % (self.host, path))
        if status != 200:
            raise RegistryError("%s: HTTP %d for %s" % (self.host, status, path))
        return hdrs, body

    def manifest(self, reference):
        """The manifest for a tag or a digest: (digest, media type, bytes)."""
        headers, body = self.get("%s/manifests/%s" % (self.repository, reference), REGISTRY_ACCEPT)
        digest = Digest(body)
        announced = headers.get("docker-content-digest")
        if announced and announced != digest:
            raise RegistryError("manifest %s: the digest does not match its content" % reference)
        media = (headers.get("content-type") or "").split(";")[0].strip()
        return digest, media or GuessMIMEType(body), body

    def blob(self, digest):
        """A blob by its digest, checked against it. Only sha256 digests are checked."""
        if not digest.startswith("sha256:"):
            raise RegistryError("blob %s: only sha256 digests are supported" % digest)
        _, body = self.get("%s/blobs/%s" % (self.repository, digest))
        if Digest(body) != digest:
            raise RegistryError("blob %s: the digest does not match its content" % digest)
        return body


def choose_manifest(media, body, ctx=None):
    """The digest of the manifest to use for this machine if the one given is an index or a
    manifest list (part 6 picks the entry). None for a manifest itself."""
    if media == OCI1_INDEX_MEDIA_TYPE:
        chooser = OCI1IndexChooseInstance
    elif media == DOCKER_V2_LIST_MEDIA_TYPE:
        chooser = Schema2ListChooseInstance
    else:
        return None
    try:
        index = json.loads(body.decode("utf-8"))
    except ValueError:
        raise RegistryError("the image index is not valid JSON")
    return chooser(index.get("manifests") or [], ctx)


def _tar_bytes(blob):
    """The tar stream of a layer: gzip is removed, a plain tar is kept."""
    if blob[:4] == b"\x28\xb5\x2f\xfd":
        raise RegistryError("zstd layers are not supported yet")
    if blob[:2] == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=io.BytesIO(blob)).read()
    return blob


def resolve_in_root(root, parts):
    """The directory on disk for the path components parts, below root. Symlinks on the way
    are followed inside root only: an absolute link starts again at root, and ".." cannot
    leave it. The last component of a layer entry is not resolved (see apply_layer)."""
    current = []
    queue = list(parts)
    hops = 0
    while queue:
        part = queue.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if current:
                current.pop()
            continue
        here = os.path.join(root, *(current + [part]))
        if os.path.islink(here):
            hops += 1
            if hops > LAYER_MAX_SYMLINKS:
                raise RegistryError("too many symbolic links in a layer")
            target = os.readlink(here)
            if target.startswith("/"):
                current = []
            queue = target.split("/") + queue
            continue
        current.append(part)
    return os.path.join(root, *current)


def _remove(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _clear(path):
    """Removes whatever is at path, if anything."""
    if os.path.lexists(path):
        _remove(path)


def _own(path, member):
    """Gives the entry the owner from the layer. Only root can do that; others keep theirs."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.lchown(path, member.uid, member.gid)
        except OSError:
            pass


def _name_parts(name):
    return [p for p in name.split("/") if p not in ("", ".")]


def apply_layer(blob, rootfs):
    """Unpacks one layer over rootfs: files, directories, links, and whiteouts that remove what
    the layers below put there. Device nodes are skipped (they need privileges)."""
    tar = tarfile.open(fileobj=io.BytesIO(_tar_bytes(blob)), mode="r:")
    try:
        members = tar.getmembers()
        # an opaque whiteout empties its directory before the layer's own entries go in
        members.sort(key=lambda m: _name_parts(m.name)[-1:] != [".wh..wh..opq"])
        for member in members:
            parts = _name_parts(member.name)
            if not parts:
                continue
            name = parts[-1]
            try:
                parent = resolve_in_root(rootfs, parts[:-1])
                if not os.path.isdir(parent):
                    os.makedirs(parent)
                if name == ".wh..wh..opq":
                    for child in os.listdir(parent):
                        _remove(os.path.join(parent, child))
                    continue
                if name.startswith(".wh."):
                    _remove(os.path.join(parent, name[4:]))
                    continue
                target = os.path.join(parent, name)
                if member.isdir():
                    if os.path.islink(target) or (os.path.lexists(target)
                                                  and not os.path.isdir(target)):
                        _remove(target)
                    if not os.path.isdir(target):
                        os.mkdir(target)
                    os.chmod(target, member.mode & 0o7777)
                elif member.isreg():
                    _clear(target)
                    source = tar.extractfile(member)
                    with io.open(target, "wb") as fh:
                        fh.write(source.read())
                    os.chmod(target, member.mode & 0o7777)
                elif member.issym():
                    _clear(target)
                    os.symlink(member.linkname, target)
                elif member.islnk():
                    link = _name_parts(member.linkname)
                    source = os.path.join(resolve_in_root(rootfs, link[:-1]), link[-1])
                    if not os.path.lexists(source):
                        raise RegistryError("hard link to %s, which is not in the layers"
                                            % member.linkname)
                    _clear(target)
                    if os.path.islink(source):  # never read through a link: copy the link
                        os.symlink(os.readlink(source), target)
                    else:
                        shutil.copyfile(source, target)
                        os.chmod(target, os.stat(source).st_mode & 0o7777)
                elif member.isfifo():
                    _clear(target)
                    os.mkfifo(target)
                else:
                    continue
                _own(target, member)
            except OSError as exc:
                raise RegistryError("layer entry %s: %s" % (member.name, exc))
    finally:
        tar.close()


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
