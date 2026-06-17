"""Gap-term scanning, batch proposal, and DB preseed."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from cltk.stops.lat import STOPS as _CLTK_STOPS

from common.deepseek import _call_deepseek_batch
from common.lemmatize import lemmatize_latin
from storage.models import Segment

# ── Gap term proposal knobs ───────────────────────────────────────────────────
# Override via run() parameters or GAP_* env vars read in pipeline.py.

_GAP_FREQ_FLOOR: int = 10
# Lower gate lets 4-5 char core Scholastic vocabulary through (actus, esse, bonum).
# Spurious function words are caught by CLTK stops + freq ceiling + DB stopword list.
_GAP_MIN_LEN: int = 3
# Zipf ceiling: lemmas appearing in >40% of segments are structural connectors, not
# terminology (dico appears in 77% of Q1–Q6 segments). Applied after freq collection.
_GAP_FREQ_CEILING_PCT: float = 0.40
_GAP_BATCH_SIZE: int = 50   # lemmas per DeepSeek batch call
_GAP_MAX_WORKERS: int = 10  # concurrent batch requests


def _strip_lemma_suffix(lemma: str) -> str:
    """Strip CLTK's trailing numeric disambiguation suffix (dico2 → dico)."""
    return re.sub(r"\d+$", "", lemma)


def _canonical_lemma(lemma: str) -> str:
    """Canonical gap-lemma form: numeric suffix stripped, then lowercased.

    Latin dictionary lemmas are conventionally lowercase, but CLTK returns
    sentence-initial / proper-case tokens capitalized ("Actus", "Caritas").
    Without canonicalization those become capital-variant *duplicate* gap terms
    (Actus alongside actus) — pure noise for the reviewer — and a capitalized
    token can even shadow a lowercase Krystal term ("Caritas" leaking as a gap
    proposal next to the approved "caritas"). Every Krystal lemma is lowercase,
    so lowercasing both deduplicates gap terms and makes Krystal membership
    case-insensitive.
    """
    return _strip_lemma_suffix(lemma).lower()


def _load_ignored_lemmas(conn) -> frozenset[str]:
    """Load lemmas with category='stopword' from DB — permanently silenced by reviewers."""
    with conn.cursor() as cur:
        cur.execute("SELECT latin_lemma FROM glossary_term WHERE category = 'stopword'")
        return frozenset(row[0].lower() for row in cur.fetchall() if row[0] is not None)


def _scan_gap_lemmas(
    segments: list[Segment],
    krystal_lemmas: set[str],
    freq_floor: int,
    min_len: int = _GAP_MIN_LEN,
    freq_ceiling_pct: float = _GAP_FREQ_CEILING_PCT,
    ignored_lemmas: frozenset[str] = frozenset(),
) -> dict[str, dict]:
    """One read-only pass over all segments to collect gap lemma data.

    Returns {lemma: {freq, best_latin, best_czech, best_english}} for lemmas
    (CLTK lemma with numeric suffix stripped) that appear in ≥ freq_floor segments
    and pass all noise filters.

    Noise filters applied in order:
      1. Length gate (len > min_len) — drops 1-3 char function words
      2. CLTK STOPS (case-insensitive) — drops Classical Latin stopwords
      3. DB ignored_lemmas (category='stopword') — drops Scholastic structural words
         permanently silenced by reviewers
      4. Freq ceiling (freq ≤ freq_ceiling_pct * total_segments) — drops Zipfian
         connectors that appear in too many segments to be theological terms
    """
    lemma_data: dict[str, dict] = {}
    total_segments = len(segments)
    # Self-consistent regardless of caller casing: Krystal membership is tested
    # against lowercase lemmas (see _canonical_lemma).
    krystal_lemmas = {k.lower() for k in krystal_lemmas}

    for seg in segments:
        latin = seg.latin or ""
        if not latin:
            continue
        czech = seg.czech or ""
        english = seg.english or ""

        seen_in_seg: set[str] = set()
        for token in re.findall(r"[a-zA-Z]+", latin):
            cands = lemmatize_latin(token)
            if not cands:
                continue
            # Phase 2 tries all candidates for Krystal lookup; skip here if any would
            # hit. Case-insensitive so a sentence-initial "Caritas" resolves to the
            # Krystal "caritas" instead of leaking as a gap proposal.
            if any(c.lower() in krystal_lemmas for c in cands):
                continue
            lemma = _canonical_lemma(cands[0])  # suffix-stripped + lowercased
            if (
                len(lemma) <= min_len
                or lemma in krystal_lemmas
                or lemma in _CLTK_STOPS
                or lemma in ignored_lemmas
                or lemma in seen_in_seg
            ):
                continue
            seen_in_seg.add(lemma)

            d = lemma_data.get(lemma)
            if d is None:
                d = lemma_data[lemma] = {
                    "freq": 0,
                    "best_latin": latin[:300],
                    "best_czech": czech[:300],
                    "best_english": english[:300],
                }
            d["freq"] += 1
            # Upgrade context if current best lacks Czech or English
            if czech and not d["best_czech"]:
                d["best_czech"] = czech[:300]
            if english and not d["best_english"]:
                d["best_english"] = english[:300]

    if total_segments == 0:
        import sys
        print("[WARN] _scan_gap_lemmas called with 0 segments — returning empty result", file=sys.stderr)
        return {}
    freq_ceiling = int(total_segments * freq_ceiling_pct)
    return {
        lemma: d for lemma, d in lemma_data.items()
        if freq_floor <= d["freq"] <= freq_ceiling
    }


