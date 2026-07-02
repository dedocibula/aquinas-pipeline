"""Polish translated segments using a swappable backend (DeepSeek or Anthropic).

polish_segment mirrors the translate_segment contract:
  (status, [UsageInfo], PolishOutcome)

Status values:
  'polished'  — (sk, polish) written to DB
  'skipped'   — a (sk, human) row exists; human is authoritative; nothing written
  'no_source' — no (sk, model) draft found; nothing to polish
  'error'     — polisher raised; nothing written

The polisher operates entirely on Slovak text (the model draft + required_slovak
constraint terms).  It does not need the Latin source or CLTK lemmatisation:
surface-form expansion is only useful when the model translates FROM Latin and
needs to match inflected surface forms.  Here the model rewrites existing Slovak,
so lemma-form constraints are correct and unambiguous.
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from dotenv import load_dotenv

from common.anthropic_client import AnthropicClient
from common.deepseek_client import ChatResult, DeepSeekClient
from common.pricing import UsageInfo
from common.prompt_blocks import build_polish_user_content
from polish.guards import run_guards
from storage.db import get_conn, source_id
from storage.repositories import GlossaryRepository, SegmentRepository

load_dotenv()

log = logging.getLogger(__name__)

_SYSTEM_PROMPT: str | None = None
_PROMPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "prompts"

DEEPSEEK_MODEL  = os.getenv("POLISH_DEEPSEEK_MODEL", "deepseek-v4-flash")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2048


def _load_system() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = (_PROMPTS_DIR / "polish_system.txt").read_text(encoding="utf-8")
    return _SYSTEM_PROMPT


# ── Polisher backends ──────────────────────────────────────────────────────────


class PolisherBase(ABC):
    """Abstract polish backend. Implementations wrap a specific API client."""

    @abstractmethod
    def polish(self, user_content: str, system: str) -> ChatResult: ...


class DeepSeekPolisher(PolisherBase):
    def __init__(self) -> None:
        self._client = DeepSeekClient(DEEPSEEK_MODEL, timeout=60)

    def polish(self, user_content: str, system: str) -> ChatResult:
        return self._client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
            temperature=0.3,
            max_tokens=MAX_TOKENS,
            thinking={"type": "disabled"},
        )


class AnthropicPolisher(PolisherBase):
    def __init__(self) -> None:
        self._client = AnthropicClient(ANTHROPIC_MODEL)

    def polish(self, user_content: str, system: str) -> ChatResult:
        return self._client.chat(
            [{"role": "user", "content": user_content}],
            max_tokens=MAX_TOKENS,
            system=system,
        )


_DEFAULT_POLISHER: PolisherBase = DeepSeekPolisher()


def make_polisher(backend: str = "deepseek") -> PolisherBase:
    """Return a fresh polisher instance for the given backend name.

    Always returns a new instance — callers that use a thread pool (run_polish)
    must not share a single polisher across threads, because the underlying HTTP
    client may not be thread-safe.  _DEFAULT_POLISHER is kept only as the
    single-threaded pilot default for polish_segment.
    """
    if backend == "anthropic":
        return AnthropicPolisher()
    return DeepSeekPolisher()


# ── Per-segment result ─────────────────────────────────────────────────────────


@dataclass
class PolishOutcome:
    """Per-segment analytics record for a polish pass.

    guard_flags mirrors the dict returned by run_guards(); recorded in pilot JSONL
    and reports for per-element-type guard pass-rate analysis.
    polished_text is the actual polished SK content, stored in the JSONL so that
    cross-run polish comparisons (polish_compare.py) can show prior vs current text
    even after reset_golden deletes segment_text(sk,polish) rows between epochs.
    """

    segment_id: int
    guard_flags: dict = field(default_factory=dict)
    polished_text: str | None = None


# ── Single-segment polisher (pilot + inline use) ───────────────────────────────


def polish_segment(
    segment_id: int,
    conn,
    *,
    _client: PolisherBase | None = None,
    _autocommit: bool = True,
) -> tuple[str, list[UsageInfo], PolishOutcome]:
    """Polish one translated segment and write (sk, polish) to DB.

    _client is the test seam: inject a PolisherBase subclass to avoid live API
    calls in unit tests.  In production, the module-level DeepSeekPolisher is used.

    _autocommit controls whether this function commits the (sk,polish) write itself.
    Pass False when the caller needs to bundle this write with additional changes
    (e.g. a status flip) in a single atomic commit — the caller must then commit.
    When True (default, pilot path), commits before returning 'polished'.
    Other statuses commit nothing regardless of _autocommit.

    Guard policy: writes (sk,polish) regardless of guard flags — the caller (pilot)
    reads PolishOutcome.guard_flags to decide what to do. For bulk runs use
    run_polish() which enforces guards before writing.
    """
    outcome = PolishOutcome(segment_id=segment_id)
    seg_repo = SegmentRepository(conn)

    # Human text is authoritative — never overwrite with a machine polish.
    if seg_repo.get_sk_text(segment_id, "human") is not None:
        log.info("segment_id=%d: (sk,human) exists; skipping polish", segment_id)
        return "skipped", [], outcome

    model_text = seg_repo.get_sk_text(segment_id, "model")
    if not model_text:
        log.warning("segment_id=%d: no (sk,model) draft; cannot polish", segment_id)
        return "no_source", [], outcome

    locked_terms = GlossaryRepository(conn).locked_terms(segment_id)
    constraints = [c.to_prompt_dict() for c in locked_terms]
    reviewer_notes = seg_repo.get_reviewer_notes_text(segment_id)

    user_content = build_polish_user_content(model_text, constraints, reviewer_notes)
    polisher = _client if _client is not None else _DEFAULT_POLISHER

    try:
        result = polisher.polish(user_content, _load_system())
    except Exception as exc:
        log.error("segment_id=%d: polisher error: %s", segment_id, exc)
        return "error", [], outcome

    polished = result.content.strip()

    flags = run_guards(model_text, polished, constraints)
    outcome.guard_flags = flags

    src_polish_id = source_id(conn, "polish")
    SegmentRepository(conn).write_segment_text(segment_id, "sk", src_polish_id, polished)
    if _autocommit:
        conn.commit()
    outcome.polished_text = polished

    log.info(
        "segment_id=%d: polished ok=%s ratio=%.2f",
        segment_id,
        flags["ok"],
        flags["length_ratio"],
    )
    return "polished", [result.usage], outcome


# ── Bulk synchronous runner ────────────────────────────────────────────────────


@dataclass
class _PolishStats:
    total: int = 0
    polished: int = 0
    guard_failed: int = 0
    errored: int = 0
    no_source: int = 0
    cost_usd: float = 0.0


def run_polish(
    *,
    limit: int | None = None,
    element_types: list[str] | None = None,
    segment_ids: list[int] | None = None,
    max_workers: int = 10,
    backend: str = "deepseek",
) -> _PolishStats:
    """Polish all eligible segments synchronously using a thread pool.

    Fetches candidates via SegmentRepository.get_polish_candidates() (translated,
    no human/polish row). Each worker opens its own DB connection and commits
    immediately on success — a crash loses at most one in-flight segment.

    Guard policy: skips the DB write if run_guards fails (unlike polish_segment
    which always writes and lets the caller decide).

    segment_ids: when provided, only consider those specific segments (used by
    translate.run to limit polish to segments translated in the current run).
    """
    with get_conn() as conn:
        candidates = SegmentRepository(conn).get_polish_candidates(
            element_types=element_types, limit=limit, segment_ids=segment_ids
        )

    if not candidates:
        log.info("run_polish: no candidates")
        return _PolishStats()

    log.info("run_polish: %d candidates (backend=%s workers=%d)", len(candidates), backend, max_workers)
    polisher = make_polisher(backend)
    system_text = _load_system()
    stats = _PolishStats()
    lock = threading.Lock()

    def _process(seg_id: int) -> None:
        with get_conn() as conn:
            seg_repo = SegmentRepository(conn)
            model_text = seg_repo.get_sk_text(seg_id, "model")
            if not model_text:
                with lock:
                    stats.total += 1
                    stats.no_source += 1
                return

            locked_terms = GlossaryRepository(conn).locked_terms(seg_id)
            constraints = [c.to_prompt_dict() for c in locked_terms]
            reviewer_notes = seg_repo.get_reviewer_notes_text(seg_id)
            user_content = build_polish_user_content(model_text, constraints, reviewer_notes)

        try:
            result = polisher.polish(user_content, system_text)
        except Exception as exc:
            log.error("segment_id=%d: polisher error: %s", seg_id, exc)
            with lock:
                stats.total += 1
                stats.errored += 1
            return

        polished = result.content.strip()
        flags = run_guards(model_text, polished, constraints)

        with lock:
            stats.total += 1
            stats.cost_usd += result.usage.cost_usd

        if not flags["ok"]:
            log.warning(
                "segment_id=%d: guard failed sentence_delta=%s term_ok=%s ratio=%.3f; skipping",
                seg_id, flags["sentence_delta"], flags["term_retention_ok"], flags["length_ratio"],
            )
            with lock:
                stats.guard_failed += 1
            return

        with get_conn() as conn:
            src_polish_id = source_id(conn, "polish")
            SegmentRepository(conn).write_segment_text(seg_id, "sk", src_polish_id, polished)
            conn.commit()

        log.info("segment_id=%d: polished", seg_id)
        with lock:
            stats.polished += 1

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_process, sid): sid for sid in candidates}
        for fut in as_completed(futures):
            fut.result()

    log.info(
        "run_polish complete: polished=%d guard_failed=%d errored=%d no_source=%d cost=$%.4f",
        stats.polished, stats.guard_failed, stats.errored, stats.no_source, stats.cost_usd,
    )
    return stats


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Synchronous corpus polish (DeepSeek or Anthropic)")
    parser.add_argument("--limit", type=int, default=None, help="Cap number of segments")
    parser.add_argument("--element-types", nargs="*", default=None,
                        help="Filter by element_type (e.g. respondeo arg)")
    parser.add_argument("--workers", type=int, default=int(os.getenv("POLISH_WORKERS", "10")))
    parser.add_argument("--backend", choices=["deepseek", "anthropic"], default="deepseek")
    args = parser.parse_args()
    stats = run_polish(
        limit=args.limit,
        element_types=args.element_types,
        max_workers=args.workers,
        backend=args.backend,
    )
    print(
        f"Done. polished={stats.polished} guard_failed={stats.guard_failed} "
        f"errored={stats.errored} no_source={stats.no_source} cost=~${stats.cost_usd:.4f}"
    )


if __name__ == "__main__":
    main()
