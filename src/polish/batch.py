"""Production batch polish pass via the Anthropic Batch API (50% discount).

Usage:
    uv run python -m polish.batch [--limit N] [--element-types TYPE [TYPE ...]]

Selects translated, non-(sk,human) segments → chunks into Batch API requests
(custom_id = segment_id) → polls until ended → processes results keyed by
custom_id → runs guards → writes (sk,polish) for guard-passing segments;
guard-failures are logged but left unpolished → emits reports/m5_polish_production.txt.

Write rule (from claude-corrections.md): each result is committed immediately
on success — never buffered in a single end-of-run commit.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import load_dotenv

from common.prompt_blocks import build_hard_constraints_block
from polish.guards import run_guards
from storage.db import get_conn, source_id
from storage.repositories import GlossaryRepository, SegmentRepository

load_dotenv()

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048
BATCH_SIZE = 10_000  # well under the 100,000 limit per batch
POLL_INTERVAL = 60  # seconds

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
_REPORTS_DIR  = _PROJECT_ROOT / "reports"
_PROMPTS_DIR  = _PROJECT_ROOT / "prompts"

_SYSTEM_PROMPT: str | None = None


def _load_system() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = (_PROMPTS_DIR / "polish_system.txt").read_text(encoding="utf-8")
    return _SYSTEM_PROMPT


# ── segment selection ─────────────────────────────────────────────────────────


def fetch_batch_candidates(
    conn,
    *,
    element_types: list[str] | None = None,
    limit: int | None = None,
) -> list[int]:
    """Return segment_ids eligible for batch polishing.

    Eligible = translation_status='translated' AND no (sk,human) row AND no
    existing (sk,polish) row (skip-already-done rule).  Optionally filtered by
    element_type and capped by limit.
    """
    type_clause = ""
    params: list = []
    if element_types:
        placeholders = ", ".join(["%s"] * len(element_types))
        type_clause = f"AND seg.element_type IN ({placeholders})"
        params.extend(element_types)

    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT %s"
        params.append(limit)

    sql = f"""
        SELECT seg.segment_id
        FROM segment seg
        WHERE seg.translation_status = 'translated'
          AND NOT EXISTS (
              SELECT 1 FROM segment_text st
              JOIN source s ON s.source_id = st.source_id
              WHERE st.segment_id = seg.segment_id
                AND st.lang = 'sk'
                AND s.code = 'human'
          )
          AND NOT EXISTS (
              SELECT 1 FROM segment_text st
              JOIN source s ON s.source_id = st.source_id
              WHERE st.segment_id = seg.segment_id
                AND st.lang = 'sk'
                AND s.code = 'polish'
          )
          {type_clause}
        ORDER BY seg.segment_id
        {limit_clause}
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [row[0] for row in cur.fetchall()]


# ── request building ──────────────────────────────────────────────────────────


@dataclass
class _SegmentPayload:
    segment_id: int
    model_text: str
    constraints: list[dict] = field(default_factory=list)


def _build_payload(conn, segment_id: int) -> _SegmentPayload | None:
    """Load (sk,model) text and constraints for one segment."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT st.content
            FROM segment_text st
            JOIN source s ON s.source_id = st.source_id
            WHERE st.segment_id = %s AND st.lang = 'sk' AND s.code = 'model'
            LIMIT 1
            """,
            (segment_id,),
        )
        row = cur.fetchone()
    if not row:
        log.warning("segment_id=%d: no (sk,model) text; skipping from batch", segment_id)
        return None

    constraints = [c.to_prompt_dict() for c in GlossaryRepository(conn).locked_terms(segment_id)]
    return _SegmentPayload(segment_id=segment_id, model_text=row[0], constraints=constraints)


def _build_request(payload: _SegmentPayload, system_text: str) -> Request:
    constraints_block = build_hard_constraints_block(payload.constraints)
    user_content = (
        f"<source_draft>\n{payload.model_text}\n</source_draft>\n\n"
        f"{constraints_block}\n\n"
        "Polish the Slovak draft above. Output only the polished Slovak text."
    )
    return Request(
        custom_id=str(payload.segment_id),
        params=MessageCreateParamsNonStreaming(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
        ),
    )