def _ensure_glossary_term(conn, lemma: str, category: str | None = None) -> int:
    """Insert glossary_term (with model category) if not present; return term_id.

    On conflict the category is refreshed only for gap terms (no approved senses).
    Krystal-seeded terms have approved senses and must never have their category
    overwritten by a model re-run.

    The lemma is canonicalized to lowercase so capital-variant tokens never create
    a duplicate gap term (Actus vs actus) — the unique constraint on latin_lemma
    is case-sensitive, so this guarantee must live here, not only in the scanner.
    """
    lemma = _canonical_lemma(lemma)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO glossary_term (latin_lemma, is_multiword, category)
            VALUES (%s, false, %s)
            ON CONFLICT (latin_lemma) DO UPDATE
                SET category = EXCLUDED.category
                WHERE NOT EXISTS (
                    SELECT 1 FROM glossary_sense
                    WHERE term_id = glossary_term.term_id AND status = 'approved'
                )
            RETURNING term_id
            """,
            (lemma, category),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        # Conflict on a Krystal term (approved senses exist) — fetch without updating
        cur.execute(
            "SELECT term_id FROM glossary_term WHERE latin_lemma = %s",
            (lemma,),
        )
        return cur.fetchone()[0]


def _write_gap_proposals(conn, proposals: dict, src_model: int) -> dict[str, dict]:
    """Pre-write glossary_term(category) + glossary_sense + sense_rendering per gap term.

    Uses DO UPDATE for sense_rendering so re-runs refresh proposals. Returns
    gap_terms_db: {lemma: {term_id, sense_id, version, category, slovak}}
    for the main loop to attach term_usage rows to.
    """
    gap_terms_db: dict[str, dict] = {}
    for lemma, term in sorted(proposals["terms"].items()):
        sk_proposal = term["slovak"]
        category = term["category"]
        term_id = _ensure_glossary_term(conn, lemma, category)
        with conn.cursor() as cur:
            # If this CLTK lemma is already a Krystal term (has an approved sense),
            # skip creating a gap proposal — it resolves via the Krystal path in Phase 2.
            cur.execute(
                "SELECT sense_id, status FROM glossary_sense "
                "WHERE term_id = %s AND context_label IS NULL",
                (term_id,),
            )
            existing = cur.fetchone()
            if existing is not None and existing[1] == "approved":
                continue
            # glossary_sense has no unique constraint covering (term_id, NULL context_label)
            # because NULLs are not equal under standard SQL uniqueness. Use explicit
            # SELECT-then-INSERT to avoid duplicate proposed senses on re-runs.
            row = existing  # may be an existing proposed sense
            if row is None:
                cur.execute(
                    "INSERT INTO glossary_sense (term_id, context_label, status) "
                    "VALUES (%s, NULL, 'proposed') RETURNING sense_id",
                    (term_id,),
                )
                row = cur.fetchone()
            sense_id = row[0]
            # Overwrite on re-run to refresh the proposal
            cur.execute(
                """
                INSERT INTO sense_rendering (sense_id, lang, content, source_id)
                VALUES (%s, 'sk', %s, %s)
                ON CONFLICT (sense_id, lang, source_id) DO UPDATE SET content = EXCLUDED.content
                """,
                (sense_id, sk_proposal, src_model),
            )
            cur.execute("SELECT version FROM glossary_sense WHERE sense_id = %s", (sense_id,))
            version = cur.fetchone()[0]

        gap_terms_db[lemma] = {
            "term_id": term_id,
            "sense_id": sense_id,
            "version": version,
            "category": category,
            "slovak": sk_proposal,
        }
    conn.commit()
    return gap_terms_db


def _load_existing_gap_terms(conn) -> dict[str, dict]:
    """Return {latin_lemma: {term_id, sense_id, version, category, slovak}}
    for all gap terms already written to DB (proposed status, sk rendering present).
    Used on re-runs to skip re-calling DeepSeek for already-translated lemmas.
    Each key is the CLTK lemma (after suffix strip) — not a DeepSeek-corrected canonical.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT gt.latin_lemma, gt.term_id, gs.sense_id, gs.version,
                   gt.category, sr.content
            FROM glossary_term gt
            JOIN glossary_sense gs
              ON gs.term_id = gt.term_id AND gs.context_label IS NULL
            JOIN sense_rendering sr
              ON sr.sense_id = gs.sense_id AND sr.lang = 'sk'
            WHERE gs.status = 'proposed'
            """
        )
        return {
            row[0]: {
                "term_id": row[1],
                "sense_id": row[2],
                "version": row[3],
                "category": row[4],
                "slovak": row[5],
            }
            for row in cur.fetchall()
        }


def _propose_gap_terms(
    gap_data: dict[str, dict],
    batch_size: int = _GAP_BATCH_SIZE,
    max_workers: int = _GAP_MAX_WORKERS,
    conn=None,
    src_model: int | None = None,
) -> dict:
    """Batch-call DeepSeek to classify and translate gap lemmas.

    Each CLTK lemma is its own key — no canonicalization, no fragment merging.
    When conn + src_model are supplied each successful batch is written to DB
    immediately after it completes — no re-translation cost on retry runs.

    Returns:
      {
        "terms":        {lemma: {slovak, category, freq, best_latin, best_czech, best_english}},
        "dropped":      [lemma, ...],   # batch failed / model omitted
        "gap_terms_db": {lemma: {term_id, sense_id, version, category, slovak}}
                        (populated only when conn is provided),
      }
    """
    items = [
        {
            "lemma": lemma,
            "best_latin": d["best_latin"],
            "best_czech": d["best_czech"],
            "best_english": d["best_english"],
        }
        for lemma, d in sorted(gap_data.items())
    ]
    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]

    print(
        f"  Calling DeepSeek for {len(items)} gap lemmas "
        f"in {len(batches)} batches ({max_workers} concurrent)...",
        flush=True,
    )

    raw: dict[str, dict] = {}  # lemma → {category, slovak}
    gap_terms_db: dict[str, dict] = {}
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_batch = {pool.submit(_call_deepseek_batch, b): b for b in batches}
        for future in as_completed(future_to_batch):
            try:
                batch_raw = future.result()
            except Exception as exc:
                print(f"  [WARN] batch failed, lemmas dropped this run: {exc}", flush=True)
                completed += 1
                if completed % 20 == 0 or completed == len(batches):
                    print(f"  {completed}/{len(batches)} batches complete", flush=True)
                continue

            raw.update(batch_raw)

            # Write this batch's proposals to DB immediately when conn is supplied.
            if conn is not None and src_model is not None and batch_raw:
                batch_terms = {
                    lemma: {
                        "slovak": entry["slovak"],
                        "category": entry["category"],
                        "freq": gap_data[lemma]["freq"],
                        "best_latin": gap_data[lemma]["best_latin"],
                        "best_czech": gap_data[lemma]["best_czech"],
                        "best_english": gap_data[lemma]["best_english"],
                    }
                    for lemma, entry in batch_raw.items()
                }
                new_entries = _write_gap_proposals(conn, {"terms": batch_terms}, src_model)
                gap_terms_db.update(new_entries)

            completed += 1
            if completed % 20 == 0 or completed == len(batches):
                print(f"  {completed}/{len(batches)} batches complete", flush=True)

    dropped = [lemma for lemma in gap_data if lemma not in raw]
    if dropped:
        print(
            f"  [WARN] {len(dropped)} gap lemmas had no proposal (batch failure / model "
            f"omission) and are skipped this run; re-run to pick them up.",
            flush=True,
        )

    terms = {
        lemma: {
            "slovak": entry["slovak"],
            "category": entry["category"],
            "freq": gap_data[lemma]["freq"],
            "best_latin": gap_data[lemma]["best_latin"],
            "best_czech": gap_data[lemma]["best_czech"],
            "best_english": gap_data[lemma]["best_english"],
        }
        for lemma, entry in raw.items()
    }

    return {"terms": terms, "dropped": dropped, "gap_terms_db": gap_terms_db}
