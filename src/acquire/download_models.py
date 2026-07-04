"""Download all model files required by the lemmatize pipeline.

MorphoDiTa models are placed under <project_root>/models/ as subdirectories,
mirroring the ZIP archive structure from LINDAT. Existing directories are skipped.

Czech model (version 161115):
  czech-morfflex-pdt-161115/   — CC BY-NC-SA 4.0
  Source: https://lindat.mff.cuni.cz/repository/handle/11234/1-1836

Slovak model (version 170914):
  slovak-morfflex-pdt-170914/  — CC BY-NC-SA 4.0
  Source: https://lindat.mff.cuni.cz/repository/handle/11234/1-3278

CLTK Latin models are placed under ~/cltk_data/ by the CLTK library itself.

Usage:
  uv run python -m acquire.download_models
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import urllib.request
import zipfile

_MODELS_DIR = pathlib.Path(__file__).resolve().parents[2] / "models"

# Bitstream UUIDs resolved via the DSpace 7 REST API:
#   /server/api/core/items/<item-uuid>/bundles → ORIGINAL bundle
#   /server/api/core/bundles/<bundle-uuid>/bitstreams → content href
_MORPHODITA_MODELS = [
    {
        "dirname": "czech-morfflex-pdt-161115",
        "label": "Czech MorfFlex+PDT 161115",
        "url": "https://lindat.mff.cuni.cz/repository/server/api/core/bitstreams/e5cd15f2-4457-49c2-a460-17a6bfdd097a/content",
    },
    {
        "dirname": "slovak-morfflex-pdt-170914",
        "label": "Slovak MorfFlex+PDT 170914",
        "url": "https://lindat.mff.cuni.cz/repository/server/api/core/bitstreams/e84cfe5d-3e79-46a9-add9-0fe7e9a3f5f4/content",
    },
]


def _download_and_extract(url: str, dest_dir: pathlib.Path, label: str) -> None:
    print(f"  Downloading {label} ...", flush=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)
    try:
        urllib.request.urlretrieve(url, tmp_path)
        print(f"  Extracting to {dest_dir.parent} ...", flush=True)
        with zipfile.ZipFile(tmp_path) as zf:
            zf.extractall(dest_dir.parent)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download/extract {label}: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    print(f"  Done: {dest_dir}", flush=True)


def _download_morphodita() -> None:
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for spec in _MORPHODITA_MODELS:
        dest = _MODELS_DIR / spec["dirname"]
        if dest.exists():
            print(f"[skip] {spec['label']} already present: {dest}")
            continue
        print(f"[download] {spec['label']}")
        _download_and_extract(spec["url"], dest, spec["label"])


def _download_cltk() -> None:
    cltk_path = pathlib.Path.home() / "cltk_data" / "lat" / "model" / "lat_models_cltk"
    if cltk_path.exists():
        print("[skip] CLTK Latin models already present")
        return
    print("[download] CLTK Latin models (lat_models_cltk)")
    from cltk.data.fetch import FetchCorpus
    FetchCorpus("lat").import_corpus("lat_models_cltk")
    print(f"  Done: {cltk_path}")


def main() -> None:
    _download_cltk()
    _download_morphodita()
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
