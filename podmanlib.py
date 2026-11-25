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

import functools
import re
import subprocess
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
