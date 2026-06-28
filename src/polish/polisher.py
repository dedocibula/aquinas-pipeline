"""Polish a translated segment using Claude Sonnet 4.6.

polish_segment mirrors the translate_segment contract:
  (status, [UsageInfo], PolishOutcome)

Status values:
  'polished'  — (sk, polish) written to DB
  'skipped'   — a (sk, human) row exists; human is authoritative; nothing written
  'no_source' — no (sk, model) draft found; nothing to polish
  'error'     — AnthropicClient raised; nothing written

The polisher operates entirely on Slovak text (the model draft + required_slovak
constraint terms).  It does not need the Latin source or CLTK lemmatisation:
surface-form expansion is only useful when the model translates FROM Latin and
needs to match inflected surface forms.  Here the model rewrites existing Slovak,
so lemma-form constraints are correct and unambiguous.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field

from dotenv import load_dotenv

from common.anthropic_client import AnthropicClient
from common.pricing import UsageInfo
from common.prompt_blocks import build_hard_constraints_block
from polish.guards import run_guards
from storage.db import source_id
from storage.repositories import GlossaryRepository, SegmentRepository

load_dotenv()

log = logging.getLogger(__name__)

_SYSTEM_PROMPT: str | None = None
_PROMPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "prompts"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048


def _load_system() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = (_PROMPTS_DIR / "polish_system.txt").read_text(encoding="utf-8")
    return _SYSTEM_PROMPT


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


def _get_sk_text(conn, segment_id: int, src_code: str) -> str | None:
    """Return segment_text(sk) for the given source code, or None if absent."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT st.content
            FROM segment_text st
            JOIN source s ON s.source_id = st.source_id
            WHERE st.segment_id = %s AND st.lang = 'sk' AND s.code = %s
            LIMIT 1
            """,
            (segment_id, src_code),
        )
        row = cur.fetchone()
    return row[0] if row else None


def polish_segment(
    segment_id: int,
    conn,
    *,
    _client: AnthropicClient | None = None,
) -> tuple[str, list[UsageInfo], PolishOutcome]:
    """Polish one translated segment and write (sk, polish) to DB.

    _client is the test seam: inject a fake AnthropicClient to avoid live API
    calls in unit tests.  In production, a new AnthropicClient(MODEL) is created.

    Always commits before returning 'polished'.  Other statuses commit nothing.
    """
    outcome = PolishOutcome(segment_id=segment_id)

    # Human text is authoritative — never overwrite with a machine polish.
    if _get_sk_text(conn, segment_id, "human") is not None:
        log.info("segment_id=%d: (sk,human) exists; skipping polish", segment_id)
        return "skipped", [], outcome

    model_text = _get_sk_text(conn, segment_id, "model")
    if not model_text:
        log.warning("segment_id=%d: no (sk,model) draft; cannot polish", segment_id)
        return "no_source", [], outcome

    locked_terms = GlossaryRepository(conn).locked_terms(segment_id)
    # Use lemma-form constraints (not surface-expanded): the polisher rewrites
    # existing Slovak, so the model needs required_slovak terms, not Latin
    # surface forms.  Surface expansion would create duplicate constraint entries
    # (one per inflected Latin form) with no benefit.
    constraints = [c.to_prompt_dict() for c in locked_terms]

    constraints_block = build_hard_constraints_block(constraints)
    user_content = (
        f"<source_draft>\n{model_text}\n</source_draft>\n\n"
        f"{constraints_block}\n\n"
        "Polish the Slovak draft above. Output only the polished Slovak text."
    )

    client = _client if _client is not None else AnthropicClient(MODEL)
    try:
        result = client.chat(
            [{"role": "user", "content": user_content}],
            max_tokens=MAX_TOKENS,
            system=_load_system(),
        )
    except Exception as exc:
        log.error("segment_id=%d: Anthropic error: %s", segment_id, exc)
        return "error", [], outcome

    polished = result.content.strip()

    flags = run_guards(model_text, polished, constraints)
    outcome.guard_flags = flags

    seg_repo = SegmentRepository(conn)
    src_polish_id = source_id(conn, "polish")
    seg_repo.write_segment_text(segment_id, "sk", src_polish_id, polished)
    conn.commit()
    # Set polished_text only after the DB write commits — a pre-commit assignment
    # would make parse_polish_jsonl include ghost records for failed writes.
    outcome.polished_text = polished

    log.info(
        "segment_id=%d: polished ok=%s ratio=%.2f",
        segment_id,
        flags["ok"],
        flags["length_ratio"],
    )
    return "polished", [result.usage], outcome
