"""Bake a standalone copy of index.html with data inlined — for the Claude artifact
preview, which is CSP-locked and can't fetch /api/*.

The live pod uses index.html as-is (it fetches its data). This script only produces a
throwaway static file for sharing a snapshot.

    python build_preview.py EVENTS.json [OUT.html]

EVENTS.json: a JSON array of event rows (the /api/events response, e.g. captured from
Neon). Logical levels are read from logical_levels.json next to this script.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: build_preview.py EVENTS.json [OUT.html]")
    events = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "preview.html"
    levels = json.loads((HERE / "logical_levels.json").read_text())

    html = (HERE / "index.html").read_text()
    start, end = html.index("/* DATA_BOOT_START"), html.index("/* DATA_BOOT_END */")
    baked = (
        "/* DATA_BOOT_START (baked) */\n"
        f"const EVENTS = {json.dumps(events, separators=(',', ':'))};\n"
        f"const LEVEL_SEED = {json.dumps(levels, separators=(',', ':'))};\n"
    )
    html = html[:start] + baked + html[end:]
    out.write_text(html)
    print(f"wrote {out} ({len(html)} bytes, {len(events)} events)")


if __name__ == "__main__":
    main()
