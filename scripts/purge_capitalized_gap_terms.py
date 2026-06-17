"""One-off cleanup: canonicalize capitalized gap terms in the live glossary.

Companion to the forward-fix in ``ingest/gap_terms`` (gap lemmas are now
canonicalized to lowercase at scan time). That fix only prevents *new*
capital-variant duplicates; this script removes the ones already written before
the fix landed.

A *gap term* here is a ``glossary_term`` whose ``latin_lemma`` starts with an
uppercase letter and which has **no approved sense** (Krystal terms are never
touched — they all happen to be lowercase, and the approved-sense guard is
re-asserted per term before any delete).

For every canonical lowercase form ``lo``:
  * **lowercase twin exists** (a ``glossary_term`` with ``latin_lemma = lo``):
    every capitalized member is a pure duplicate → DELETE it (and its senses /
    renderings / term_usage).
  * **no lowercase twin**: keep one member, rename it in place
    (``latin_lemma = lo``); DELETE any remaining capitalized duplicates.

FKs into glossary_term/sense/rendering are ``NO ACTION`` (verified), so deletes
go child-first: term_usage → sense_rendering → glossary_sense → glossary_term.

DRY-RUN BY DEFAULT. Nothing is written unless ``--apply`` is passed. After an
apply, re-run the resolver (``python -m ingest.resolver``) so any gap term_usage
is regenerated against the canonical lemmas.

Usage:
  uv run python scripts/purge_capitalized_gap_terms.py            # dry-run report
  uv run python scripts/purge_capitalized_gap_terms.py --apply    # mutate (after review)
"""

from __future__ import annotations

import argparse
import sys

from storage.db import get_conn

# Capitalized gap terms = uppercase-initial latin_lemma with NO approved sense.
_CAP_GAP_TERMS_SQL = """
SELECT gt.term_id, gt.latin_lemma, lower(gt.latin_lemma) AS lo
FROM glossary_term gt
WHERE gt.latin_lemma ~ '^[A-Z]'
  AND NOT EXISTS (
      SELECT 1 FROM glossary_sense gs
      WHERE gs.term_id = gt.term_id AND gs.status = 'approved'
  )
ORDER BY lower(gt.latin_lemma), gt.term_id
"""


def _has_approved_sense(cur, term_id: int) -> bool:
    cur.execute(
        "SELECT 1 FROM glossary_sense WHERE term_id = %s AND status = 'approved' LIMIT 1",
        (term_id,),
    )
    return cur.fetchone() is not None


def _twin_term_id(cur, lo: str) -> int | None:
    """term_id of an existing lowercase-canonical glossary_term, if any."""
    cur.execute("SELECT term_id FROM glossary_term WHERE latin_lemma = %s", (lo,))
    row = cur.fetchone()
    return row[0] if row else None


def plan_actions(cur) -> tuple[list[dict], list[dict]]:
    """Return (deletes, renames) without mutating anything.

    deletes: [{term_id, lemma, lo, reason}]   renames: [{term_id, lemma, lo}]
    """
    cur.execute(_CAP_GAP_TERMS_SQL)
    rows = cur.fetchall()

    groups: dict[str, list[tuple[int, str]]] = {}
    for term_id, lemma, lo in rows:
        groups.setdefault(lo, []).append((term_id, lemma))

    deletes: list[dict] = []
    renames: list[dict] = []
    for lo, members in groups.items():
        members.sort()  # deterministic: lowest term_id first
        if _twin_term_id(cur, lo) is not None:
            for term_id, lemma in members:
                deletes.append({"term_id": term_id, "lemma": lemma, "lo": lo,
                                "reason": "lowercase twin exists"})
        else:
            keep_id, keep_lemma = members[0]
            renames.append({"term_id": keep_id, "lemma": keep_lemma, "lo": lo})
            for term_id, lemma in members[1:]:
                deletes.append({"term_id": term_id, "lemma": lemma, "lo": lo,
                                "reason": "duplicate of renamed sibling"})
    return deletes, renames


def _delete_term(cur, term_id: int) -> dict:
    """Delete a gap term child-first. Returns row counts. Asserts not approved."""
    if _has_approved_sense(cur, term_id):
        raise RuntimeError(
            f"refusing to delete term_id={term_id}: it has an approved sense"
        )
    cur.execute("SELECT sense_id FROM glossary_sense WHERE term_id = %s", (term_id,))
    sense_ids = [r[0] for r in cur.fetchall()]
    counts = {"term_usage": 0, "sense_rendering": 0, "glossary_sense": 0}
    if sense_ids:
        cur.execute("DELETE FROM term_usage WHERE sense_id = ANY(%s)", (sense_ids,))
        counts["term_usage"] = cur.rowcount
        cur.execute("DELETE FROM sense_rendering WHERE sense_id = ANY(%s)", (sense_ids,))
        counts["sense_rendering"] = cur.rowcount
        cur.execute("DELETE FROM glossary_sense WHERE sense_id = ANY(%s)", (sense_ids,))
        counts["glossary_sense"] = cur.rowcount
    cur.execute("DELETE FROM glossary_term WHERE term_id = %s", (term_id,))
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually mutate the DB (default: dry-run report only)")
    ap.add_argument("--limit", type=int, default=20,
                    help="how many sample rows to print per action (default 20)")
    args = ap.parse_args()

    with get_conn() as conn:
        with conn.cursor() as cur:
            deletes, renames = plan_actions(cur)

            print(f"capitalized gap terms to DELETE: {len(deletes)}")
            print(f"capitalized gap terms to RENAME in place: {len(renames)}")
            print()
            print(f"  sample deletes (first {args.limit}):")
            for d in deletes[:args.limit]:
                print(f"    {d['lemma']:<22} → drop  (canonical {d['lo']}, {d['reason']})")
            print(f"\n  sample renames (first {args.limit}):")
            for r in renames[:args.limit]:
                print(f"    {r['lemma']:<22} → {r['lo']}")
            print()

            if not args.apply:
                print("DRY-RUN — no changes written. Re-run with --apply to execute "
                      "(then re-run the resolver to regenerate gap term_usage).")
                return 0

            # ── apply ───────────────────────────────────────────────────────
            tu = sr = gs = 0
            for d in deletes:
                c = _delete_term(cur, d["term_id"])
                tu += c["term_usage"]
                sr += c["sense_rendering"]
                gs += c["glossary_sense"]
            for r in renames:
                # Guard against a twin appearing mid-run (shouldn't, single tx).
                if _twin_term_id(cur, r["lo"]) is not None:
                    raise RuntimeError(
                        f"rename collision for {r['lemma']} → {r['lo']}: twin exists"
                    )
                cur.execute(
                    "UPDATE glossary_term SET latin_lemma = %s WHERE term_id = %s",
                    (r["lo"], r["term_id"]),
                )
        conn.commit()

    print(f"APPLIED: deleted {len(deletes)} terms "
          f"({gs} senses, {sr} renderings, {tu} term_usage rows), "
          f"renamed {len(renames)} terms.")
    print("Next: re-run the resolver (python -m ingest.resolver) to regenerate "
          "gap term_usage against the canonical lemmas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
