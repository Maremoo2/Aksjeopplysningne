from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def combine_global_brief(reports_root: Path, run_type: str) -> None:
    usa_shareable = sorted((reports_root / "usa" / "shareable").glob("trading_brief_*.md"))
    nordic_shareable = sorted((reports_root / "nordic" / "shareable").glob("trading_brief_*.md"))
    global_dir = reports_root / "global"
    global_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%d_%H%M")
    brief_path = global_dir / f"trading_brief_global_{stamp}.md"
    json_path = global_dir / f"trading_brief_global_{stamp}.json"

    parts = ["# Global Trading Brief", "", "Market: Global", f"Run type: {run_type.title()}"]
    payload = {"market": "GLOBAL", "generated_at": now.isoformat(), "briefs": []}

    for label, candidates in (("USA", usa_shareable), ("Nordic", nordic_shareable)):
        if not candidates:
            continue
        latest = candidates[-1]
        text = latest.read_text(encoding="utf-8").strip()
        parts.extend([f"## {label}", "", text, ""])
        payload["briefs"].append({"market": label, "path": str(latest), "content": text})

    brief_path.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved global trading brief: {brief_path}")
    print(f"Saved global trading brief JSON: {json_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Combine latest USA and Nordic briefs into a global brief.")
    parser.add_argument("--reports-root", default="reports", help="Root reports directory.")
    parser.add_argument("--run-type", default="manual", help="Run type label for the combined brief.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    combine_global_brief(reports_root=Path(args.reports_root), run_type=args.run_type)


if __name__ == "__main__":
    main()
