"""
Reset stale model-proposed gap-term state — maintenance utility.

Earlier partial resolver runs left stale gap-term state in the DB: glossary_sense
rows with status='proposed', their sense_rendering(sk) rows, term_usage rows
referencing them, and the glossary_term rows that own them. Before a clean
re-run of the resolver this stale state must be removed.

Krystal-seeded terms MUST be preserved. They have at least one glossary_sense row
with status='approved'. Gap terms have ONLY status='proposed' senses and no
approved sense. The brand-new glossary_term.category column is NULL on pre-existing
stale gap terms, so it CANNOT be used to identify them. Instead, a gap term is
defined purely by its senses:

    a glossary_term every one of whose senses is status='proposed'
    (≥1 proposed sense, 0 approved senses)

expressed as:

    SELECT term_id FROM glossary_sense GROUP BY term_id HAVING bool_and(status = 'proposed')

Deletes run in FK-safe order: term_usage → sense_rendering → glossary_sense →
glossary_term, each scoped to that gap-term set.

CLI:
  python -m ingest.reset_gap_proposals             # DRY RUN: print counts, delete nothing
  python -m ingest.reset_gap_proposals --execute   # print counts, then delete

Run:
  uv run python -m ingest.reset_gap_proposals
"""

from __future__ import annotations

import argparse
import sys

from common.db import get_conn

# The set of "gap term" term_ids: every sense for the term is 'proposed' (no
# approved sense). bool_and over the status predicate is true only when ALL of a
# term's senses are proposed, so Krystal terms (which have ≥1 approved sense) are
# excluded. A term with zero senses cannot appear here (GROUP BY needs a row).
_GAP_TERM_IDS = (
    "SELECT term_id FROM glossary_sense "
    "GROUP BY term_id HAVING bool_and(status = 'proposed')"
)

# Senses belonging to gap terms.
_GAP_SENSE_IDS = (
    f"SELECT sense_id FROM glossary_sense WHERE term_id IN ({_GAP_TERM_IDS})"
)


def find_gap_proposal_state(conn) -> dict:
    """Count (do NOT delete) the rows a reset would remove.

    Returns a dict with counts of:
      term_usage      — usages pointing at a gap sense
      sense_rendering — renderings of a gap sense
      glossary_sense  — proposed senses of gap terms
      glossary_term   — gap terms that would become orphaned

    All counts are read-only SELECTs; this function issues no DELETE.
    """
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM term_usage WHERE sense_id IN ({_GAP_SENSE_IDS})"
        )
        counts["term_usage"] = cur.fetchone()[0]

        cur.execute(
            f"SELECT count(*) FROM sense_rendering WHERE sense_id IN ({_GAP_SENSE_IDS})"
        )
        counts["sense_rendering"] = cur.fetchone()[0]

        cur.execute(
            f"SELECT count(*) FROM glossary_sense WHERE term_id IN ({_GAP_TERM_IDS})"
        )
        counts["glossary_sense"] = cur.fetchone()[0]

        cur.execute(
            f"SELECT count(*) FROM glossary_term WHERE term_id IN ({_GAP_TERM_IDS})"
        )
        counts["glossary_term"] = cur.fetchone()[0]

    return counts


def reset_gap_proposals(conn) -> dict:
    """Delete all gap-term state in FK-safe order. Returns counts of deleted rows.

    Strictly scoped to gap term_ids (terms whose senses are all 'proposed').
    Krystal / approved data is never touched: a term with any approved sense fails
    the bool_and predicate and is excluded from every subquery below.

    Delete order (FK-safe):
        term_usage → sense_rendering → glossary_sense → glossary_term
    """
    deleted: dict[str, int] = {}
    with conn.cursor() as cur:
        # Materialize the gap term_id set FIRST, before any sense is deleted.
        # The set is defined by glossary_sense status (bool_and over 'proposed').
        # Deleting senses in step 3 below would otherwise empty this set, so we
        # capture the concrete ids up front and key every delete on them. This
        # also guarantees we never touch a term that legitimately has 0 senses.
        cur.execute(_GAP_TERM_IDS)
        gap_term_ids = [r[0] for r in cur.fetchall()]

        if not gap_term_ids:
            deleted.update(
                term_usage=0, sense_rendering=0, glossary_sense=0, glossary_term=0
            )
            conn.commit()
            return deleted

        gap_sense_ids_sql = "SELECT sense_id FROM glossary_sense WHERE term_id = ANY(%s)"

        # 1. term_usage references glossary_sense(sense_id).
        cur.execute(
            f"DELETE FROM term_usage WHERE sense_id IN ({gap_sense_ids_sql})",
            (gap_term_ids,),
        )
        deleted["term_usage"] = cur.rowcount

        # 2. sense_rendering references glossary_sense(sense_id).
        cur.execute(
            f"DELETE FROM sense_rendering WHERE sense_id IN ({gap_sense_ids_sql})",
            (gap_term_ids,),
        )
        deleted["sense_rendering"] = cur.rowcount

        # 3. glossary_sense references glossary_term(term_id).
        cur.execute(
            "DELETE FROM glossary_sense WHERE term_id = ANY(%s)",
            (gap_term_ids,),
        )
        deleted["glossary_sense"] = cur.rowcount

        # 4. glossary_term, now free of senses. Scoped strictly to the captured
        #    gap term_ids — no NOT EXISTS heuristic, so unrelated terms are safe.
        cur.execute(
            "DELETE FROM glossary_term WHERE term_id = ANY(%s)",
            (gap_term_ids,),
        )
        deleted["glossary_term"] = cur.rowcount

    conn.commit()
    return deleted


def _print_counts(label: str, counts: dict) -> None:
    print(label)
    for table in ("term_usage", "sense_rendering", "glossary_sense", "glossary_term"):
        print(f"  {table:<16} {counts.get(table, 0)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reset stale model-proposed gap-term state (preserves Krystal/approved)."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete. Without this flag the script is a DRY RUN.",
    )
    args = parser.parse_args(argv)

    with get_conn() as conn:
        counts = find_gap_proposal_state(conn)
        _print_counts("Gap-term state that WOULD be deleted:", counts)

        if not args.execute:
            print("\nDRY RUN — nothing deleted. Re-run with --execute to delete.")
            return 0

        deleted = reset_gap_proposals(conn)
        _print_counts("\nDeleted:", deleted)
        print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
