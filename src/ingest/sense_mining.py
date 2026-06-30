"""Sense mining — discover multisense term renderings from the cs/en corpus.

The corpus already contains two professional segment-aligned translations
(Bahounek cs ~91%, Dominican en ~99%). For every glossary term we look at the
segments that use it (via term_usage) and ask: which Czech lemmas are unusually
associated with this term's segments compared to the corpus baseline? One
dominant rendering confirms a single sense. Multiple strong, distinct
renderings mean polysemy the glossary is missing — exactly the failure class
behind 'ratio→rozum forced everywhere'.

Pipeline:
  1. collect    — segments per term with cs/en text (term_usage join, no new
                  resolution pass)
  2. mine       — per-term Czech lemma document frequencies scored by lift
                  (term rate / corpus rate); >= 2 strong renderings → candidate
  3. label      — one DeepSeek call per candidate term with sample contexts per
                  rendering cluster → per sense: context_label (English),
                  en_cue, cs_lemma, proposed sk
  4. write      — glossary_sense(status='proposed') + sense_rendering rows
                  (cs lemma / en cue / sk content) — the exact shape
                  _resolve_multi's evidence vote consumes

Review then flows through the existing Sheets export; import_approvals bumps
versions; rerun_stale re-translates affected segments.

Usage:
  # Dry-run on known polysemes (no API, no writes — prints mined clusters):
  uv run python -m ingest.sense_mining --terms ratio species principium

  # Add DeepSeek labels (API, no writes):
  uv run python -m ingest.sense_mining --terms ratio species principium --label

  # Full corpus scan, write proposed senses to DB:
  uv run python -m ingest.sense_mining --all --write
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from common.deepseek_client import DeepSeekClient
from common.lemmatize import lemmatize_czech
from storage.db import get_conn, source_id

# ── Mining knobs ──────────────────────────────────────────────────────────────

_MIN_TERM_SEGMENTS = 10   # terms with fewer segments have too little evidence
_MIN_RENDERING_DF = 3     # rendering must appear in >= this many of the term's segments
_MIN_RENDERING_RATE = 0.05  # ... and in >= 5% of them
_MIN_LIFT = 8.0           # term rate must be >= 8x the corpus baseline rate
_MAX_RENDERINGS = 6       # keep at most this many clusters per term
_SAMPLES_PER_CLUSTER = 3  # contexts sent to DeepSeek per rendering cluster
_LABEL_MAX_WORKERS = 5

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


@functools.lru_cache(maxsize=500_000)
def _cz_lemmas(token: str) -> tuple[str, ...]:
    """Cached Czech lemmatization — the corpus revisits the same tokens constantly."""
    return tuple(lm.lower() for lm in lemmatize_czech(token))


def _segment_lemmas(text: str) -> set[str]:
    """Distinct lowercased Czech lemmas of one segment (document-frequency unit)."""
    lemmas: set[str] = set()
    for token in _TOKEN_RE.findall(text):
        lemmas.update(_cz_lemmas(token))
    return lemmas


# ── 1. Collect ────────────────────────────────────────────────────────────────


def fetch_minable_terms(conn, min_segments: int = _MIN_TERM_SEGMENTS) -> list[dict]:
    """Terms with at least min_segments distinct segments in term_usage."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT gt.term_id, gt.latin_lemma, count(DISTINCT tu.segment_id) AS n_segments
            FROM glossary_term gt
            JOIN glossary_sense gs USING (term_id)
            JOIN term_usage tu ON tu.sense_id = gs.sense_id
            WHERE gs.status = 'approved'
            GROUP BY gt.term_id, gt.latin_lemma
            HAVING count(DISTINCT tu.segment_id) >= %s
            ORDER BY gt.latin_lemma
            """,
            (min_segments,),
        )
        return [
            {"term_id": r[0], "latin_lemma": r[1], "n_segments": r[2]}
            for r in cur.fetchall()
        ]


def fetch_term_segments(conn, term_id: int) -> list[dict]:
    """Segments using any sense of term_id, with their cs/en text (when present)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tu.segment_id,
                   max(st.content) FILTER (WHERE st.lang = 'cs') AS cs,
                   max(st.content) FILTER (WHERE st.lang = 'en') AS en
            FROM term_usage tu
            JOIN glossary_sense gs ON gs.sense_id = tu.sense_id
            LEFT JOIN segment_text st
                   ON st.segment_id = tu.segment_id AND st.lang IN ('cs', 'en')
            WHERE gs.term_id = %s
            GROUP BY tu.segment_id
            """,
            (term_id,),
        )
        return [{"segment_id": r[0], "cs": r[1], "en": r[2]} for r in cur.fetchall()]


def fetch_existing_senses(conn, term_id: int) -> list[dict]:
    """Existing senses of a term (any status) with their cs lemma — dedup guard."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT gs.sense_id, gs.context_label, gs.status,
                   max(sr.lemma) FILTER (WHERE sr.lang = 'cs') AS cs_lemma,
                   max(sr.content) FILTER (WHERE sr.lang = 'sk') AS sk
            FROM glossary_sense gs
            LEFT JOIN sense_rendering sr ON sr.sense_id = gs.sense_id
            WHERE gs.term_id = %s
            GROUP BY gs.sense_id, gs.context_label, gs.status
            """,
            (term_id,),
        )
        return [
            {
                "sense_id": r[0],
                "context_label": r[1],
                "status": r[2],
                "cs_lemma": r[3],
                "sk": r[4],
            }
            for r in cur.fetchall()
        ]


# ── 2. Mine ───────────────────────────────────────────────────────────────────


def compute_corpus_baseline(conn) -> tuple[Counter, int]:
    """Document frequency of every Czech lemma over all segments with cs text.

    Computed once per run (~20k segments); the per-term lift scores divide by it.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT content FROM segment_text WHERE lang = 'cs'")
        rows = cur.fetchall()

    df: Counter = Counter()
    for (text,) in rows:
        df.update(_segment_lemmas(text or ""))
    return df, len(rows)


