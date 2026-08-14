"""Render feature templates to PNG offline for quick visual iteration.

Usage (from anywhere, using the apex plugin venv):

    ./.venv/bin/python tests/render_preview.py stats
    ./.venv/bin/python tests/render_preview.py rotation
    ./.venv/bin/python tests/render_preview.py stats -o /tmp/out.png --no-open

- stats    renders from tests/fixtures/stats_sample.json (offline, no network for data)
- rotation fetches live data via APEX_AUTH from the repo .env (needs network)

Assets (avatars/icons/thumbnails) are still fetched over the network the first
time, then served from data/asset_cache/ on subsequent runs.
"""

import sys
import json
import asyncio
import argparse
import subprocess
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from dotenv import load_dotenv

load_dotenv(PLUGIN_ROOT.parent.parent / ".env")

from features.rendering import fetch_assets, render_image, RENDER_WIDTH

FIXTURE_DIR = PLUGIN_ROOT / "tests" / "fixtures"
OUTPUT_DIR = PLUGIN_ROOT / "tests" / "output"


async def build_stats() -> tuple[str, list[str], int]:
    from features.stats import build_stats_html, RENDER_WIDTH as STATS_WIDTH

    summary = json.loads((FIXTURE_DIR / "stats_sample.json").read_text())[
        "playerStatsSummary"
    ]
    built = build_stats_html(summary)
    if built is None:
        raise SystemExit("build_stats_html returned None (empty fixture?)")
    html, assets = built
    return html, assets, STATS_WIDTH


async def build_rotation() -> tuple[str, list[str], int]:
    import os

    from features.rotation import build_rotation_html, fetch_rotation

    auth = os.getenv("APEX_AUTH")
    if not auth:
        raise SystemExit("APEX_AUTH not set; add it to the repo .env to render rotation")
    data = await fetch_rotation(auth)
    built = build_rotation_html(data)
    if built is None:
        raise SystemExit("build_rotation_html returned None (no rotation data)")
    html, assets = built
    return html, assets, RENDER_WIDTH


BUILDERS = {"stats": build_stats, "rotation": build_rotation}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feature", choices=sorted(BUILDERS), help="which template to render")
    parser.add_argument("-o", "--output", type=Path, help="output PNG path")
    parser.add_argument("--no-open", action="store_true", help="do not auto-open the image")
    args = parser.parse_args()

    html, assets, width = await BUILDERS[args.feature]()
    images = await fetch_assets(assets)

    out = args.output
    if out is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_DIR / f"{args.feature}.png"

    png = await asyncio.to_thread(render_image, html, images, width)
    out.write_bytes(png)
    print(f"wrote {out} ({len(png):,} bytes, {len(images)} assets)")

    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", str(out)], check=False)  # noqa: ASYNC221


if __name__ == "__main__":
    asyncio.run(main())
