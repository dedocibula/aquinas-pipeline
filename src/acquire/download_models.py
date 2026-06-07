"""Download MorphoDiTa model files required by the lemmatize pipeline.

Models are placed under <project_root>/models/. Existing files are skipped.

Czech model (already present after initial setup):
  czech-morfflex-pdt-161115.dict  — CC BY-NC-SA

Slovak model (required for M4 terminology pre-check):
  slovak-morfflex-pdt-170914.dict — CC BY-NC-SA
  Source: http://hdl.handle.net/11234/1-3278

Usage:
  uv run python -m acquire.download_models
"""

from __future__ import annotations

import pathlib
import sys
import urllib.request

_MODELS_DIR = pathlib.Path(__file__).resolve().parents[2] / "models"

_MODELS = [
    {
        "filename": "slovak-morfflex-pdt-170914.dict",
        "url": "https://lindat.mff.cuni.cz/repository/xmlui/bitstream/handle/11234/1-3278/slovak-morfflex-pdt-170914.dict",
        "label": "Slovak MorfFlex",
    },
]


def _download(url: str, dest: pathlib.Path) -> None:
    print(f"  Downloading {dest.name} ...", flush=True)
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download {url}: {exc}") from exc
    print(f"  Saved to {dest}", flush=True)


def main() -> None:
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for spec in _MODELS:
        dest = _MODELS_DIR / spec["filename"]
        if dest.exists():
            print(f"[skip] {spec['label']} already present: {dest}")
            continue
        print(f"[download] {spec['label']}")
        _download(spec["url"], dest)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
