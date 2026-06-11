"""Seed and backfill structural formula terms for data-driven formula checking.

respondeo and sed_contra are stored with is_multiword=False (slug keys), so
phrase_match cannot find them in Latin text. This script:
  1. Promotes respondeo and sed_contra to is_multiword=True.
  2. Writes la sense_renderings (surface prefix) so _match_pattern uses la_surface
     instead of the slug key for phrase matching.
  3. Backfills term_usage rows for existing segments of the matching element_type
     so check_terminology_lemma's formula branch can verify the formula immediately
     without waiting for a full re-resolution pass.

praeterea is left as singleword — CLTK finds it via token lemmatization, and
it already appears in term_usage for arg segments. Making it multiword with a ^
anchor would prevent resolution of mid-sentence "praeterea" occurrences.

Safe to re-run (idempotent). Run:
    uv run python -m ingest.seed_formula_terms
"""

from __future__ import annotations

import sys

from common.db import get_conn, source_id

# Approved formula terms that need is_multiword=True + la surface renderings.
# la_surface: shortest anchoring prefix of the Latin opener (matched at ^ by phrase_match).
# Only approved, structural openers belong here — not all DB formula terms.
STRUCTURAL_FORMULAS = {
    "sed_contra": "Sed contra",
    "respondeo": "Respondeo dicendum quod",
}

# element_type → latin_lemma slug of the required opener.
# Used for term_usage backfill: every segment of this type must have this formula
# in term_usage so check_terminology_lemma can enforce it.
_ELEMENT_TO_FORMULA: dict[str, str] = {
    "sed_contra": "sed_contra",
    "respondeo": "respondeo",
}


def _get_formula_sense(conn, latin_lemma: str) -> dict | None:
    """Return {term_id, sense_id, version} for an approved formula term, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT gt.term_id, gs.sense_id, gs.version
            FROM glossary_term gt
            JOIN glossary_sense gs USING (term_id)
            WHERE gt.latin_lemma = %s
              AND gt.category = 'formula'
              AND gs.status = 'approved'
            LIMIT 1
            """,
            (latin_lemma,),
        )
        row = cur.fetchone()
    return {"term_id": row[0], "sense_id": row[1], "version": row[2]} if row else None


def promote_to_multiword(conn, latin_lemma: str) -> bool:
    """Set is_multiword=True for a formula term. Returns True if the row was updated."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE glossary_term
            SET is_multiword = true
            WHERE latin_lemma = %s AND category = 'formula' AND is_multiword = false
            """,
            (latin_lemma,),
        )
        return cur.rowcount > 0


def write_la_surface(conn, sense_id: int, surface: str, seed_src_id: int) -> bool:
    """Upsert a la sense_rendering. Returns True if a new row was inserted."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sense_rendering (sense_id, lang, content, source_id)
            VALUES (%s, 'la', %s, %s)
            ON CONFLICT (sense_id, lang, source_id) DO NOTHING
            """,
            (sense_id, surface, seed_src_id),
        )
        return cur.rowcount > 0


def backfill_term_usage(conn, element_type: str, sense_id: int, version: int) -> int:
    """Insert term_usage rows for all segments of element_type that lack this sense.

    Uses ON CONFLICT DO NOTHING for idempotency — safe to re-run.
    Returns the count of newly inserted rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO term_usage
                (segment_id, sense_id, sense_version_used,
                 resolution_method, confidence, status)
            SELECT s.segment_id, %s, %s, 'formula_backfill', 'auto', 'guessed'
            FROM segment s
            WHERE s.element_type = %s
              AND NOT EXISTS (
                  SELECT 1 FROM term_usage tu
                  WHERE tu.segment_id = s.segment_id AND tu.sense_id = %s
              )
            """,
            (sense_id, version, element_type, sense_id),
        )
        return cur.rowcount


def run(dry_run: bool = False) -> None:
    with get_conn() as conn:
        seed_src_id = source_id(conn, "seed")

        total_promoted = 0
        total_la_written = 0
        total_backfilled = 0
        missing: list[str] = []

        for slug, la_surface in STRUCTURAL_FORMULAS.items():
            formula = _get_formula_sense(conn, slug)
            if formula is None:
                print(f"  WARNING: no approved formula sense for '{slug}' — skipping", file=sys.stderr)
                missing.append(slug)
                continue

            sense_id = formula["sense_id"]
            version = formula["version"]
            element_type = _ELEMENT_TO_FORMULA[slug]

            if not dry_run:
                promoted = promote_to_multiword(conn, slug)
                la_written = write_la_surface(conn, sense_id, la_surface, seed_src_id)
                backfilled = backfill_term_usage(conn, element_type, sense_id, version)
            else:
                promoted = la_written = False
                backfilled = 0

            total_promoted += int(promoted)
            total_la_written += int(la_written)
            total_backfilled += backfilled

            print(
                f"  {slug}: is_multiword={'set' if promoted else 'already set'}, "
                f"la_surface={'written' if la_written else 'already present'}, "
                f"term_usage backfilled={backfilled}"
            )

        if not dry_run:
            conn.commit()

        print()
        print(f"Promoted to multiword: {total_promoted}")
        print(f"la renderings written: {total_la_written}")
        print(f"term_usage rows backfilled: {total_backfilled}")
        if missing:
            print(f"Missing approved senses (manual action needed): {missing}", file=sys.stderr)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Seed structural formula terms.")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no DB writes.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