def mine_renderings(
    segments: list[dict],
    baseline_df: Counter,
    n_corpus: int,
    min_df: int = _MIN_RENDERING_DF,
    min_rate: float = _MIN_RENDERING_RATE,
    min_lift: float = _MIN_LIFT,
    max_renderings: int = _MAX_RENDERINGS,
) -> list[dict]:
    """Score Czech lemmas by association with the term's segments.

    lift = (df_in_term_segments / n_term_segments) / (df_in_corpus / n_corpus).
    A lemma must clear absolute support (min_df), coverage (min_rate) and
    lift (min_lift) to count as a rendering. Returns the top clusters sorted
    by coverage, each with sample segment_ids for the labeling step.
    """
    term_df: Counter = Counter()
    lemma_segments: dict[str, list[int]] = {}
    n_term = 0
    for seg in segments:
        cs = seg.get("cs")
        if not cs:
            continue
        n_term += 1
        for lemma in _segment_lemmas(cs):
            term_df[lemma] += 1
            lemma_segments.setdefault(lemma, []).append(seg["segment_id"])

    if n_term == 0:
        return []

    candidates = []
    for lemma, k in term_df.items():
        rate = k / n_term
        if k < min_df or rate < min_rate:
            continue
        # Exclude the term's own segments from the baseline to avoid deflating lift
        # for high-frequency terms (their segments inflate the corpus rate for their
        # dominant renderings, reducing apparent lift and masking polysemy candidates).
        n_exclusive = max(n_corpus - n_term, 1)
        corpus_exclusive = max(baseline_df.get(lemma, 0) - k, 0)
        corpus_rate = (corpus_exclusive or 0.5) / n_exclusive
        lift = rate / corpus_rate
        if lift < min_lift:
            continue
        candidates.append(
            {
                "cs_lemma": lemma,
                "df": k,
                "rate": round(rate, 3),
                "lift": round(lift, 1),
                "score": rate * math.log(lift),
                "segment_ids": lemma_segments[lemma][:_SAMPLES_PER_CLUSTER * 4],
            }
        )

    candidates.sort(key=lambda c: -c["score"])
    return candidates[:max_renderings]


