"""Lemmatization utilities shared across ingest and translate pipelines.

Two functions:
  lemmatize_latin(surface) -> list[str]   — uses CLTK BackoffLatinLemmatizer
  lemmatize_czech(surface) -> list[str]   — uses MorphoDiTa

Both return a list of candidate lemmas (usually one; occasionally several for
ambiguous forms). The resolver is responsible for choosing among candidates.

Model requirements:
  Latin: ~/cltk_data/lat/model/lat_models_cltk   (downloaded via CLTK FetchCorpus)
  Czech: models/czech-morfflex-pdt-161115.dict    (see scripts/download_models.py)

Both functions fail loudly at first call if the required model is absent.
"""

from __future__ import annotations

import functools
import pathlib

# ── Latin ─────────────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _latin_pos_tagger():
    from cltk.tag.pos import POSTag
    return POSTag("lat")


def pos_tag_latin(text: str) -> list[tuple[str, str]]:
    """Return (surface, pos_char) pairs for Latin text.

    pos_char is the first character of the CLTK ngram tag:
      N=noun, V=verb, A=adjective, P=pronoun, D=adverb,
      R=preposition, C=conjunction, M=numeral, I=interjection
      '?'=unknown/not recognized by the tagger
    """
    tagger = _latin_pos_tagger()
    tagged = tagger.tag_ngram_123_backoff(text) or []
    return [(word, tag[0] if tag else "?") for word, tag in tagged]


@functools.lru_cache(maxsize=1)
def _latin_lemmatizer():
    try:
        from cltk.lemmatize.lat import LatinBackoffLemmatizer
        return LatinBackoffLemmatizer()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Latin lemmatizer model not found. Run:\n"
            "  uv run python -c \""
            "from cltk.data.fetch import FetchCorpus; "
            "FetchCorpus('lat').import_corpus('lat_models_cltk')"
            "\""
        ) from exc


def lemmatize_latin(surface: str) -> list[str]:
    """Return candidate lemmas for a Latin surface form.

    Examples:
        lemmatize_latin('essentiam') -> ['essentia']
        lemmatize_latin('hominem')   -> ['homo']
    """
    lemmatizer = _latin_lemmatizer()
    pairs = lemmatizer.lemmatize([surface])
    # lemmatize returns [(surface, lemma), ...]; collapse to unique lemmas
    lemmas = [lemma for _, lemma in pairs if lemma]
    return list(dict.fromkeys(lemmas)) or [surface]


# ── Czech ─────────────────────────────────────────────────────────────────────

_MODELS_DIR = pathlib.Path(__file__).resolve().parents[2] / "models"
_CZECH_DICT_GLOB = "czech-morfflex*.dict"


@functools.lru_cache(maxsize=1)
def _czech_morpho():
    try:
        import ufal.morphodita as m
    except ImportError as exc:
        raise RuntimeError(
            "ufal.morphodita not installed. Run: uv add ufal.morphodita"
        ) from exc

    dicts = sorted(_MODELS_DIR.rglob(_CZECH_DICT_GLOB))
    if not dicts:
        raise RuntimeError(
            f"Czech MorphoDiTa model not found in {_MODELS_DIR}. "
            "Download it:\n"
            "  uv run python scripts/download_models.py\n"
            "Expected file matching: models/czech-morfflex*.dict"
        )
    model_path = str(dicts[-1])  # use newest if multiple
    morpho = m.Morpho.load(model_path)
    if morpho is None:
        raise RuntimeError(f"MorphoDiTa failed to load model from {model_path}")
    return morpho


def lemmatize_czech(surface: str) -> list[str]:
    """Return candidate lemmas for a Czech surface form.

    Examples:
        lemmatize_czech('dychtění')  -> ['dychtění']
        lemmatize_czech('dychtěním') -> ['dychtění']
    """
    import ufal.morphodita as m

    morpho = _czech_morpho()
    analyses = m.TaggedLemmas()
    morpho.analyze(surface, morpho.GUESSER, analyses)

    lemmas: list[str] = []
    for analysis in analyses:
        raw = analysis.lemma
        # MorphoDiTa appends sense disambiguation suffixes like `_:B_` or `-1`.
        # Strip everything after the first non-alphabetic, non-hyphen character.
        lemma = raw.split("_")[0].rstrip("-0123456789")
        if lemma and lemma not in lemmas:
            lemmas.append(lemma)

    return lemmas if lemmas else [surface]
