"""``python -m pipeline`` → the interactive driver."""

from __future__ import annotations

import sys

from pipeline.interactive import main

if __name__ == "__main__":
    sys.exit(main())