# ── batch lifecycle ───────────────────────────────────────────────────────────


def _submit_batch(client: anthropic.Anthropic, requests: list[Request]) -> str:
    batch = client.messages.batches.create(requests=requests)
    log.info("batch submitted: id=%s requests=%d", batch.id, len(requests))
    return batch.id


def _poll_batch(client: anthropic.Anthropic, batch_id: str) -> None:
    """Block until the batch's processing_status is 'ended'."""
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        log.info("batch %s status=%s", batch_id, batch.processing_status)
        if batch.processing_status == "ended":
            counts = getattr(batch, "request_counts", None)
            if counts:
                log.info(
                    "batch %s ended: succeeded=%s errored=%s canceled=%s expired=%s",
                    batch_id,
                    getattr(counts, "succeeded", "?"),
                    getattr(counts, "errored", "?"),
                    getattr(counts, "canceled", "?"),
                    getattr(counts, "expired", "?"),
                )
            return
        time.sleep(POLL_INTERVAL)


# ── result processing ─────────────────────────────────────────────────────────


@dataclass
class _BatchStats:
    total: int = 0
    polished: int = 0
    guard_failed: int = 0
    errored: int = 0
    no_source: int = 0
    cost_usd: float = 0.0


def _process_results(
    client: anthropic.Anthropic,
    batch_id: str,
    payloads: dict[int, _SegmentPayload],
    conn,
    src_polish_id: int,
    stats: _BatchStats,
) -> None:
    """Iterate results keyed by custom_id; write guard-passing segments immediately."""
    seg_repo = SegmentRepository(conn)

    for result in client.messages.batches.results(batch_id):
        seg_id = int(result.custom_id)
        stats.total += 1

        if result.result.type == "errored":
            log.error("batch result: segment_id=%d errored: %s", seg_id, result.result.error)
            stats.errored += 1
            continue

        if result.result.type in ("canceled", "expired"):
            log.warning("batch result: segment_id=%d %s", seg_id, result.result.type)
            stats.errored += 1
            continue

        # succeeded
        msg = result.result.message
        polished = next((b.text for b in msg.content if b.type == "text"), "").strip()
        if not polished:
            log.error("batch result: segment_id=%d succeeded but empty content", seg_id)
            stats.errored += 1
            continue

        payload = payloads.get(seg_id)
        if payload is None:
            log.error("batch result: segment_id=%d not in payloads map", seg_id)
            stats.errored += 1
            continue

        # accumulate cost — batch = 50% off standard rates
        # Three separate billing buckets: input, cache-write, cache-read
        usage = msg.usage
        input_tokens  = usage.input_tokens or 0
        cache_write   = usage.cache_creation_input_tokens or 0
        output_tokens = usage.output_tokens or 0
        cache_read    = usage.cache_read_input_tokens or 0
        # Batch pricing per 1M: input $1.50, cache-write $1.875, output $7.50, cache-read $0.15
        stats.cost_usd += (
            input_tokens  * 1.500 / 1_000_000
            + cache_write * 1.875 / 1_000_000
            + output_tokens * 7.50 / 1_000_000
            + cache_read  * 0.150 / 1_000_000
        )

        flags = run_guards(payload.model_text, polished, payload.constraints)
        if not flags["ok"]:
            log.warning(
                "segment_id=%d: guard failed — sentence_delta=%s term_ok=%s "
                "particle_ok=%s ratio=%.3f; leaving unpolished",
                seg_id,
                flags["sentence_delta"],
                flags["term_retention_ok"],
                flags["particle_retention_ok"],
                flags["length_ratio"],
            )
            stats.guard_failed += 1
            continue

        try:
            seg_repo.write_segment_text(seg_id, "sk", src_polish_id, polished)
            conn.commit()
        except Exception as exc:
            log.error("segment_id=%d: DB write failed, skipping: %s", seg_id, exc)
            stats.errored += 1
            try:
                conn.rollback()
            except Exception:
                pass
            continue

        stats.polished += 1
        log.info("segment_id=%d: polished and committed", seg_id)


