"""Build a 200-segment golden set for prompt optimization.

Samples by pars × element_type to get a well-balanced mix of article parts.
Category (complex_formula / heavy_terms / standard_prose) is assigned from DB
state and recorded as metadata — not used as a sampling constraint because
respondeo/sed_contra segments are almost universally complex_formula given
the current approved formula senses.

Targets:
  I=70, I_II=50, II_II=50, III=30
  Each pars: arg≈20%, reply≈30%, respondeo≈30%, sed_contra≈20%

Excludes all questions already covered in docs/pilot_sample_100.json.
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2

_DB_URL = "postgresql://aquinas:aquinas@localhost:5432/aquinas"
_CURRENT_SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "pilot_sample_100.json"
_OUT = Path(__file__).resolve().parents[1] / "docs" / "pilot_sample_200.json"

# (pars, type_bucket) → count
_TARGETS: dict[tuple[str, str], int] = {
    ("I",     "arg"):       14,
    ("I",     "reply"):     21,
    ("I",     "respondeo"): 21,
    ("I",     "sed_contra"):14,
    ("I_II",  "arg"):       10,
    ("I_II",  "reply"):     15,
    ("I_II",  "respondeo"): 15,
    ("I_II",  "sed_contra"):10,
    ("II_II", "arg"):       10,
    ("II_II", "reply"):     15,
    ("II_II", "respondeo"): 15,
    ("II_II", "sed_contra"):10,
    ("III",   "arg"):        6,
    ("III",   "reply"):      9,
    ("III",   "respondeo"):  9,
    ("III",   "sed_contra"): 6,
}  # total = 200


def _assign_category(fml_approved: int, nfml_approved: int) -> str:
    if fml_approved >= 1:
        return "complex_formula"
    if nfml_approved >= 3:
        return "heavy_terms"
    return "standard_prose"


def _type_bucket(element_type: str) -> str:
    if element_type.startswith("arg"):
        return "arg"
    if element_type.startswith("reply"):
        return "reply"
    return element_type  # respondeo, sed_contra


def _pick(pool: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Pick up to n items, preferring one segment per question."""
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


def main(seed: int = 42) -> None:
    rng = random.Random(seed)

    with open(_CURRENT_SAMPLE) as f:
        current = json.load(f)
    excluded_q = {
        ".".join(s["locator_path"].split(".")[:2])
        for s in current["segments"]
    }

    conn = psycopg2.connect(_DB_URL)
    cur = conn.cursor()
    cur.execute("""
        SELECT
            s.segment_id,
            s.locator_path::text,
            s.element_type,
            COALESCE(c.fml_approved, 0),
            COALESCE(c.nfml_approved, 0)
        FROM segment s
        LEFT JOIN (
            SELECT
                tu.segment_id,
                COUNT(DISTINCT gt.term_id) FILTER (WHERE gt.category = 'formula')     AS fml_approved,
                COUNT(DISTINCT gt.term_id) FILTER (WHERE gt.category != 'formula')    AS nfml_approved
            FROM term_usage tu
            JOIN glossary_sense gs ON tu.sense_id = gs.sense_id AND gs.status = 'approved'
            JOIN glossary_term gt ON gs.term_id = gt.term_id
            GROUP BY tu.segment_id
        ) c ON c.segment_id = s.segment_id
        WHERE s.element_type IN ('arg', 'reply', 'respondeo', 'sed_contra')
    """)
    all_rows = cur.fetchall()
    conn.close()

    # pool[(pars, type_bucket)] = list of candidate dicts
    pool: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for sid, lp, etype, fml_a, nfml_a in all_rows:
        parts = lp.split(".")
        q = f"{parts[0]}.{parts[1]}"
        if q in excluded_q:
            continue
        pars = parts[0]
        if pars not in ("I", "I_II", "II_II", "III"):
            continue
        tb = _type_bucket(etype)
        pool[(pars, tb)].append({
            "segment_id": sid,
            "locator_path": lp,
            "element_type": etype,
            "score": fml_a + nfml_a,
            "category": _assign_category(fml_a, nfml_a),
        })

    selected: list[dict] = []
    for (pars, tb), target in sorted(_TARGETS.items()):
        candidates = pool[(pars, tb)]
        picked = _pick(candidates, target, rng)
        if len(picked) < target:
            print(
                f"  WARNING: {pars}/{tb} wanted {target}, got {len(picked)} "
                f"(pool={len(candidates)})",
                file=sys.stderr,
            )
        selected.extend(picked)

    # Report
    pars_got = Counter(s["locator_path"].split(".")[0] for s in selected)
    cat_got = Counter(s["category"] for s in selected)
    type_got = Counter(_type_bucket(s["element_type"]) for s in selected)
    q_count = len({".".join(s["locator_path"].split(".")[:2]) for s in selected})
    print(f"Selected: {len(selected)} segments from {q_count} questions")
    print(f"Pars:     {dict(sorted(pars_got.items()))}")
    print(f"Category: {dict(sorted(cat_got.items()))}")
    print(f"Type:     {dict(sorted(type_got.items()))}")

    output = {
        "description": "200-segment pilot sample for prompt generalization test (epoch 4+ baseline)",
        "selection_criteria": {
            "pars_targets": {"I": 70, "I_II": 50, "II_II": 50, "III": 30},
            "type_fraction": {"arg": "20%", "reply": "30%", "respondeo": "30%", "sed_contra": "20%"},
            "excludes_questions_from": "docs/pilot_sample_100.json",
            "seed": seed,
        },
        "counts": {"total": len(selected), **{k: v for k, v in cat_got.items()}},
        "segments": sorted(selected, key=lambda s: s["locator_path"]),
    }
    _OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"Written to {_OUT}")


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 42
    main(seed)
