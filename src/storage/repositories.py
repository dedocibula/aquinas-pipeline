"""Repository layer: all SQL lives here, behind cohesive per-aggregate classes.

Each repository wraps a live psycopg2 connection and exposes typed methods that
return the dataclasses in ``storage.models`` (Term/Sense/Segment/Constraint) or
plain scalars. The SQL was moved verbatim from the former scattered helpers
(``glossary_repo``, ``corpus_db``, inline ``translate.loop`` / ``translate.run``
helpers, ``ingest.resolution._write_term_usage``); only the row→model mapping is
new. Those former module-level functions remain as thin wrappers that delegate
here while the dict→model boundary migrates.
"""

from __future__ import annotations

import json

import psycopg2.extras

from storage.models import Constraint, Segment, Sense, Term

# Element types the resolver runs on (skip non-body segments). Titles resolve to
# zero terms (no Latin) but are included so the resolver leaves an auditable
# empty result. Duplicated from resolution.py to avoid a circular import.
_BODY_TYPES = {"arg", "sed_contra", "respondeo", "reply", "article_title", "question_title"}


class GlossaryRepository:
    """All glossary_term / glossary_sense / sense_rendering access."""

    def __init__(self, conn):
        self.conn = conn

    # ── Reads ──────────────────────────────────────────────────────────────────

    def load_glossary(self) -> tuple[list[Term], list[Term]]:
        """Return (multiword_terms, singleword_terms), sorted for determinism.

        Only 'approved' senses are loaded into the Krystal lookup. 'proposed'
        senses belong to gap terms and must keep being resolved via gap methods
        on every run, not promoted just because a previous run created them.
        All language renderings use LATERAL ORDER BY authority_rank so the
        highest-authority source wins (lower rank = higher authority). Each
        LATERAL returns at most one row per lang, so no GROUP BY is needed.
        """
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT gt.term_id, gt.latin_lemma, gt.is_multiword, gt.category, gt.la_surface,
                       gs.sense_id, gs.context_label, gs.version,
                       cs_sub.lemma   AS cs_lemma,
                       cs_sub.content AS cs_content,
                       en_sub.content AS en_cue,
                       sk_sub.content AS sk_content
                FROM glossary_term gt
                JOIN glossary_sense gs USING (term_id)
                LEFT JOIN LATERAL (
                    SELECT sr.lemma, sr.content
                    FROM sense_rendering sr
                    JOIN source src ON src.source_id = sr.source_id
                    WHERE sr.sense_id = gs.sense_id AND sr.lang = 'cs'
                    ORDER BY src.authority_rank
                    LIMIT 1
                ) cs_sub ON true
                LEFT JOIN LATERAL (
                    SELECT sr.content
                    FROM sense_rendering sr
                    JOIN source src ON src.source_id = sr.source_id
                    WHERE sr.sense_id = gs.sense_id AND sr.lang = 'en'
                    ORDER BY src.authority_rank
                    LIMIT 1
                ) en_sub ON true
                LEFT JOIN LATERAL (
                    SELECT sr.content
                    FROM sense_rendering sr
                    JOIN source src ON src.source_id = sr.source_id
                    WHERE sr.sense_id = gs.sense_id AND sr.lang = 'sk'
                    ORDER BY src.authority_rank
                    LIMIT 1
                ) sk_sub ON true
                WHERE gs.status = 'approved'
                ORDER BY gt.latin_lemma, gs.sense_id
            """)
            rows = cur.fetchall()

        # Group sense rows under their term, preserving first-seen term order and
        # the sense order the query produced (latin_lemma, sense_id).
        term_rows: dict[int, dict] = {}
        sense_lists: dict[int, list[Sense]] = {}
        for row in rows:
            tid = row["term_id"]
            if tid not in term_rows:
                term_rows[tid] = row
                sense_lists[tid] = []
            # term.la_surface is shared by every sense row of the term.
            sense_lists[tid].append(
                Sense(
                    sense_id=row["sense_id"],
                    context_label=row["context_label"],
                    version=row["version"],
                    cs_lemma=row["cs_lemma"],
                    cs_content=row["cs_content"],
                    en_cue=row["en_cue"],
                    sk_content=row["sk_content"],
                    la_surface=row["la_surface"],
                )
            )

        terms = [
            Term.from_row(term_rows[tid], tuple(sense_lists[tid])) for tid in term_rows
        ]
        all_terms = sorted(terms, key=lambda t: t.latin_lemma)
        multiword = [t for t in all_terms if t.is_multiword]
        singleword = [t for t in all_terms if not t.is_multiword]
        return multiword, singleword

    def locked_terms(self, segment_id: int) -> list[Constraint]:
        """Return approved term constraints for a segment.

        Only approved senses with a SK rendering are included. The SK rendering
        is the highest-authority one (ORDER BY authority_rank, DISTINCT ON sense).
        """
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (gs.sense_id)
                    gt.latin_lemma,
                    gt.category,
                    gt.la_surface   AS latin_surface,
                    sr.content      AS required_slovak,
                    gs.sense_id,
                    gs.version,
                    gs.context_label
                FROM term_usage tu
                JOIN glossary_sense gs  ON gs.sense_id = tu.sense_id AND gs.status = 'approved'
                JOIN glossary_term  gt  ON gt.term_id  = gs.term_id
                JOIN sense_rendering sr ON sr.sense_id = gs.sense_id AND sr.lang = 'sk'
                JOIN source          s  ON s.source_id  = sr.source_id
                WHERE tu.segment_id = %s
                  AND sr.content IS NOT NULL
                ORDER BY gs.sense_id, s.authority_rank
                """,
                (segment_id,),
            )
            return [Constraint.from_row(r) for r in cur.fetchall()]

    def get_current_sense(self, sense_id: int) -> dict | None:
        """Fetch current version and status for a sense. Returns None if not found.

        Returns a dict ``{sense_id, version, status}`` — the approval importer
        only needs the status, but the full triple is kept for provenance.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT sense_id, version, status FROM glossary_sense WHERE sense_id = %s",
                (sense_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"sense_id": row[0], "version": row[1], "status": row[2]}

    def get_la_surface(self, sense_id: int) -> str | None:
        """Fetch la_surface for the term owning this sense. Returns None if absent."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT gt.la_surface
                FROM glossary_sense gs
                JOIN glossary_term gt ON gt.term_id = gs.term_id
                WHERE gs.sense_id = %s
                """,
                (sense_id,),
            )
            row = cur.fetchone()
        return row[0] if row is not None else None

    # ── Writes ─────────────────────────────────────────────────────────────────

    def update_sense_status(self, sense_id: int, status: str) -> None:
        """Set glossary_sense.status for a reviewer approval or rejection.

        Valid statuses: 'approved', 'rejected'. 'proposed' is the initial state
        set by the gap-term preseed and must not be set here.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE glossary_sense SET status = %s WHERE sense_id = %s",
                (status, sense_id),
            )

    def bump_sense_version(self, sense_id: int) -> int:
        """Increment glossary_sense.version and return the new value.

        The stale-segment query uses sense_version_used < current version to find
        segments that need re-translation after a reviewer correction.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE glossary_sense SET version = version + 1 "
                "WHERE sense_id = %s RETURNING version",
                (sense_id,),
            )
            return cur.fetchone()[0]

    def write_human_rendering(self, sense_id: int, sk_text: str, src_id: int) -> None:
        """Persist a reviewer-confirmed Slovak rendering.

        Writes to sense_rendering(lang='sk', source_id=src_id). The model-proposed
        rendering (source_id=model) is preserved alongside it for audit.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sense_rendering (sense_id, lang, content, source_id)
                VALUES (%s, 'sk', %s, %s)
                ON CONFLICT (sense_id, lang, source_id) DO UPDATE
                    SET content = EXCLUDED.content
                """,
                (sense_id, sk_text, src_id),
            )

    def write_context_label(self, sense_id: int, label: str | None) -> None:
        """Set glossary_sense.context_label. Does NOT bump the sense version.

        An empty label should be passed as None so it lands as SQL NULL.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE glossary_sense SET context_label = %s WHERE sense_id = %s",
                (label, sense_id),
            )

    def write_human_surface(self, sense_id: int, surface: str) -> None:
        """Write la_surface onto the glossary_term that owns this sense."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE glossary_term SET la_surface = %s
                WHERE term_id = (SELECT term_id FROM glossary_sense WHERE sense_id = %s)
                """,
                (surface, sense_id),
            )


