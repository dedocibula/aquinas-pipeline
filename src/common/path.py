"""Locator/path conversion helpers shared between the server and notify modules."""

from __future__ import annotations

import re


def url_to_ltree(st_locator: str) -> str:
    """Convert an aquinas.cc-style locator to an ltree path.

    Examples:
        ST.I.Q3.A1    → I.q3.a1
        ST.II-I.Q1.A1 → II-I.q1.a1
        I.Q3.A1       → I.q3.a1
    """
    s = st_locator
    if s.upper().startswith("ST."):
        s = s[3:]
    # Only lowercase Q→q and A→a; pars labels (I, II-I) are uppercase in DB.
    s = re.sub(r"Q(\d+)", lambda m: f"q{m.group(1)}", s)
    s = re.sub(r"A(\d+)", lambda m: f"a{m.group(1)}", s)
    return s


def ltree_depth(path: str) -> int:
    """Count the number of labels in an ltree path string (dot-separated)."""
    return len(path.split("."))


def _relabel_parts(ltree_path: str) -> list[str]:
    """Turn an ltree path's dot-separated labels back into ST-style labels.

    E.g. 'I.q3.a1' → ['I', 'Q3', 'A1'].
    """
    parts = ltree_path.split(".")
    labels = []
    for p in parts:
        if p.startswith("q") and p[1:].isdigit():
            labels.append("Q" + p[1:])
        elif p.startswith("a") and p[1:].isdigit():
            labels.append("A" + p[1:])
        else:
            labels.append(p.upper())
    return labels


def locator_to_title(ltree_path: str) -> str:
    """Turn an ltree path like 'I.q3.a1' into 'ST I, Q3, A1'."""
    return "ST " + ", ".join(_relabel_parts(ltree_path))


def ltree_to_url_locator(ltree_path: str) -> str:
    """Convert an ltree path (e.g. 'I.q3.a1') back to 'ST.I.Q3.A1' for URL construction."""
    return "ST." + ".".join(_relabel_parts(ltree_path))


def article_path_from_locator(locator: str) -> str:
    """Truncate a segment locator (ltree path) to its containing article path.

    E.g. ``I.q3.a1.arg1`` -> ``I.q3.a1``.
    """
    return ".".join(locator.split(".")[:3])


def segment_link(base_url: str, locator: str, segment_id: int) -> str:
    """Deep link to a segment's row within its article page."""
    coord = ltree_to_url_locator(article_path_from_locator(locator))
    return f"{base_url}/~{coord}#seg-{segment_id}"
