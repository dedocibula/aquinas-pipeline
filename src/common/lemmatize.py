"""Lemmatization utilities shared across ingest and translate pipelines.

Three functions:
  lemmatize_latin(surface) -> list[str]   — uses CLTK BackoffLatinLemmatizer
  lemmatize_czech(surface) -> list[str]   — uses MorphoDiTa
  lemmatize_slovak(surface) -> list[str]  — uses MorphoDiTa (Slovak MorfFlex)

All return a list of candidate lemmas (usually one; occasionally several for
ambiguous forms). The resolver is responsible for choosing among candidates.

Model requirements:
  Latin:  ~/cltk_data/lat/model/lat_models_cltk   (downloaded via CLTK FetchCorpus)
  Czech:  models/czech-morfflex-pdt-161115.dict    (see src/acquire/download_models.py)
  Slovak: models/slovak-morfflex-pdt-170914.dict   (see src/acquire/download_models.py)

Both MorphoDiTa functions share the same ufal.morphodita binding and the same
lemma-cleaning logic. They differ only in which model file they load.

All functions fail loudly at first call if the required model is absent.
"""

from __future__ import annotations

import functools
import pathlib
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

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


# ── MorphoDiTa shared helpers ─────────────────────────────────────────────────

_MODELS_DIR = pathlib.Path(__file__).resolve().parents[2] / "models"


def _load_morpho(glob: str, label: str):
    """Load a MorphoDiTa Morpho object from the first matching model file.

    Args:
        glob:  Glob pattern relative to _MODELS_DIR (e.g. 'czech-morfflex*.dict').
        label: Human-readable language label for error messages.

    Returns:
        A loaded ufal.morphodita.Morpho instance.

    Raises:
        RuntimeError if ufal.morphodita is not installed or the model is missing.
    """
    try:
        import ufal.morphodita as m
    except ImportError as exc:
        raise RuntimeError(
            "ufal.morphodita not installed. Run: uv add ufal.morphodita"
        ) from exc

    dicts = sorted(_MODELS_DIR.rglob(glob))
    if not dicts:
        raise RuntimeError(
            f"{label} MorphoDiTa model not found in {_MODELS_DIR}. "
            "Download it:\n"
            "  uv run python -m acquire.download_models\n"
            f"Expected file matching: models/{glob}"
        )
    model_path = str(dicts[-1])  # use newest if multiple versions present
    morpho = m.Morpho.load(model_path)
    if morpho is None:
        raise RuntimeError(f"MorphoDiTa failed to load {label} model from {model_path}")
    return morpho


def _extract_lemmas(morpho, surface: str) -> list[str]:
    """Run MorphoDiTa analysis and return cleaned lemmas for one surface form.

    MorphoDiTa appends sense disambiguation suffixes to lemmas (e.g. `_:B_`,
    `-1`). This function strips them, deduplicates, and falls back to the
    surface form if no analysis is found. The logic is identical for Czech and
    Slovak since both use the same MorfFlex/PDT tag conventions.
    """
    import ufal.morphodita as m

    analyses = m.TaggedLemmas()
    morpho.analyze(surface, morpho.GUESSER, analyses)

    lemmas: list[str] = []
    for analysis in analyses:
        raw = analysis.lemma
        # Strip sense suffixes: split on '_' then strip trailing digits/hyphens.
        lemma = raw.split("_")[0].rstrip("-0123456789")
        if lemma and lemma not in lemmas:
            lemmas.append(lemma)

    return lemmas if lemmas else [surface]


# ── Czech ─────────────────────────────────────────────────────────────────────

_CZECH_DICT_GLOB = "czech-morfflex*.dict"


@functools.lru_cache(maxsize=1)
def _czech_morpho():
    return _load_morpho(_CZECH_DICT_GLOB, "Czech")


def lemmatize_czech(surface: str) -> list[str]:
    """Return candidate lemmas for a Czech surface form.

    Examples:
        lemmatize_czech('dychtění')  -> ['dychtění']
        lemmatize_czech('dychtěním') -> ['dychtění']
    """
    return _extract_lemmas(_czech_morpho(), surface)


# ── Slovak ────────────────────────────────────────────────────────────────────

_SLOVAK_DICT_GLOB = "slovak-morfflex*.dict"


@functools.lru_cache(maxsize=1)
def _slovak_morpho():
    # NOTE: lru_cache is process-local. If you add concurrent workers via
    # ProcessPoolExecutor, pre-warm this at worker init to avoid per-worker
    # disk I/O on first translation pass.
    return _load_morpho(_SLOVAK_DICT_GLOB, "Slovak")


def lemmatize_slovak(surface: str) -> list[str]:
    """Return candidate lemmas for a Slovak surface form.

    Uses the MorfFlex SK 170914 dictionary (96.3% lemma accuracy on PDT).
    Download: http://hdl.handle.net/11234/1-3278

    Examples:
        lemmatize_slovak('vierou')   -> ['viera']
        lemmatize_slovak('rozumu')   -> ['rozum']
        lemmatize_slovak('poznaniu') -> ['poznanie']
        lemmatize_slovak('múdrosťou') -> ['múdrosť']
    """
    return _extract_lemmas(_slovak_morpho(), surface)