def mine_english_cues(segments: list[dict], top_n: int = 8) -> list[str]:
    """Most frequent non-stopword English tokens in the term's segments.

    Crude on purpose — these are hints for the DeepSeek labeler, which picks
    the actual en_cue per sense. No English lemmatizer needed.
    """
    stop = frozenset(
        "the a an of to in is are was were and or for that this it as by with not be "
        "but on from at which who whom what there their they we you he she his her its "
        "all any some no nor so if then than thus also can may must should would could "
        "have has had do does did been being i one two such things thing said says say "
        "therefore now hence whether because since while when answer objection reply "
        "question article whatever wherefore through itself himself according above".split()
    )
    counts: Counter = Counter()
    for seg in segments:
        en = seg.get("en")
        if not en:
            continue
        tokens = {t.lower() for t in _TOKEN_RE.findall(en)}
        counts.update(t for t in tokens if t not in stop and len(t) > 2)
    return [t for t, _ in counts.most_common(top_n)]


# ── 3. Label (DeepSeek) ───────────────────────────────────────────────────────

_DEEPSEEK_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"
)
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

_client = DeepSeekClient(_DEEPSEEK_MODEL, url=_DEEPSEEK_URL, timeout=60)

_LABEL_SYSTEM = """\
You are a terminologist for a Latin→Slovak translation of Aquinas's Summa Theologiae.
A Latin term is rendered by several distinct Czech words in a professional Czech
translation — each distinct rendering usually marks a distinct sense of the term.

For each rendering cluster you receive sample sentence contexts. Decide which clusters
are genuinely distinct senses (merge clusters that are synonyms of the same sense;
drop clusters that are coincidental collocates, not renderings of the Latin term).

Return ONLY a JSON object:
{"senses": [{"cs_lemma": "<Czech lemma of the cluster>",
             "context_label": "<English, 3-6 words, e.g. 'as aspect or viewpoint'>",
             "en_cue": "<one lowercase English word typical for this sense>",
             "sk": "<Slovak lemma for this sense>"}]}
List the dominant sense first. Do not invent senses without cluster evidence."""


def _build_label_user_turn(
    latin_lemma: str,
    clusters: list[dict],
    contexts: dict[str, list[str]],
    en_cues: list[str],
) -> str:
    parts = [f"Latin term: {latin_lemma}"]
    if en_cues:
        parts.append(f"Frequent English words in this term's segments: {', '.join(en_cues)}")
    for c in clusters:
        parts.append(
            f"\nCluster '{c['cs_lemma']}' "
            f"(in {c['df']} segments, {c['rate'] * 100:.0f}% of term occurrences):"
        )
        for ctx in contexts.get(c["cs_lemma"], []):
            parts.append(f"  - {ctx[:300]}")
    return "\n".join(parts)


def call_deepseek_label(system: str, user: str, retries: int = 3) -> dict:
    """One labeling call; returns the parsed JSON object. Raises after retries."""
    if not os.environ.get("DEEPSEEK_API_KEY", ""):
        raise RuntimeError("DEEPSEEK_API_KEY is not set (load .env).")

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            chat = _client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                max_tokens=1024,
                response_format={"type": "json_object"},
            )
            return json.loads(chat.content)
        except (RuntimeError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"DeepSeek labeling failed after {retries} attempts: {last_exc}")


