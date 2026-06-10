"""Tests for src/ingest/sense_mining.py."""

from __future__ import annotations

from collections import Counter
from unittest.mock import MagicMock, patch

from ingest.sense_mining import (
    _MIN_LIFT,
    _build_label_user_turn,
    fetch_minable_terms,
    label_term,
    mine_english_cues,
    mine_renderings,
    write_proposed_senses,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _seg(segment_id: int, cs: str | None = None, en: str | None = None) -> dict:
    return {"segment_id": segment_id, "cs": cs, "en": en}


def _cursor(rows):
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    return cur


def _conn(rows=None):
    conn = MagicMock()
    conn.cursor.return_value = _cursor(rows or [])
    return conn


# ── mine_renderings ───────────────────────────────────────────────────────────


def test_mine_renderings_passes_threshold():
    """A lemma with high lift and sufficient df/rate passes all filters."""
    segments = [_seg(i, cs="rozum") for i in range(20)]
    # Corpus baseline: 'rozum' in 10 of 10000 corpus segments → rate ≈ 0.001
    baseline_df: Counter = Counter({"rozum": 10})
    n_corpus = 10_000

    # _segment_lemmas returns the actual lemmas; we patch it to avoid MorphoDiTa dependency
    with patch("ingest.sense_mining._segment_lemmas", return_value={"rozum"}):
        result = mine_renderings(segments, baseline_df, n_corpus)

    assert len(result) == 1
    r = result[0]
    assert r["cs_lemma"] == "rozum"
    # rate = 20/20 = 1.0; corpus_rate = 10/10000 = 0.001 → lift = 1000 >> 8
    assert r["lift"] > _MIN_LIFT
    assert r["df"] == 20
    assert r["rate"] == 1.0


def test_mine_renderings_filters_low_lift():
    """A lemma with low lift (common in corpus) is excluded."""
    segments = [_seg(i, cs="je") for i in range(20)]
    # 'je' is very common: 8000 of 10000 corpus segments → lift ≈ 1.25 < 8
    baseline_df: Counter = Counter({"je": 8000})
    n_corpus = 10_000

    with patch("ingest.sense_mining._segment_lemmas", return_value={"je"}):
        result = mine_renderings(segments, baseline_df, n_corpus)

    assert result == []


def test_mine_renderings_filters_low_df():
    """A lemma with df below min_df is excluded even if lift is high."""
    # Only 2 segments contain the lemma → below _MIN_RENDERING_DF=3
    segments = [_seg(1, cs="rare"), _seg(2, cs="rare")] + [_seg(i) for i in range(3, 20)]
    baseline_df: Counter = Counter()
    n_corpus = 10_000

    def fake_lemmas(text):
        return {"rare"} if text == "rare" else set()

    with patch("ingest.sense_mining._segment_lemmas", side_effect=fake_lemmas):
        result = mine_renderings(segments, baseline_df, n_corpus, min_df=3)

    assert all(r["cs_lemma"] != "rare" for r in result)


def test_mine_renderings_filters_low_rate():
    """A lemma with coverage below min_rate is excluded."""
    # 1 segment out of 25 → rate = 0.04 < _MIN_RENDERING_RATE=0.05
    segments = [_seg(1, cs="rare")] + [_seg(i) for i in range(2, 26)]
    baseline_df: Counter = Counter()
    n_corpus = 100

    def fake_lemmas(text):
        return {"rare"} if text == "rare" else set()

    with patch("ingest.sense_mining._segment_lemmas", side_effect=fake_lemmas):
        result = mine_renderings(segments, baseline_df, n_corpus, min_rate=0.05)

    assert all(r["cs_lemma"] != "rare" for r in result)


def test_mine_renderings_no_cs_text():
    """Segments without cs text contribute nothing."""
    segments = [_seg(i, cs=None) for i in range(20)]
    baseline_df: Counter = Counter()

    result = mine_renderings(segments, baseline_df, n_corpus=100)

    assert result == []


def test_mine_renderings_oov_baseline_uses_half():
    """A lemma absent from corpus baseline uses 0.5/n_corpus as floor (no div-by-zero)."""
    segments = [_seg(i, cs="neologizmus") for i in range(20)]
    # 'neologizmus' not in baseline at all
    baseline_df: Counter = Counter()
    n_corpus = 1_000

    with patch("ingest.sense_mining._segment_lemmas", return_value={"neologizmus"}):
        result = mine_renderings(segments, baseline_df, n_corpus)

    # lift = 1.0 / (0.5/1000) = 2000 → well above threshold
    assert len(result) == 1
    assert result[0]["cs_lemma"] == "neologizmus"


def test_mine_renderings_caps_at_max_renderings():
    """Result is capped at max_renderings."""
    segments = [_seg(i, cs="x") for i in range(20)]
    baseline_df: Counter = Counter()
    n_corpus = 10_000

    # Return 10 distinct lemmas, all with high lift
    with patch(
        "ingest.sense_mining._segment_lemmas", return_value={f"lemma{i}" for i in range(10)}
    ):
        result = mine_renderings(segments, baseline_df, n_corpus, max_renderings=3)

    assert len(result) <= 3


def test_mine_renderings_sorted_by_score():
    """Clusters are returned sorted descending by score (rate * log lift)."""
    segments = [_seg(i, cs="x") for i in range(30)]
    baseline_df: Counter = Counter({"high": 5, "medium": 50})
    n_corpus = 10_000

    # 'high': rate=1.0, corpus=0.0005 → lift=2000; 'medium': rate=0.5, corpus=0.005 → lift=100
    call_count = [0]

    def fake_lemmas(text):
        call_count[0] += 1
        idx = call_count[0] - 1
        # Alternate: even segs have 'high', odd segs have 'medium'
        if idx % 2 == 0:
            return {"high"}
        return {"medium"}

    with patch("ingest.sense_mining._segment_lemmas", side_effect=fake_lemmas):
        result = mine_renderings(segments, baseline_df, n_corpus)

    if len(result) >= 2:
        assert result[0]["score"] >= result[1]["score"]


# ── mine_english_cues ─────────────────────────────────────────────────────────


def test_mine_english_cues_removes_stopwords():
    segs = [_seg(1, en="the reason for this is that knowledge is the principle")]
    cues = mine_english_cues(segs, top_n=10)
    assert "the" not in cues
    assert "is" not in cues
    assert "reason" in cues or "knowledge" in cues or "principle" in cues


def test_mine_english_cues_top_n():
    segs = [_seg(i, en="ratio ratio ratio aspect aspect principle") for i in range(5)]
    cues = mine_english_cues(segs, top_n=2)
    assert len(cues) <= 2


def test_mine_english_cues_no_english():
    segs = [_seg(1, en=None), _seg(2, en="")]
    assert mine_english_cues(segs) == []


# ── fetch_minable_terms ───────────────────────────────────────────────────────


def test_fetch_minable_terms_passes_min_segments():
    rows = [(1, "ratio", 150), (2, "species", 80)]
    conn = _conn(rows)
    result = fetch_minable_terms(conn, min_segments=10)
    assert len(result) == 2
    assert result[0] == {"term_id": 1, "latin_lemma": "ratio", "n_segments": 150}


def test_fetch_minable_terms_sql_has_having():
    conn = _conn([])
    fetch_minable_terms(conn, min_segments=25)
    sql, params = conn.cursor.return_value.execute.call_args.args
    assert "HAVING" in sql.upper()
    assert params == (25,)


# ── write_proposed_senses ─────────────────────────────────────────────────────


def _sense(cs_lemma, context_label, en_cue="reason", sk="rozum"):
    return {
        "cs_lemma": cs_lemma,
        "context_label": context_label,
        "en_cue": en_cue,
        "sk": sk,
    }


def test_write_proposed_senses_inserts_new():
    """A sense with no existing cs_lemma or context_label gets inserted."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (42,)  # new sense_id
    conn.cursor.return_value = cur

    with patch("ingest.sense_mining.fetch_existing_senses", return_value=[]):
        n = write_proposed_senses(
            conn, term_id=1, senses=[_sense("rozum", "as reason")], src_model=9
        )

    assert n == 1
    conn.commit.assert_called_once()
    # Should have called INSERT INTO glossary_sense + 3 × sense_rendering
    assert cur.execute.call_count == 4


def test_write_proposed_senses_skips_existing_cs_lemma():
    """A sense whose cs_lemma is already present for this term is skipped."""
    existing = [
        {
            "sense_id": 5,
            "context_label": "as reason",
            "status": "approved",
            "cs_lemma": "rozum",
            "sk": "rozum",
        }
    ]
    with patch("ingest.sense_mining.fetch_existing_senses", return_value=existing):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        n = write_proposed_senses(
            conn, term_id=1, senses=[_sense("rozum", "as intellect")], src_model=9
        )

    assert n == 0
    cur.execute.assert_not_called()


def test_write_proposed_senses_skips_existing_context_label():
    """A sense whose context_label is already present is skipped (even if cs_lemma differs)."""
    existing = [
        {
            "sense_id": 5,
            "context_label": "as reason",
            "status": "proposed",
            "cs_lemma": "důvod",
            "sk": "dôvod",
        }
    ]
    with patch("ingest.sense_mining.fetch_existing_senses", return_value=existing):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur

        n = write_proposed_senses(
            conn, term_id=1, senses=[_sense("rozum", "as reason")], src_model=9
        )

    assert n == 0
    cur.execute.assert_not_called()


def test_write_proposed_senses_partial_skip():
    """Only the non-duplicate sense is written when one of two is a duplicate."""
    existing = [
        {
            "sense_id": 5,
            "context_label": "as reason",
            "status": "approved",
            "cs_lemma": "rozum",
            "sk": "rozum",
        }
    ]
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (43,)
    conn.cursor.return_value = cur

    senses = [
        _sense("rozum", "as reason"),  # duplicate cs_lemma → skip
        _sense("hľadisko", "as viewpoint"),  # new → insert
    ]
    with patch("ingest.sense_mining.fetch_existing_senses", return_value=existing):
        n = write_proposed_senses(conn, term_id=1, senses=senses, src_model=9)

    assert n == 1
    # 1 glossary_sense insert + 3 sense_rendering inserts
    assert cur.execute.call_count == 4


# ── label_term validation ─────────────────────────────────────────────────────


def test_label_term_rejects_invented_renderings():
    """Model must not label a cs_lemma outside the mined clusters."""
    clusters = [
        {
            "cs_lemma": "rozum",
            "df": 50,
            "rate": 0.5,
            "lift": 200.0,
            "score": 1.0,
            "segment_ids": [1, 2, 3],
        },
    ]
    invented_response = {
        "senses": [
            {"cs_lemma": "vôľa", "context_label": "as will", "en_cue": "will", "sk": "vôľa"},
        ]
    }
    term = {"term_id": 1, "latin_lemma": "ratio", "n_segments": 100}
    conn = _conn([])

    with (
        patch("ingest.sense_mining.fetch_cluster_contexts", return_value={}),
        patch("ingest.sense_mining.mine_english_cues", return_value=[]),
        patch("ingest.sense_mining.call_deepseek_label", return_value=invented_response),
    ):
        result = label_term(conn, term, clusters, [])

    assert result == []


def test_label_term_accepts_valid_cluster_label():
    """Model labeling a known cluster lemma is accepted."""
    clusters = [
        {
            "cs_lemma": "rozum",
            "df": 50,
            "rate": 0.5,
            "lift": 200.0,
            "score": 1.0,
            "segment_ids": [1, 2, 3],
        },
    ]
    valid_response = {
        "senses": [
            {
                "cs_lemma": "rozum",
                "context_label": "as reason or intellect",
                "en_cue": "reason",
                "sk": "rozum",
            },
        ]
    }
    term = {"term_id": 1, "latin_lemma": "ratio", "n_segments": 100}
    conn = _conn([])

    with (
        patch("ingest.sense_mining.fetch_cluster_contexts", return_value={}),
        patch("ingest.sense_mining.mine_english_cues", return_value=[]),
        patch("ingest.sense_mining.call_deepseek_label", return_value=valid_response),
    ):
        result = label_term(conn, term, clusters, [])

    assert len(result) == 1
    assert result[0]["cs_lemma"] == "rozum"


def test_label_term_drops_incomplete_sense():
    """Senses missing required fields are silently dropped."""
    clusters = [
        {
            "cs_lemma": "rozum",
            "df": 30,
            "rate": 0.3,
            "lift": 100.0,
            "score": 0.5,
            "segment_ids": [1],
        }
    ]
    incomplete = {
        "senses": [
            {"cs_lemma": "rozum", "context_label": "as reason"},  # missing en_cue and sk
        ]
    }
    term = {"term_id": 1, "latin_lemma": "ratio", "n_segments": 50}
    conn = _conn([])

    with (
        patch("ingest.sense_mining.fetch_cluster_contexts", return_value={}),
        patch("ingest.sense_mining.mine_english_cues", return_value=[]),
        patch("ingest.sense_mining.call_deepseek_label", return_value=incomplete),
    ):
        result = label_term(conn, term, clusters, [])

    assert result == []


# ── _build_label_user_turn ────────────────────────────────────────────────────


def test_build_label_user_turn_includes_lemma_and_cues():
    clusters = [
        {
            "cs_lemma": "rozum",
            "df": 50,
            "rate": 0.5,
            "lift": 200.0,
            "score": 1.0,
            "segment_ids": [],
        },
    ]
    contexts = {"rozum": ["Preto rozum riadi vôľu."]}
    turn = _build_label_user_turn("ratio", clusters, contexts, en_cues=["reason", "intellect"])
    assert "ratio" in turn
    assert "rozum" in turn
    assert "reason" in turn
    assert "Preto rozum riadi vôľu." in turn
