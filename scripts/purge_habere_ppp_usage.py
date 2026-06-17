"""One-off cleanup: delete bogus *habitus* term_usage rows from perfect-passive habere.

CLTK mislemmatizes the perfect-passive participle 'habitum' (in 'habitum est' /
'habita sunt' — the stock scholastic "as has been said/held") to the noun
*habitus*, an approved Krystal term. The resolver therefore wrote a habitus
term_usage row for many segments that never mention the concept, and the
pipeline then demanded 'habitus' in their Slovak draft.

The root cause is fixed in ``ingest/resolver`` (``_suppressed_habitus_tokens``),
so re-resolution no longer writes these rows. This script removes the rows
already written before that fix. It reuses the *same* detector as the resolver,
so "what gets deleted" and "what the resolver now skips" are one source of truth:
a habitus term_usage row is bogus iff ``_suppressed_habitus_tokens(segment.latin)``
is non-empty (i.e. the construction is the segment's only habitus evidence).

Only ``term_usage`` rows are touched — the habitus glossary_term/senses are
legitimate and are never modified.

DRY-RUN BY DEFAULT. Nothing is written unless ``--apply`` is passed.

Usage:
  uv run python scripts/purge_habere_ppp_usage.py            # dry-run report
  uv run python scripts/purge_habere_ppp_usage.py --apply    # delete (after review)
"""

from __future__ import annotations

import argparse
import sys

from ingest.resolver import _suppressed_habitus_tokens
from storage.db import get_conn

# All term_usage rows whose sense belongs to the *habitus* term, with the
# segment's Latin text so the resolver detector can classify each one.
_HABITUS_USAGE_SQL = """
SELECT tu.usage_id, tu.segment_id, s.locator_path::text, st.content
FROM term_usage tu
JOIN glossary_sense gs ON gs.sense_id = tu.sense_id
JOIN glossary_term gt ON gt.term_id = gs.term_id
LEFT JOIN segment s ON s.segment_id = tu.segment_id
LEFT JOIN segment_text st ON st.segment_id = tu.segment_id AND st.lang = 'la'
WHERE gt.latin_lemma = 'habitus'
ORDER BY s.locator_path
"""


def find_bogus(cur) -> list[dict]:
    """Return [{usage_id, segment_id, locator, tokens}] for PPP-only habitus rows."""
    cur.execute(_HABITUS_USAGE_SQL)
    bogus: list[dict] = []
    for usage_id, segment_id, locator, latin in cur.fetchall():
        suppressed = _suppressed_habitus_tokens(latin or "")
        if suppressed:
            bogus.append({
                "usage_id": usage_id,
                "segment_id": segment_id,
                "locator": locator,
                "tokens": sorted(suppressed),
            })
    return bogus


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete the rows (default: dry-run report only)")
    ap.add_argument("--limit", type=int, default=30,
                    help="how many sample rows to print (default 30)")
    args = ap.parse_args()

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Total habitus rows, for context.
            cur.execute("""
                SELECT count(*)
                FROM term_usage tu
                JOIN glossary_sense gs ON gs.sense_id = tu.sense_id
                JOIN glossary_term gt ON gt.term_id = gs.term_id
                WHERE gt.latin_lemma = 'habitus'
            """)
            total = cur.fetchone()[0]

            bogus = find_bogus(cur)
            print(f"habitus term_usage rows total: {total}")
            print(f"bogus (perfect-passive habere only) to DELETE: {len(bogus)}")
            print(f"genuine kept: {total - len(bogus)}")
            print()
            print(f"  sample (first {args.limit}):")
            for b in bogus[:args.limit]:
                print(f"    {b['locator']:<28} usage_id={b['usage_id']:<7} "
                      f"({'+'.join(b['tokens'])})")
            print()

            if not args.apply:
                print("DRY-RUN — no changes written. Re-run with --apply to delete.")
                return 0

            usage_ids = [b["usage_id"] for b in bogus]
            if usage_ids:
                cur.execute("DELETE FROM term_usage WHERE usage_id = ANY(%s)", (usage_ids,))
                deleted = cur.rowcount
            else:
                deleted = 0
        conn.commit()

    print(f"APPLIED: deleted {deleted} bogus habitus term_usage rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