def fetch_cluster_contexts(conn, clusters: list[dict]) -> dict[str, list[str]]:
    """Czech text of sample segments per cluster (for the labeling prompt)."""
    seg_ids = sorted({sid for c in clusters for sid in c["segment_ids"][:_SAMPLES_PER_CLUSTER]})
    if not seg_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT segment_id, content FROM segment_text "
            "WHERE lang = 'cs' AND segment_id = ANY(%s)",
            (seg_ids,),
        )
        text_by_id = dict(cur.fetchall())
    return {
        c["cs_lemma"]: [
            text_by_id[sid]
            for sid in c["segment_ids"][:_SAMPLES_PER_CLUSTER]
            if sid in text_by_id
        ]
        for c in clusters
    }


def label_term(term: dict, clusters: list[dict], contexts: dict[str, list[str]], segments: list[dict]) -> list[dict]:
    """Label one candidate term's clusters via DeepSeek; returns sense dicts."""
    en_cues = mine_english_cues(segments)
    user = _build_label_user_turn(term["latin_lemma"], clusters, contexts, en_cues)
    result = call_deepseek_label(_LABEL_SYSTEM, user)
    senses = result.get("senses") or []
    cluster_lemmas = {c["cs_lemma"] for c in clusters}
    valid = []
    for s in senses:
        if not all(s.get(k) for k in ("cs_lemma", "context_label", "en_cue", "sk")):
            continue
        if s["cs_lemma"] not in cluster_lemmas:
            # Model must label mined evidence, not invent renderings.
            continue
        valid.append(s)
    return valid


# ── 4. Write ──────────────────────────────────────────────────────────────────


def write_proposed_senses(conn, term_id: int, senses: list[dict], src_model: int) -> int:
    """Insert proposed glossary_sense + cs/en/sk renderings; returns insert count.

    Senses whose cs_lemma or context_label already exists for the term are
    skipped — never duplicate, never touch approved senses (Krystal is law).
    Matching is case-insensitive and tracked within the batch, so capital-variant
    proposals ("Vôľa" vs "vôľa", "Anima" vs "anima") never reach the reviewer as
    separate senses.
    """
    existing = fetch_existing_senses(conn, term_id)
    seen_cs = {e["cs_lemma"].casefold() for e in existing if e["cs_lemma"]}
    seen_labels = {e["context_label"].casefold() for e in existing if e["context_label"]}

    written = 0
    with conn.cursor() as cur:
        for s in senses:
            cs_key = s["cs_lemma"].casefold()
            label_key = s["context_label"].casefold()
            if cs_key in seen_cs or label_key in seen_labels:
                continue
            seen_cs.add(cs_key)
            seen_labels.add(label_key)
            cur.execute(
                "INSERT INTO glossary_sense (term_id, context_label, status) "
                "VALUES (%s, %s, 'proposed') RETURNING sense_id",
                (term_id, s["context_label"]),
            )
            sense_id = cur.fetchone()[0]
            for lang, lemma, content in (
                ("cs", s["cs_lemma"], s["cs_lemma"]),
                ("en", None, s["en_cue"]),
                ("sk", None, s["sk"]),
            ):
                cur.execute(
                    """
                    INSERT INTO sense_rendering (sense_id, lang, lemma, content, source_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (sense_id, lang, source_id) DO NOTHING
                    """,
                    (sense_id, lang, lemma, content, src_model),
                )
            written += 1
    conn.commit()
    return written


# ── Orchestration ─────────────────────────────────────────────────────────────


def mine_term(conn, term: dict, baseline_df: Counter, n_corpus: int) -> dict:
    """Collect + mine one term. Returns {term, segments, clusters, is_candidate}."""
    segments = fetch_term_segments(conn, term["term_id"])
    clusters = mine_renderings(segments, baseline_df, n_corpus)
    return {
        "term": term,
        "segments": segments,
        "clusters": clusters,
        "is_candidate": len(clusters) >= 2,
    }


def _print_mined(result: dict) -> None:
    term = result["term"]
    flag = "POLYSEMY CANDIDATE" if result["is_candidate"] else "single/none"
    print(f"\n{term['latin_lemma']} — {len(result['segments'])} segments — {flag}")
    for c in result["clusters"]:
        print(
            f"  {c['cs_lemma']:<20} df={c['df']:>4}  rate={c['rate']:>5.0%}"
            f"  lift={c['lift']:>7.1f}  score={c['score']:.3f}"
        )


