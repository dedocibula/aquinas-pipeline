"""Reset the 100-segment golden set to pending for the next optimization epoch."""
from __future__ import annotations

import json
import os
from pathlib import Path

from common.db import get_conn

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SAMPLE = _REPO_ROOT / os.environ.get("PILOT_SAMPLE_FILE", "docs/pilot_sample_100.json")


def main() -> None:
    data = json.loads(_SAMPLE.read_text())
    segments = data["segments"] if isinstance(data, dict) else data
    ids = [s["segment_id"] for s in segments]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE segment SET translation_status = 'pending' WHERE segment_id = ANY(%s)",
                (ids,),
            )
            updated = cur.rowcount
            cur.execute(
                "DELETE FROM segment_text WHERE segment_id = ANY(%s) AND lang = 'sk'",
                (ids,),
            )
            deleted = cur.rowcount
        conn.commit()
    print(f"Reset {updated} segments to pending, deleted {deleted} Slovak text rows.")


if __name__ == "__main__":
    main()