class SegmentRepository:
    """All segment / segment_text access, plus corpus-wide status queries."""

    def __init__(self, conn):
        self.conn = conn

    # ── Per-segment reads ──────────────────────────────────────────────────────

    def get_segment(self, segment_id: int) -> Segment | None:
        """Return the segment with its la/cs/en texts, or None if not found.

        Carries reply_to and translation_status (the v_segment shape used by the
        translation loop).
        """
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    s.segment_id,
                    s.locator_path::text AS locator_path,
                    s.element_type,
                    s.reply_to,
                    s.translation_status,
                    max(t.content) FILTER (WHERE t.lang = 'la') AS latin,
                    max(t.content) FILTER (WHERE t.lang = 'cs') AS czech,
                    max(t.content) FILTER (WHERE t.lang = 'en') AS english
                FROM segment s
                LEFT JOIN segment_text t USING (segment_id)
                WHERE s.segment_id = %s
                GROUP BY s.segment_id, s.locator_path, s.element_type,
                         s.reply_to, s.translation_status
                """,
                (segment_id,),
            )
            row = cur.fetchone()
        return Segment.from_row(row) if row else None

    def get_segment_id_by_locator(self, locator: str, work_id: int | None = None) -> int | None:
        """Return the segment_id at an exact locator, or None if absent.

        Used by the text-overlay parsers (Czech/English) to attach text to an
        existing segment, and by the Latin structural parser to test whether a
        shared title placeholder already exists. When work_id is given the lookup
        is scoped to that work.
        """
        with self.conn.cursor() as cur:
            if work_id is None:
                cur.execute(
                    "SELECT segment_id FROM segment WHERE locator_path = %s::ltree",
                    (locator,),
                )
            else:
                cur.execute(
                    "SELECT segment_id FROM segment "
                    "WHERE locator_path = %s::ltree AND work_id = %s",
                    (locator, work_id),
                )
            row = cur.fetchone()
        return row[0] if row else None

    def get_article_title_locators(self) -> list[str]:
        """Return every article_title locator, ordered.

        The text-overlay parsers use this as the default set of articles to
        ingest when the caller passes none.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT locator_path::text FROM segment "
                "WHERE element_type = 'article_title' ORDER BY locator_path"
            )
            return [row[0] for row in cur.fetchall()]

    def load_body_segments(self, work_id: int) -> list[Segment]:
        """Return body segments with la/cs/en text for the work, sorted by locator."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT s.segment_id, s.locator_path::text AS locator_path, s.element_type,
                       max(t.content) FILTER (WHERE t.lang = 'la') AS latin,
                       max(t.content) FILTER (WHERE t.lang = 'cs') AS czech,
                       max(t.content) FILTER (WHERE t.lang = 'en') AS english
                FROM segment s
                LEFT JOIN segment_text t USING (segment_id)
                WHERE s.work_id = %s
                  AND s.element_type = ANY(%s)
                GROUP BY s.segment_id, s.locator_path, s.element_type
                ORDER BY s.locator_path
                """,
                (work_id, list(_BODY_TYPES)),
            )
            return [Segment.from_row(r) for r in cur.fetchall()]

    # ── Per-segment writes ─────────────────────────────────────────────────────

    def write_segment_text(self, segment_id: int, lang: str, src_id: int, content: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO segment_text (segment_id, lang, content, source_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (segment_id, lang, source_id) DO UPDATE
                    SET content = EXCLUDED.content
                """,
                (segment_id, lang, content, src_id),
            )

    # ── Structural writes (Latin segment-graph creation) ───────────────────────

    def wipe_article(self, article_locator: str, work_id: int) -> None:
        """Delete every row under an article locator, in FK-dependency order.

        run_segment → term_usage → segment_text → segment. Makes the Latin parser
        idempotent: re-parsing an article fully replaces its prior segment graph.
        """
        self._wipe(article_locator, work_id, match="<@")

    def wipe_segment(self, locator: str, work_id: int) -> None:
        """Delete a single segment and its dependents (exact-match locator).

        Same FK-dependency order as ``wipe_article`` but matches one locator
        exactly rather than a subtree — used for leaf segments like preambles.
        """
        self._wipe(locator, work_id, match="=")

    def _wipe(self, locator: str, work_id: int, *, match: str) -> None:
        """Delete a segment subtree (``match='<@'``) or a single segment
        (``match='='``) and its dependents, in FK-dependency order:
        run_segment → term_usage → segment_text → segment."""
        selector = (
            f"segment_id IN (SELECT segment_id FROM segment "
            f"WHERE locator_path {match} %s::ltree AND work_id = %s)"
        )
        with self.conn.cursor() as cur:
            cur.execute(f"DELETE FROM run_segment WHERE {selector}", (locator, work_id))
            cur.execute(f"DELETE FROM term_usage WHERE {selector}", (locator, work_id))
            cur.execute(f"DELETE FROM segment_text WHERE {selector}", (locator, work_id))
            cur.execute(
                f"DELETE FROM segment WHERE locator_path {match} %s::ltree AND work_id = %s",
                (locator, work_id),
            )

    def create_segment(self, work_id: int, locator: str, element_type: str) -> int:
        """Insert a segment row and return its new segment_id."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO segment (work_id, locator_path, element_type) "
                "VALUES (%s, %s::ltree, %s) RETURNING segment_id",
                (work_id, locator, element_type),
            )
            return cur.fetchone()[0]

    def set_reply_to(self, segment_id: int, reply_to: int) -> None:
        """Link a reply segment to the objection segment it answers."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE segment SET reply_to = %s WHERE segment_id = %s",
                (reply_to, segment_id),
            )

    def update_translation_status(self, segment_id: int, status: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE segment SET translation_status = %s WHERE segment_id = %s",
                (status, segment_id),
            )

    def write_reviewer_notes(self, segment_id: int, notes: dict, iteration: int) -> None:
        payload = {"iteration": iteration, **notes}
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE segment SET reviewer_notes = %s WHERE segment_id = %s",
                (psycopg2.extras.Json(payload), segment_id),
            )

    def update_sense_version_used(self, segment_id: int, sense_id: int, version: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE term_usage SET sense_version_used = %s "
                "WHERE segment_id = %s AND sense_id = %s",
                (version, segment_id, sense_id),
            )

    # ── Corpus-wide orchestration queries ──────────────────────────────────────

    def get_all_article_locators(self, work_id: int = 1) -> list[str]:
        """Return distinct article-level locator prefixes (first 3 ltree components).

        Examples: 'I.q1.a1', 'I.q1.question_title', 'I.q2.a3', ... Each prefix is
        one unit of orchestration work. Segments at depth >= 3 are grouped by
        their 3-component prefix; depth-2 segments have no article anchor.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ltree2text(subpath(locator_path, 0, 3)) AS prefix
                FROM segment
                WHERE work_id = %s
                  AND nlevel(locator_path) >= 3
                ORDER BY prefix
                """,
                (work_id,),
            )
            return [row[0] for row in cur.fetchall()]

    def body_text_coverage(self, lang: str) -> tuple[int, int, list[str]]:
        """Return (segments_with_text, total_body_segments, missing_locators) for lang.

        Body segments are arg/sed_contra/respondeo/reply. Drives the per-source
        coverage report (e.g. the Czech Bahounek ingest).
        """
        body_types = ["arg", "sed_contra", "respondeo", "reply"]
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(DISTINCT st.segment_id) FROM segment_text st "
                "JOIN segment s ON st.segment_id = s.segment_id "
                "WHERE st.lang = %s AND s.element_type = ANY(%s)",
                (lang, body_types),
            )
            with_text = cur.fetchone()[0]

            cur.execute(
                "SELECT count(*) FROM segment WHERE element_type = ANY(%s)",
                (body_types,),
            )
            total = cur.fetchone()[0]

            cur.execute(
                "SELECT s.locator_path::text FROM segment s "
                "WHERE s.element_type = ANY(%s) AND NOT EXISTS ("
                "SELECT 1 FROM segment_text st "
                "WHERE st.segment_id = s.segment_id AND st.lang = %s) "
                "ORDER BY s.locator_path",
                (body_types, lang),
            )
            missing = [row[0] for row in cur.fetchall()]
        return with_text, total, missing

    def get_pending_segment_ids_for_article(
        self,
        locator_prefix: str,
        work_id: int = 1,
        segment_filter: frozenset[int] | None = None,
    ) -> list[int]:
        """Return pending segment IDs under locator_prefix that have translatable text.

        Ordered by locator_path so translate_segment calls are deterministic across
        workers. work_id guards against a different loaded work sharing the prefix.
        segment_filter: when provided, only those segment IDs are returned.
        """
        with self.conn.cursor() as cur:
            if segment_filter is not None:
                cur.execute(
                    """
                    SELECT s.segment_id
                    FROM segment s
                    WHERE s.locator_path <@ %s::ltree
                      AND s.work_id = %s
                      AND s.translation_status = 'pending'
                      AND s.segment_id = ANY(%s)
                      AND EXISTS (
                          SELECT 1 FROM segment_text st
                          WHERE st.segment_id = s.segment_id AND st.lang IN ('la', 'en')
                      )
                    ORDER BY s.locator_path
                    """,
                    (locator_prefix, work_id, list(segment_filter)),
                )
            else:
                cur.execute(
                    """
                    SELECT s.segment_id
                    FROM segment s
                    WHERE s.locator_path <@ %s::ltree
                      AND s.work_id = %s
                      AND s.translation_status = 'pending'
                      AND EXISTS (
                          SELECT 1 FROM segment_text st
                          WHERE st.segment_id = s.segment_id AND st.lang IN ('la', 'en')
                      )
                    ORDER BY s.locator_path
                    """,
                    (locator_prefix, work_id),
                )
            return [row[0] for row in cur.fetchall()]

    def has_pending_segments(
        self,
        locator_prefix: str,
        work_id: int = 1,
        segment_filter: frozenset[int] | None = None,
    ) -> bool:
        """Return True if the article has at least one pending segment.

        segment_filter: when provided, only those segment IDs count as pending.
        """
        with self.conn.cursor() as cur:
            if segment_filter is not None:
                cur.execute(
                    """
                    SELECT 1
                    FROM segment
                    WHERE locator_path <@ %s::ltree
                      AND work_id = %s
                      AND translation_status = 'pending'
                      AND segment_id = ANY(%s)
                    LIMIT 1
                    """,
                    (locator_prefix, work_id, list(segment_filter)),
                )
            else:
                cur.execute(
                    """
                    SELECT 1
                    FROM segment
                    WHERE locator_path <@ %s::ltree
                      AND work_id = %s
                      AND translation_status = 'pending'
                    LIMIT 1
                    """,
                    (locator_prefix, work_id),
                )
            return cur.fetchone() is not None

    def get_stale_segments(self, work_id: int = 1) -> list[int]:
        """Return segment IDs whose term_usage references an outdated glossary sense.

        A segment is stale when any sense it used has since been updated
        (sense_version_used < current glossary_sense.version). work_id scopes the
        result so a multi-work DB never cross-contaminates re-runs.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT tu.segment_id
                FROM term_usage tu
                JOIN glossary_sense gs ON tu.sense_id = gs.sense_id
                JOIN segment s ON s.segment_id = tu.segment_id
                WHERE s.work_id = %s
                  AND tu.sense_version_used < gs.version
                ORDER BY tu.segment_id
                """,
                (work_id,),
            )
            return [row[0] for row in cur.fetchall()]

    def get_human_edited_segments(self, segment_ids: list[int]) -> list[int]:
        """Return the subset of segment_ids that have a human-edited Slovak text row.

        A segment_text(sk) row from the 'human' source means a reviewer already
        touched the final text. rerun_stale must never reset such segments.
        """
        if not segment_ids:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT st.segment_id
                FROM segment_text st
                JOIN source s ON s.source_id = st.source_id
                WHERE st.segment_id = ANY(%s)
                  AND st.lang = 'sk'
                  AND s.code = 'human'
                ORDER BY st.segment_id
                """,
                (segment_ids,),
            )
            return [row[0] for row in cur.fetchall()]

    def flag_needs_human(self, segment_ids: list[int], note: str) -> None:
        """Set translation_status='needs_human' with a reviewer note, no re-translation.

        The note lands in reviewer_notes.last_feedback so it shows up in the
        needs-human triage report and the preview server detail panel.
        """
        if not segment_ids:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE segment "
                "SET translation_status = 'needs_human', reviewer_notes = %s "
                "WHERE segment_id = ANY(%s)",
                (psycopg2.extras.Json({"last_feedback": note}), segment_ids),
            )

    def reset_translation_status(self, segment_ids: list[int]) -> None:
        """Reset translation_status to 'pending' for the given segments.

        Existing segment_text(sk, model) and term_usage rows are left in place;
        translate_segment upserts over them on re-run. reviewer_notes is cleared
        since the old feedback is no longer valid.
        """
        if not segment_ids:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE segment "
                "SET translation_status = 'pending', reviewer_notes = NULL "
                "WHERE segment_id = ANY(%s)",
                (segment_ids,),
            )