def run(
    terms_filter: list[str] | None,
    do_label: bool,
    do_write: bool,
    min_segments: int = _MIN_TERM_SEGMENTS,
) -> None:
    with get_conn() as conn:
        all_terms = fetch_minable_terms(conn, min_segments)
        if terms_filter:
            wanted = set(terms_filter)
            all_terms = [t for t in all_terms if t["latin_lemma"] in wanted]
            missing = wanted - {t["latin_lemma"] for t in all_terms}
            if missing:
                print(f"[WARN] not minable (absent or < {min_segments} segments): {sorted(missing)}")
        print(f"Mining {len(all_terms)} terms...", flush=True)

        print("Computing corpus baseline (cs lemma document frequencies)...", flush=True)
        t0 = time.monotonic()
        baseline_df, n_corpus = compute_corpus_baseline(conn)
        print(f"  {len(baseline_df)} lemmas over {n_corpus} segments "
              f"in {time.monotonic() - t0:.0f}s", flush=True)

        results = []
        for i, term in enumerate(all_terms, 1):
            results.append(mine_term(conn, term, baseline_df, n_corpus))
            if i % 200 == 0:
                print(f"  mined {i}/{len(all_terms)} terms", flush=True)

        candidates = [r for r in results if r["is_candidate"]]
        for r in results if terms_filter else candidates:
            _print_mined(r)
        print(f"\n{len(candidates)} polysemy candidates of {len(results)} mined terms")

        if not do_label:
            return

        src_model = source_id(conn, "model") if do_write else None
        print(f"\nLabeling {len(candidates)} candidates via DeepSeek "
              f"({_LABEL_MAX_WORKERS} concurrent)...", flush=True)

        # Pre-fetch all cluster contexts in the main thread — psycopg2 connections
        # are not thread-safe; fetch before handing work to the pool.
        for r in candidates:
            r["contexts"] = fetch_cluster_contexts(conn, r["clusters"])

        total_written = 0
        with ThreadPoolExecutor(max_workers=_LABEL_MAX_WORKERS) as pool:
            futures = {
                pool.submit(label_term, r["term"], r["clusters"], r["contexts"], r["segments"]): r
                for r in candidates
            }
            for future in as_completed(futures):
                r = futures[future]
                lemma = r["term"]["latin_lemma"]
                try:
                    senses = future.result()
                except RuntimeError as exc:
                    print(f"  [WARN] {lemma}: labeling failed — {exc}", flush=True)
                    continue
                print(f"\n  {lemma}: {len(senses)} senses")
                for s in senses:
                    print(f"    [{s['context_label']}] cs={s['cs_lemma']} "
                          f"en={s['en_cue']} sk={s['sk']}")
                if do_write and senses:
                    n = write_proposed_senses(conn, r["term"]["term_id"], senses, src_model)
                    total_written += n
                    print(f"    → {n} proposed senses written")
        if do_write:
            print(f"\nTotal proposed senses written: {total_written}")
            print("Next: export to Sheets for review → import_approvals → re-resolve → rerun_stale")


def main() -> None:
    parser = argparse.ArgumentParser(description="glossary sense mining")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--terms", nargs="+", metavar="LEMMA",
                       help="mine only these Latin lemmas (dry-run friendly)")
    group.add_argument("--all", action="store_true", help="mine every minable term")
    parser.add_argument("--label", action="store_true",
                        help="label polysemy candidates via DeepSeek")
    parser.add_argument("--write", action="store_true",
                        help="write labeled senses as 'proposed' (implies --label)")
    parser.add_argument("--min-segments", type=int, default=_MIN_TERM_SEGMENTS)
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()
    run(
        terms_filter=args.terms,
        do_label=args.label or args.write,
        do_write=args.write,
        min_segments=args.min_segments,
    )


if __name__ == "__main__":
    sys.exit(main())
