"""Build a balanced segment sample for prompt optimization.

Category assignment (mutually exclusive, applied in order):
  complex_formula  — 8+ formula-category term_usage hits
  heavy_terms      — 5+ approved glossary_sense hits; not complex_formula
  standard_prose   — 0-2 approved hits; not in other sets
  (segments with 3-4 approved hits fall outside all categories and are skipped)

Global split: 30% complex_formula, 40% heavy_terms, 30% standard_prose.
Within each category, pars are sampled equally (I, I_II, II_II, III); shortfalls
from sparse pars are backfilled from whichever pars has segments to spare.

Usage:
    uv run python -m optimize.build_sample [n_segments] [--seed N] [--exclude FILE] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2

_DB_URL = "postgresql://aquinas:aquinas@localhost:5432/aquinas"
_SAMPLES_DIR = Path(__file__).resolve().parent / "samples"

_PARS = ("I", "I_II", "II_II", "III")
_CATEGORY_FRACTIONS = {"complex_formula": 0.30, "heavy_terms": 0.40, "standard_prose": 0.30}


def _assign_category(fml_hits: int, approved_hits: int) -> str | None:
    if fml_hits >= 8:
        return "complex_formula"
    if approved_hits >= 5:
        return "heavy_terms"
    if approved_hits <= 2:
        return "standard_prose"
    return None  # 3-4 approved hits — not in any category


def _pick(pool: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Pick up to n items from pool, preferring one segment per question."""
    rng.shuffle(pool)
    seen_q: set[str] = set()
    picked: list[dict] = []
    deferred: list[dict] = []
    for seg in pool:
        q = ".".join(seg["locator_path"].split(".")[:2])
        if q not in seen_q:
            picked.append(seg)
            seen_q.add(q)
        else:
            deferred.append(seg)
        if len(picked) == n:
            return picked
    picked.extend(deferred[: n - len(picked)])
    return picked[:n]


def _sample_category(
    pools_by_pars: dict[str, list[dict]],
    n: int,
    rng: random.Random,
) -> list[dict]:
    """Sample n segments with equal pars quotas; backfill shortfalls from remaining segments."""
    per_pars = n // len(_PARS)
    remainder = n % len(_PARS)
    quotas = {p: per_pars + (1 if i < remainder else 0) for i, p in enumerate(_PARS)}

    picked: list[dict] = []
    used_ids: set[int] = set()
    shortfall = 0

    for pars in _PARS:
        pool = list(pools_by_pars.get(pars, []))  # copy so shuffle is local
        got = _pick(pool, quotas[pars], rng)
        picked.extend(got)
        used_ids.update(s["segment_id"] for s in got)
        shortfall += quotas[pars] - len(got)

    if shortfall > 0:
        backfill = [
            s
            for pars in _PARS
            for s in pools_by_pars.get(pars, [])
            if s["segment_id"] not in used_ids
        ]
        picked.extend(_pick(backfill, shortfall, rng))

    return picked


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a balanced segment sample.")
    parser.add_argument(
        "n_segments", nargs="?", type=int, default=200,
        help="Total segments to sample (default: 200)",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--exclude", type=Path, default=None,
        help="JSON sample file whose questions to exclude",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Output path (default: samples/pilot_sample_N.json)",
    )
    args = parser.parse_args()

    n = args.n_segments
    rng = random.Random(args.seed)
    out = args.out or (_SAMPLES_DIR / f"pilot_sample_{n}.json")

    excluded_q: set[str] = set()
    if args.exclude and args.exclude.exists():
        with open(args.exclude) as f:
            data = json.load(f)
        excluded_q = {
            ".".join(s["locator_path"].split(".")[:2])
            for s in data["segments"]
        }

    conn = psycopg2.connect(_DB_URL)
    cur = conn.cursor()
    cur.execute("""
        SELECT
            s.segment_id,
            s.locator_path::text,
            s.element_type,
            COALESCE(c.fml_hits, 0),
            COALESCE(c.approved_hits, 0)
        FROM segment s
        LEFT JOIN (
            SELECT
                tu.segment_id,
                COUNT(*) FILTER (WHERE gt.category = 'formula') AS fml_hits,
                COUNT(*) FILTER (WHERE gs.status = 'approved')  AS approved_hits
            FROM term_usage tu
            JOIN glossary_sense gs ON tu.sense_id = gs.sense_id
            JOIN glossary_term gt ON gs.term_id = gt.term_id
            GROUP BY tu.segment_id
        ) c ON c.segment_id = s.segment_id
        WHERE s.element_type IN ('arg', 'reply', 'respondeo', 'sed_contra')
    """)
    all_rows = cur.fetchall()
    conn.close()

    # pools[category][pars] = list of candidate segments
    pools: dict[str, dict[str, list[dict]]] = {cat: defaultdict(list) for cat in _CATEGORY_FRACTIONS}
    for sid, lp, etype, fml_hits, approved_hits in all_rows:
        parts = lp.split(".")
        q = f"{parts[0]}.{parts[1]}"
        if q in excluded_q:
            continue
        pars = parts[0]
        if pars not in _PARS:
            continue
        category = _assign_category(fml_hits, approved_hits)
        if category is None:
            continue
        pools[category][pars].append({
            "segment_id": sid,
            "locator_path": lp,
            "element_type": etype,
            "fml_hits": fml_hits,
            "approved_hits": approved_hits,
            "category": category,
        })

    # Global category targets — standard_prose absorbs rounding remainder
    n_complex = round(n * _CATEGORY_FRACTIONS["complex_formula"])
    n_heavy = round(n * _CATEGORY_FRACTIONS["heavy_terms"])
    n_standard = n - n_complex - n_heavy

    selected: list[dict] = []
    for cat, n_cat in [
        ("complex_formula", n_complex),
        ("heavy_terms", n_heavy),
        ("standard_prose", n_standard),
    ]:
        got = _sample_category(pools[cat], n_cat, rng)
        if len(got) < n_cat:
            print(
                f"  WARNING: {cat} wanted {n_cat}, got {len(got)} "
                f"(pool={sum(len(v) for v in pools[cat].values())})",
                file=sys.stderr,
            )
        selected.extend(got)

    pars_got = Counter(s["locator_path"].split(".")[0] for s in selected)
    cat_got = Counter(s["category"] for s in selected)
    type_got = Counter(s["element_type"] for s in selected)
    q_count = len({".".join(s["locator_path"].split(".")[:2]) for s in selected})
    print(f"Selected: {len(selected)} segments from {q_count} questions")
    print(f"Pars:     {dict(sorted(pars_got.items()))}")
    print(f"Category: {dict(sorted(cat_got.items()))}")
    print(f"Type:     {dict(sorted(type_got.items()))}")

    output = {
        "description": f"{n}-segment sample for prompt optimization",
        "selection_criteria": {
            "complex_formula": "8+ formula-category term_usage hits; spread across Summa parts and element types",
            "heavy_terms": "5+ approved-status glossary_sense hits; NOT in complex_formula set; spread across parts/types",
            "standard_prose": "0-2 approved terms; NOT in other sets; pseudo-random spread across parts/types",
            "pars_split": "equal quota per pars within each category; shortfalls backfilled from other pars",
            "category_fractions": _CATEGORY_FRACTIONS,
            "seed": args.seed,
            **({"excludes_questions_from": str(args.exclude)} if args.exclude else {}),
        },
        "counts": {"total": len(selected), **dict(cat_got)},
        "segments": sorted(selected, key=lambda s: s["locator_path"]),
    }
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"Written to {out}")


if __name__ == "__main__":
    main()