class TermUsageRepository:
    """All term_usage access."""

    def __init__(self, conn):
        self.conn = conn

    def write_term_usage(self, segment_id: int, resolutions) -> int:
        """Write term_usage rows. Idempotent per (segment_id, sense_id). Returns count.

        Only 'guessed' rows are wiped — confirmed rows survive re-runs (Principle 3:
        re-runs are segment-scoped and never overwrite reviewed work).
        """
        if not resolutions:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM term_usage WHERE segment_id = %s AND status = 'guessed'",
                (segment_id,),
            )
            for res in resolutions:
                cur.execute(
                    """
                    INSERT INTO term_usage
                      (segment_id, sense_id, sense_version_used,
                       resolution_method, confidence, signals, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'guessed')
                    """,
                    (
                        segment_id,
                        res.sense.sense_id,
                        res.sense.version,
                        res.method,
                        res.confidence,
                        json.dumps(res.signals) if res.signals else None,
                    ),
                )
        return len(resolutions)


class RunRepository:
    """All translation_run / run_segment access.

    Unlike the other repositories, callers here open their own short-lived
    connection per phase (run start, run close), so the connection/commit lifecycle
    stays with the caller in translate.run; this class owns only the SQL.
    """

    def __init__(self, conn):
        self.conn = conn

    def glossary_snapshot(self) -> dict:
        """Return {approved_senses, max_version} for the run's provenance record."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FILTER (WHERE status = 'approved'), max(version) "
                "FROM glossary_sense"
            )
            approved, max_version = cur.fetchone()
        return {"approved_senses": approved, "max_version": max_version}

    def open_run(
        self,
        *,
        flow_name: str,
        git_sha: str | None,
        prompt_hash: str,
        snapshot: dict,
        translator_model: str,
        reviewer_model: str,
        temperature: float,
        filters: dict | None,
        max_workers: int,
    ) -> int:
        """Insert a translation_run row at flow start; return its run_id.

        finished_at stays NULL until finalize_run — a crashed run is recognizable.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO translation_run
                    (flow_name, git_sha, prompt_hash, glossary_snapshot,
                     translator_model, reviewer_model, temperature,
                     filters, max_workers)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING run_id
                """,
                (
                    flow_name,
                    git_sha,
                    prompt_hash,
                    psycopg2.extras.Json(snapshot),
                    translator_model,
                    reviewer_model,
                    temperature,
                    psycopg2.extras.Json(filters) if filters else None,
                    max_workers,
                ),
            )
            return cur.fetchone()[0]

    def insert_run_segments(self, run_id: int, records: list[dict]) -> None:
        """Bulk-insert run_segment rows from per-segment analytics records."""
        with self.conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO run_segment
                    (run_id, segment_id, final_status, iterations_used,
                     chosen_iteration, cost_usd, failure_classes, last_feedback)
                VALUES %s
                """,
                [
                    (
                        run_id,
                        rec["segment_id"],
                        rec["final_status"],
                        rec["iterations_used"],
                        rec["chosen_iteration"],
                        rec["cost_usd"],
                        psycopg2.extras.Json(rec["failure_classes"])
                        if rec["failure_classes"]
                        else None,
                        rec["last_feedback"],
                    )
                    for rec in records
                ],
            )

    def finalize_run(
        self,
        run_id: int,
        *,
        total_segments: int,
        total_translated: int,
        total_needs_human: int,
        total_cost: float,
    ) -> None:
        """Stamp finished_at and the run totals."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE translation_run
                SET finished_at = now(),
                    total_segments = %s,
                    total_translated = %s,
                    total_needs_human = %s,
                    total_cost_usd = %s
                WHERE run_id = %s
                """,
                (total_segments, total_translated, total_needs_human, total_cost, run_id),
            )
