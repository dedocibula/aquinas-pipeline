"""One-time migration: normalise glossary_term.latin_lemma to lowercase for
formula/term categories, approve respondeo, delete bogus praeterea mining senses.

Safe to re-run (all ops are idempotent or guarded by checks).
"""
from __future__ import annotations

import psycopg2

CONN = "postgresql://aquinas:aquinas@localhost:5432/aquinas"

# ── Bogus praeterea senses injected by sense_mining ──────────────────────────
BOGUS_SENSE_IDS = [14642, 14643, 14644, 14645, 14646, 14647]


def run() -> None:
    conn = psycopg2.connect(CONN)
    try:
        with conn:
            _delete_bogus_senses(conn)
            _approve_respondeo(conn)
            _delete_cap_praeterea(conn)
            _merge_cap_with_twins(conn)
            _rename_cap_no_twins(conn)
        print("Migration complete.")
    finally:
        conn.close()


def _delete_bogus_senses(conn) -> None:
    """Delete the 6 praeterea mining senses that are semantically wrong."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM sense_rendering WHERE sense_id = ANY(%s)", (BOGUS_SENSE_IDS,)
        )
        cur.execute(
            "DELETE FROM glossary_sense WHERE sense_id = ANY(%s)", (BOGUS_SENSE_IDS,)
        )
    print(f"  Deleted {len(BOGUS_SENSE_IDS)} bogus praeterea senses.")


def _approve_respondeo(conn) -> None:
    """Approve respondeo (lowercase) with capitalised SK rendering 'Odpovedám'.

    - Updates sense 16247 SK content to 'Odpovedám' (sentence-initial capitalisation).
    - Sets status = 'approved'.
    - Deletes the duplicate sense on the capitalised Respondeo term (sense 14819).
    """
    with conn.cursor() as cur:
        # Fix capitalisation of the SK rendering on the lowercase sense
        cur.execute(
            "UPDATE sense_rendering SET content = 'Odpovedám' "
            "WHERE sense_id = 16247 AND lang = 'sk'",
        )
        # Approve
        cur.execute(
            "UPDATE glossary_sense SET status = 'approved' WHERE sense_id = 16247",
        )
        # Delete the duplicate on the capitalized Respondeo term
        cur.execute("DELETE FROM term_usage WHERE sense_id = 14819")
        cur.execute("DELETE FROM sense_rendering WHERE sense_id = 14819")
        cur.execute("DELETE FROM glossary_sense WHERE sense_id = 14819")
        # Drop the capitalized term if now empty
        cur.execute(
            "DELETE FROM glossary_term WHERE latin_lemma = 'Respondeo' "
            "AND NOT EXISTS (SELECT 1 FROM glossary_sense WHERE term_id = "
            "(SELECT term_id FROM glossary_term WHERE latin_lemma = 'Respondeo'))"
        )
        # Also drop term_usage rows that pointed at the now-deleted sense 14819
        cur.execute("DELETE FROM term_usage WHERE sense_id = 14819")
    print("  Approved respondeo → Odpovedám; deleted Respondeo duplicate.")


def _delete_cap_praeterea(conn) -> None:
    """Delete the capitalised Praeterea term (sense 10542, sk='Mimo to').
    The lowercase praeterea (sense 9422, sk='Ďalej') is already approved.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM term_usage WHERE sense_id = 10542")
        cur.execute("DELETE FROM sense_rendering WHERE sense_id = 10542")
        cur.execute("DELETE FROM glossary_sense WHERE sense_id = 10542")
        cur.execute(
            "DELETE FROM glossary_term WHERE latin_lemma = 'Praeterea' "
            "AND NOT EXISTS (SELECT 1 FROM glossary_sense WHERE term_id = "
            "(SELECT term_id FROM glossary_term WHERE latin_lemma = 'Praeterea'))"
        )
    print("  Deleted capitalised Praeterea term.")


def _merge_cap_with_twins(conn) -> None:
    """Delete capitalised formula/term entries that have a lowercase twin.

    The lowercase twin is canonical (matches CLTK output). Drop cap term's
    term_usage, senses, renderings, then the term itself.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cap.term_id, cap.latin_lemma
            FROM   glossary_term cap
            JOIN   glossary_term low
                ON low.latin_lemma = LOWER(cap.latin_lemma)
               AND low.term_id     != cap.term_id
            WHERE  cap.latin_lemma ~ '^[A-Z]'
              AND  cap.category IN ('formula', 'term')
            """
        )
        pairs = cur.fetchall()

    deleted = 0
    with conn.cursor() as cur:
        for cap_term_id, cap_lemma in pairs:
            # Drop term_usage rows on this cap term's senses
            cur.execute(
                "DELETE FROM term_usage WHERE sense_id IN "
                "(SELECT sense_id FROM glossary_sense WHERE term_id = %s)",
                (cap_term_id,),
            )
            # Drop renderings
            cur.execute(
                "DELETE FROM sense_rendering WHERE sense_id IN "
                "(SELECT sense_id FROM glossary_sense WHERE term_id = %s)",
                (cap_term_id,),
            )
            # Drop senses
            cur.execute("DELETE FROM glossary_sense WHERE term_id = %s", (cap_term_id,))
            # Drop term
            cur.execute("DELETE FROM glossary_term WHERE term_id = %s", (cap_term_id,))
            deleted += 1

    print(f"  Deleted {deleted} capitalised terms that had lowercase twins.")


def _rename_cap_no_twins(conn) -> None:
    """Rename capitalised formula/term entries (no lowercase twin) to lowercase."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cap.term_id, cap.latin_lemma
            FROM   glossary_term cap
            WHERE  cap.latin_lemma ~ '^[A-Z]'
              AND  cap.category IN ('formula', 'term')
              AND  NOT EXISTS (
                  SELECT 1 FROM glossary_term low
                  WHERE  low.latin_lemma = LOWER(cap.latin_lemma)
                    AND  low.term_id != cap.term_id
              )
            """
        )
        orphans = cur.fetchall()

    renamed = skipped = 0
    with conn.cursor() as cur:
        for term_id, lemma in orphans:
            new_lemma = lemma.lower()
            # Guard: a lowercase twin might have been created by a previous iteration
            cur.execute(
                "SELECT 1 FROM glossary_term WHERE latin_lemma = %s AND term_id != %s",
                (new_lemma, term_id),
            )
            if cur.fetchone():
                # Twin was created mid-loop — delete this entry instead
                cur.execute(
                    "DELETE FROM term_usage WHERE sense_id IN "
                    "(SELECT sense_id FROM glossary_sense WHERE term_id = %s)",
                    (term_id,),
                )
                cur.execute(
                    "DELETE FROM sense_rendering WHERE sense_id IN "
                    "(SELECT sense_id FROM glossary_sense WHERE term_id = %s)",
                    (term_id,),
                )
                cur.execute("DELETE FROM glossary_sense WHERE term_id = %s", (term_id,))
                cur.execute("DELETE FROM glossary_term WHERE term_id = %s", (term_id,))
                skipped += 1
            else:
                cur.execute(
                    "UPDATE glossary_term SET latin_lemma = %s WHERE term_id = %s",
                    (new_lemma, term_id),
                )
                renamed += 1

    print(f"  Renamed {renamed} capitalised terms to lowercase; deleted {skipped} late-conflict duplicates.")


if __name__ == "__main__":
    run()