# ── report ────────────────────────────────────────────────────────────────────


def _write_report(stats: _BatchStats, elapsed_s: float, num_batches: int) -> None:
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _REPORTS_DIR / "m5_polish_production.txt"
    hours, rem = divmod(int(elapsed_s), 3600)
    mins, secs = divmod(rem, 60)
    guard_rate = (stats.polished / stats.total * 100) if stats.total else 0.0
    lines = [
        "M5 POLISH PRODUCTION BATCH SUMMARY",
        f"  Generated:         {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"  Batches submitted: {num_batches}",
        "",
        f"  Total results:     {stats.total}",
        f"  Polished (written):{stats.polished}  ({guard_rate:.1f}%)",
        f"  Guard-failed:      {stats.guard_failed}  (left unpolished)",
        f"  Errored/canceled:  {stats.errored}",
        f"  No source:         {stats.no_source}",
        "",
        f"  Est. API cost:     ~${stats.cost_usd:.4f}",
        f"  Wall time:         {hours}h {mins}m {secs}s",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("report written to %s", path)


# ── main entry point ──────────────────────────────────────────────────────────


def run_batch(
    *,
    element_types: list[str] | None = None,
    limit: int | None = None,
    _client: anthropic.Anthropic | None = None,
) -> _BatchStats:
    """Run the production batch polish pass.

    _client is the test seam — inject a fake anthropic.Anthropic() in tests.
    """
    start = time.monotonic()
    client = _client if _client is not None else anthropic.Anthropic()
    system_text = _load_system()

    with get_conn() as conn:
        segment_ids = fetch_batch_candidates(
            conn, element_types=element_types, limit=limit
        )

    if not segment_ids:
        log.info("no candidates for batch polish; nothing to do")
        stats = _BatchStats()
        _write_report(stats, 0.0, 0)
        return stats

    log.info("batch polish: %d candidate segments", len(segment_ids))

    # Build payloads (reads (sk,model) + constraints per segment)
    payloads: dict[int, _SegmentPayload] = {}
    with get_conn() as conn:
        for seg_id in segment_ids:
            p = _build_payload(conn, seg_id)
            if p is None:
                continue
            payloads[seg_id] = p

    if not payloads:
        log.warning("no payloads could be built; aborting")
        stats = _BatchStats(no_source=len(segment_ids))
        _write_report(stats, time.monotonic() - start, 0)
        return stats

    stats = _BatchStats(no_source=len(segment_ids) - len(payloads))

    # Chunk → submit → poll → process (one connection per batch result set)
    payload_list = list(payloads.values())
    chunks = [payload_list[i:i + BATCH_SIZE] for i in range(0, len(payload_list), BATCH_SIZE)]
    num_batches = len(chunks)

    for chunk_idx, chunk in enumerate(chunks, 1):
        log.info("chunk %d/%d: building %d requests", chunk_idx, num_batches, len(chunk))
        requests = [_build_request(p, system_text) for p in chunk]
        batch_id = _submit_batch(client, requests)
        _poll_batch(client, batch_id)

        chunk_payloads = {p.segment_id: p for p in chunk}
        with get_conn() as conn:
            src_polish_id = source_id(conn, "polish")
            _process_results(client, batch_id, chunk_payloads, conn, src_polish_id, stats)

    elapsed = time.monotonic() - start
    _write_report(stats, elapsed, num_batches)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Batch polish pass via Anthropic Batch API")
    parser.add_argument("--limit", type=int, default=None, help="Cap number of segments")
    parser.add_argument(
        "--element-types",
        nargs="*",
        default=None,
        help="Filter by element_type (e.g. body response objection)",
    )
    args = parser.parse_args()
    stats = run_batch(element_types=args.element_types, limit=args.limit)
    print(
        f"Done. polished={stats.polished} guard_failed={stats.guard_failed} "
        f"errored={stats.errored} cost=~${stats.cost_usd:.4f}"
    )


if __name__ == "__main__":
    main()
