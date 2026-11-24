"""Implementation surface for tenant-isolated cache keys.

The public package surface is cachekey.api, which only re-exports these
eight names.  Every key is built as::

    escape_part(tenant) SEPARATOR decimal_count [SEPARATOR escape_part(p)]*

The escaped tenant plus its trailing separator is the ownership boundary,
and every logical part is escaped independently so it cannot inject a
segment or a wildcard.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from urllib.parse import quote, unquote

SEPARATOR = ":"
CONTROL_FLOOR = 0x20
CONTROL_DELETE = 0x7F
MAX_PARTS = 32


def _has_control(value: str) -> bool:
    """Return True when value contains a C0 or DEL control character."""
    return any(ord(ch) < CONTROL_FLOOR or ord(ch) == CONTROL_DELETE for ch in value)


def tenant_prefix(tenant: str) -> str:
    """Return the canonical cache-key prefix for one tenant.

    Contract
    --------
    tenant must be a non-empty str.  Reject None, non-str values, the empty
    string, and any tenant containing a control character with ValueError or
    TypeError as appropriate.

    The returned value is escape_part(tenant) followed by SEPARATOR, so it
    always ends with the separator.  The tenant name itself is escaped before
    the boundary is appended; concatenating a raw tenant with a separator is
    not sufficient.

    The trailing separator is the ownership boundary.  A tenant named acme
    must not own keys belonging to acme2, even though acme2 starts with acme.
    """
    # Defect 01/02: the tenant is escaped, and the prefix ends with exactly
    # one SEPARATOR so that "acme" can never be a textual prefix of "acme2".
    if not isinstance(tenant, str):
        raise TypeError("tenant must be a str")
    if not tenant:
        raise ValueError("tenant must be non-empty")
    if _has_control(tenant):
        raise ValueError("tenant must not contain control characters")
    return escape_part(tenant) + SEPARATOR


def build_key(tenant: str, parts: list[str]) -> str:
    """Build a canonical cache key from a tenant and logical segments.

    Contract
    --------
    Format: escaped tenant, SEPARATOR, decimal segment count, then one
    SEPARATOR plus one escaped part for each element of parts.  For tenant
    acme and parts ["order:1"] the shape is acme + ":" + 1 + ":" +
    escaped(order:1).

    parts must be a list or tuple of str with at most MAX_PARTS elements.
    Each part is escaped independently with escape_part, so a separator
    inside a logical part can never create an extra segment.  An empty part
    is legal and still counts as one segment.  Duplicate parts are legal.

    The result must be accepted by parse_key and must round-trip to the same
    tenant and parts.  This function keeps no state between calls: equal
    arguments always produce equal keys, and requests for different tenants
    always start with different tenant prefixes.
    """
    # Defect 01: every key begins with this tenant's own escaped prefix.
    prefix = tenant_prefix(tenant)
    if not isinstance(parts, (list, tuple)):
        raise TypeError("parts must be a list or tuple of str")
    if len(parts) > MAX_PARTS:
        raise ValueError(f"parts must have at most {MAX_PARTS} elements")
    # Defect 03: each part is escaped independently, so a separator inside a
    # logical part cannot create an extra segment.
    escaped = []
    for part in parts:
        escaped.append(escape_part(part))
    segments = [str(len(escaped))]
    segments.extend(escaped)
    return prefix + SEPARATOR.join(segments)


def parse_key(key: str) -> dict:
    """Parse a canonical cache key and validate every structural invariant.

    Contract
    --------
    Split key on SEPARATOR.  Segment zero is the escaped tenant, segment one
    is the decimal segment count, and the remaining segments are escaped
    parts.  Validate all of the following before returning anything:

    - key is a str and uses exactly SEPARATOR, not a lookalike character;
    - the count is ASCII decimal, has no sign, and has no leading zero unless
      the count itself is zero;
    - 2 + count equals the number of segments, so a forged extra segment or a
      missing segment raises ValueError;
    - every raw segment is free of control characters;
    - the decoded tenant is non-empty, and the decoded part count equals the
      declared count.

    Return {"tenant": str, "parts": list[str]} with unquoted values.  Raise
    ValueError for any malformed key.  A key is either fully accepted or
    rejected; never return a partially parsed result.  Escaped separators
    and escaped control characters inside parts must survive this round trip
    as ordinary characters of the returned part.
    """
    if not isinstance(key, str):
        raise TypeError("key must be a str")
    segments = key.split(SEPARATOR)

    # Defect 05: the declared count must match the real number of segments,
    # so a forged extra or missing segment is rejected.
    if len(segments) < 2:
        raise ValueError("key must contain a tenant and a segment count")
    # Defect 04: raw control characters may never appear in any segment;
    # escaping is the only way they are allowed to travel.
    if _has_control(key):
        raise ValueError("key must not contain control characters")

    raw_tenant, raw_count = segments[0], segments[1]

    # Defect 05: ASCII decimal only, no sign, no leading zero unless zero.
    if not re.fullmatch(r"0|[1-9][0-9]*", raw_count):
        raise ValueError("segment count must be a canonical decimal integer")
    count = int(raw_count)
    if count > MAX_PARTS:
        raise ValueError(f"segment count must not exceed {MAX_PARTS}")

    # Defect 05: 2 + count must equal the number of segments exactly.
    if 2 + count != len(segments):
        raise ValueError("segment count does not match the number of segments")

    # Defect 01/02: the first segment is the escaped tenant and must be
    # non-empty after decoding.
    try:
        tenant = unquote(raw_tenant, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("tenant segment is not valid percent-encoding") from exc
    if not tenant:
        raise ValueError("tenant must be non-empty")

    parts = []
    for raw_part in segments[2:]:
        try:
            parts.append(unquote(raw_part, errors="strict"))
        except UnicodeDecodeError as exc:
            raise ValueError("part segment is not valid percent-encoding") from exc

    # Defect 05: the decoded part count must equal the declared count.
    if len(parts) != count:
        raise ValueError("decoded part count does not match the declared count")

    return {"tenant": tenant, "parts": parts}


def escape_part(part: str) -> str:
    """Escape one logical segment so it is inert inside a cache key.

    Contract
    --------
    part must be a str; raise TypeError otherwise.  Encode part as UTF-8 with
    percent-encoding using an empty safe set.  The returned string must not
    contain SEPARATOR or any control character in the ranges U+0000 through
    U+001F and U+007F.  Spaces, percent signs, and the separator itself are
    all escaped; special-casing only spaces is a defect.  The encoding must
    be reversible and injective so that parse_key can recover the exact
    original part.
    """
    if not isinstance(part, str):
        raise TypeError("part must be a str")
    # Defect 03/04: quote with an empty safe set; this escapes SEPARATOR,
    # spaces, percent signs and every control character (as %00-%1F / %7F).
    return quote(part, safe="", encoding="utf-8", errors="strict")


def scope_ok(key: str, tenant: str) -> bool:
    """Return True only when key is a valid key owned by tenant.

    Contract
    --------
    Ownership needs a strict separator boundary: comparing only the escaped
    tenant text is unsafe because tenant acme would then accept keys of
    acme2.  The key must also parse successfully, and the parsed tenant must
    equal tenant exactly.  Malformed keys, keys for another tenant, and keys
    whose prefix merely starts with the tenant name all return False.

    This function treats key as untrusted input and returns False instead of
    raising for malformed keys.  It must never return True for a key that
    parse_key rejects.
    """
    if not isinstance(key, str) or not isinstance(tenant, str):
        return False
    # Defect 06: require the full escaped tenant plus the separator boundary,
    # never a bare textual prefix such as "acme" matching "acme2".
    try:
        prefix = tenant_prefix(tenant)
    except (TypeError, ValueError):
        return False
    if not key.startswith(prefix):
        return False
    try:
        parsed = parse_key(key)
    except (TypeError, ValueError):
        return False
    return parsed["tenant"] == tenant


def invalidate(store: dict, tenant: str, pattern: str) -> int:
    """Delete tenant-owned keys matching a logical wildcard pattern.

    Contract
    --------
    store is a mutable mapping from cache key to arbitrary value.  pattern is
    matched against the decoded suffix of a key: the unquoted parts joined
    with SEPARATOR.  Only the star wildcard is supported; it matches any run
    of characters including the empty run.  Question marks, brackets, braces,
    and backslashes are literal characters, and there is no implicit leading
    or trailing wildcard.

    Ownership is decided with scope_ok before matching.  Therefore keys for
    other tenants, malformed keys, and keys of lookalike tenants such as
    acme2 are never deleted when tenant is acme.  A pattern must never be
    prefixed directly onto the tenant and used as a raw global wildcard.

    Collect every victim key first, then delete all of them, and return the
    exact number deleted.  Do not count non-matching or foreign keys.  If no
    key matches, return zero and leave store unchanged.
    """
    if not isinstance(store, MutableMapping):
        raise TypeError("store must be a mutable mapping")
    if not isinstance(pattern, str):
        raise TypeError("pattern must be a str")
    # Defect 07: only "*" is a wildcard; everything else (?, [], {}, \) is
    # literal text.  Build the matcher on the decoded logical suffix, never
    # by splicing the pattern into a raw global key glob.
    regex = re.compile(
        "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern),
        re.DOTALL,
    )

    victims = []
    for key in list(store):
        # Defect 07/08: ownership is decided by scope_ok before any matching,
        # so foreign and lookalike-tenant keys can never be selected.
        if not scope_ok(key, tenant):
            continue
        try:
            parsed = parse_key(key)
        except (TypeError, ValueError):
            continue
        suffix = SEPARATOR.join(parsed["parts"])
        if regex.fullmatch(suffix):
            victims.append(key)

    for key in victims:
        del store[key]
    return len(victims)


def validate_policy(policy: dict) -> list[str]:
    """Return issue codes for a cache-key policy; empty list means valid.

    Contract
    --------
    A valid policy is a mapping with these exact boolean fields:

    - require_tenant_prefix must be True; otherwise report
      "missing_tenant_prefix";
    - escape_parts must be True; otherwise report
      "separator_allowed_in_part";
    - allow_global_invalidation must be False; otherwise report
      "global_invalidation_allowed".

    A non-mapping policy, a missing field, or a field whose value is not a
    bool reports "invalid_policy".  Collect every applicable issue rather
    than stopping at the first one.  Extra fields are ignored.  Return a
    deterministic list without duplicates, and return [] only when all three
    security requirements hold.
    """
    if not isinstance(policy, Mapping):
        return ["invalid_policy"]

    required = (
        ("require_tenant_prefix", "missing_tenant_prefix"),
        ("escape_parts", "separator_allowed_in_part"),
        # Defect 09: a policy that permits global invalidation is rejected.
        ("allow_global_invalidation", "global_invalidation_allowed"),
    )

    issues: list[str] = []
    for field, issue in required:
        if field not in policy:
            if "invalid_policy" not in issues:
                issues.append("invalid_policy")
            continue
        value = policy[field]
        if not isinstance(value, bool):
            if "invalid_policy" not in issues:
                issues.append("invalid_policy")
            continue
        if field == "allow_global_invalidation":
            # Safe value is False.
            if value is True and issue not in issues:
                issues.append(issue)
        else:
            # Safe value is True.
            if value is False and issue not in issues:
                issues.append(issue)
    return issues


def key_batch(requests: list[dict], policy: dict) -> dict:
    """Build keys for many independent requests under one policy.

    Contract
    --------
    First validate policy with validate_policy.  If issues are returned,
    raise ValueError carrying the issue list and build no key at all.

    Each request is a mapping with request_id (non-empty str), tenant
    (non-empty str), and parts (list of str).  request_id values must be
    unique.  Every key is built with build_key from that request's own tenant
    and its own parts, so a later tenant must never reuse an earlier tenant
    prefix and a later request must never overwrite an earlier key.

    Return {request_id: key}.  Validate every request before building any
    key, and raise ValueError for a duplicate request_id, a missing field, or
    a malformed field.  The order of requests must not change the key built
    for any individual request: the batch result for a request is identical
    to calling build_key(tenant, parts) directly.
    """
    issues = validate_policy(policy)
    if issues:
        raise ValueError(f"invalid cache-key policy: {issues}")

    if not isinstance(requests, (list, tuple)):
        raise TypeError("requests must be a list or tuple")

    # Validate every request before building any key, so a bad request can
    # never leave a half-built batch behind.
    seen: set[str] = set()
    validated: list[tuple[str, str, list[str]]] = []
    for request in requests:
        if not isinstance(request, Mapping):
            raise ValueError("each request must be a mapping")
        for field in ("request_id", "tenant", "parts"):
            if field not in request:
                raise ValueError(f"request is missing field {field!r}")
        request_id = request["request_id"]
        tenant = request["tenant"]
        parts = request["parts"]
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty str")
        if request_id in seen:
            raise ValueError(f"duplicate request_id {request_id!r}")
        seen.add(request_id)
        if not isinstance(tenant, str) or not tenant:
            raise ValueError("tenant must be a non-empty str")
        if not isinstance(parts, (list, tuple)) or not all(
            isinstance(part, str) for part in parts
        ):
            raise ValueError("parts must be a list of str")
        if len(parts) > MAX_PARTS:
            raise ValueError(f"parts must have at most {MAX_PARTS} elements")
        validated.append((request_id, tenant, list(parts)))

    # Defect 10: build each key from its own tenant and its own parts; never
    # reuse a prefix across requests.
    built: dict[str, str] = {}
    for request_id, tenant, parts in validated:
        built[request_id] = build_key(tenant, parts)
    return built