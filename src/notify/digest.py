"""Daily digest of unread comment replies — one consolidated email per user.

``collect_digests`` -> render one text email per recipient -> send -> mark_thread_notified
(only after a successful send, per recipient). Idempotent: a clean re-run (nothing new
since the last send) yields ``[]``.

Scheduling is an operational step, not a code gate: run this as a Railway cron service
(Settings -> Cron Schedule, UTC only), e.g. ``0 16 * * *`` for ~18:00 Europe/Bratislava.
"""

from __future__ import annotations

import os

from notify.email_sender import DryRunEmailSender, EmailSender
from server.db import collect_digests, mark_thread_notified
from storage.db import get_conn
from storage.models import UserDigest


def _log(msg: str) -> None:
    print(f"digest: {msg}", flush=True)


def _ltree_to_url_locator(ltree_path: str) -> str:
    """Convert an ltree path (e.g. 'I.q3.a1') back to 'ST.I.Q3.A1' for URL construction.

    A pure copy of ``server.app._ltree_to_url_locator`` — kept local so ``notify`` doesn't
    need to import the Flask app module.
    """
    parts = ltree_path.split(".")
    result = []
    for p in parts:
        if p.startswith("q") and p[1:].isdigit():
            result.append("Q" + p[1:])
        elif p.startswith("a") and p[1:].isdigit():
            result.append("A" + p[1:])
        else:
            result.append(p.upper())
    return "ST." + ".".join(result)


def _segment_link(base_url: str, locator: str, segment_id: int) -> str:
    """Deep link to a segment's row within its article page."""
    article_locator = ".".join(locator.split(".")[:3])
    coord = _ltree_to_url_locator(article_locator)
    return f"{base_url}/~{coord}#seg-{segment_id}"


def render_digest(digest: UserDigest, base_url: str) -> tuple[str, str]:
    """Build (subject, text body) for one recipient's digest, items grouped by locator."""
    n = len(digest.items)
    subject = f"Aquinas: {n} new repl{'y' if n == 1 else 'ies'} to threads you're in"

    by_locator: dict[str, list] = {}
    for item in digest.items:
        by_locator.setdefault(item.locator, []).append(item)

    lines = [subject, ""]
    for locator, items in by_locator.items():
        link = _segment_link(base_url, locator, items[0].segment_id)
        lines.append(f"{locator}  ({link})")
        for item in items:
            lines.append(f"  {item.author} · {item.created_at:%Y-%m-%d %H:%M}")
            lines.append(f"    {item.body}")
        lines.append("")

    return subject, "\n".join(lines).rstrip() + "\n"


def send_comment_digest(sender: EmailSender | DryRunEmailSender | None = None) -> list[str]:
    """Send today's digest to every recipient with unread comment replies.

    ``sender`` defaults to ``EmailSender.from_env()``; pass a ``DryRunEmailSender`` for
    tests/local runs without SMTP. Returns the user_emails a digest was actually sent to
    (a recipient whose send fails is skipped, logged, and left un-notified for retry).
    """
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    sender = sender or EmailSender.from_env()

    _log("starting comment scan")
    with get_conn() as conn:
        digests = collect_digests(conn)
    total_items = sum(len(d.items) for d in digests)
    _log(f"found {len(digests)} recipient(s), {total_items} unread item(s) total")

    sent_to: list[str] = []
    failed_to: list[str] = []
    for digest in digests:
        subject, body = render_digest(digest, base_url)
        _log(f"sending to {digest.user_email} ({len(digest.items)} item(s))")
        try:
            sender.send(digest.user_email, subject, body)
        except Exception as exc:
            _log(f"FAILED sending to {digest.user_email}: {exc!r}")
            failed_to.append(digest.user_email)
            continue

        sent_to.append(digest.user_email)
        with get_conn() as conn:
            for seg_id in {item.segment_id for item in digest.items}:
                mark_thread_notified(conn, seg_id, digest.user_email)
        _log(f"sent to {digest.user_email}")

    _log(f"complete — sent {len(sent_to)}/{len(digests)}, failed {len(failed_to)}")
    return sent_to


if __name__ == "__main__":
    send_comment_digest()