@functools.lru_cache(maxsize=4096)
def generate_slovak_forms(lemma: str) -> frozenset[str]:
    """Return every inflected surface form for a Slovak lemma, lowercased.

    Uses MorphoDiTa *generation* — the closed-vocabulary direction, reliable
    for any lemma present in the MorfFlex SK dictionary. This is the inverse
    of lemmatize_slovak: instead of analysing arbitrary text (open vocabulary,
    where the dictionary has gaps), it enumerates the finite form set of a
    known lemma.

    Returns an empty frozenset for out-of-vocabulary lemmas (e.g. archaic
    'čnosť' — the dictionary only has modern 'cnosť' — or the Latin loan
    'habitus'). Callers must apply their own OOV fallback.

    Examples:
        generate_slovak_forms('rozum') -> frozenset({'rozum', 'rozumu', ...})
        generate_slovak_forms('čnosť') -> frozenset()
    """
    import ufal.morphodita as m

    morpho = _slovak_morpho()
    lemmas_forms = m.TaggedLemmasForms()
    # Returns -1 for OOV lemmas, leaving lemmas_forms empty — no need to branch.
    morpho.generate(lemma, "", morpho.GUESSER, lemmas_forms)
    return frozenset(
        form.form.lower() for entry in lemmas_forms for form in entry.forms
    )


# ── Slovak term matching ──────────────────────────────────────────────────────

def normalise(s: str) -> str:
    """Lowercase and strip diacritics for loose containment comparison."""
    s = s.lower()
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()


@dataclass(frozen=True)
class SlovakTermMatcher:
    """Decide whether a Slovak lemma occurs (in any inflected form) in a draft.

    Wraps two strategies behind one object:

    * MorphoDiTa *generation* (:meth:`forms`) — the reliable closed-vocabulary
      form set for a lemma present in the MorfFlex SK dictionary.
    * a normalised stem-prefix fallback (:meth:`stem`) for OOV lemmas (archaic
      'čnosť', Latin loan 'habitus') and MorfFlex coverage gaps (e.g. 'pamäť'
      generates only {'pamäti'}, missing 'pamäťou'/'pamätiam').

    ``generate`` is injectable so the matching policy can be unit-tested without
    loading the MorphoDiTa model.
    """

    generate: Callable[[str], frozenset[str]] = generate_slovak_forms

    def forms(self, lemma: str) -> frozenset[str]:
        """Inflected surface forms for ``lemma`` (lowercased); empty if OOV."""
        return self.generate(lemma.lower())

    @staticmethod
    def stem(word: str) -> str:
        """Derive a normalised stem prefix for a lemma MorphoDiTa cannot generate.

        Latin loans decline without their -us ending (habitus → habitu/habitom),
        so it is stripped. Otherwise trailing vowels are inflectional endings.
        Stems shorter than 3 characters are too prefix-happy; keep the full word.
        """
        w = normalise(word)
        if w.endswith("us") and len(w) >= 5:
            # Latin -us loans: habitus → habit (habitu-, habitom-)
            stem = w[:-2]
        elif w.endswith("en") and len(w) >= 5:
            # Slovak ň-stem nouns: vášeň → vasen → vas (vášne, vášni, vášňou)
            # Oblique forms drop the 'en' entirely before the inflectional suffix,
            # so the 5-char stem 'vasen' never prefix-matches 'vasne'.
            stem = w[:-2]
        else:
            stem = w.rstrip("aeiouy")
        return stem if len(stem) >= 3 else w

    def matches(
        self, word: str, draft_tokens: set[str], draft_tokens_norm: set[str]
    ) -> bool:
        """True if any inflected form of ``word`` appears among the draft's tokens.

        ``draft_tokens`` are the lowercased draft tokens; ``draft_tokens_norm``
        are those tokens with diacritics stripped (for the stem-prefix fallback).
        """
        w = word.lower()
        if w in draft_tokens:
            return True
        forms = self.forms(w)
        if not forms:
            print(
                f"[PRECHECK] OOV: no MorphoDiTa forms for '{word}' — stem fallback only",
                file=sys.stderr,
            )
        if forms and forms & draft_tokens:
            return True
        # Stem-prefix fallback — covers both OOV lemmas ('čnosť', 'habitus') AND
        # MorfFlex coverage gaps. Always applied as a second-chance check.
        stem = self.stem(w)
        matched = next((t for t in draft_tokens_norm if t.startswith(stem)), None)
        if matched is not None:
            print(
                f"[PRECHECK] stem-fallback: '{word}' stem='{stem}' matched='{matched}'",
                file=sys.stderr,
            )
        return matched is not None
