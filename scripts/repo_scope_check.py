from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = REPO_ROOT / "reports" / "repo_scope_check.md"

ALLOWED_SCOPE_AREAS = [
    "momentum screening",
    "Yahoo Finance data collection",
    "strategy report generation",
    "market regime analysis",
    "trading brief generation",
    "portfolio exposure warnings",
    "trade journal and performance review",
]

IGNORED_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "reports",
}

SCANNED_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".sh",
}

BLOCKED_FILENAMES = {"trade_loop.py"}
EXCLUDED_SCAN_PATHS = {"scripts/repo_scope_check.py"}


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    fail_on_match: bool = True


RULES = [
    Rule(
        name="broker import",
        pattern=re.compile(r"^\s*(?:from|import)\s+.*\bbroker\b", re.IGNORECASE),
    ),
    Rule(
        name="place_order call",
        pattern=re.compile(r"\bplace_order\s*\(", re.IGNORECASE),
    ),
    Rule(
        name="shadow/champion live trading reference",
        pattern=re.compile(
            r"(?:\bshadow\b|\bchampion\b).{0,60}\blive trading\b|\blive trading\b.{0,60}(?:\bshadow\b|\bchampion\b)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        name="adaptive live trading reference",
        pattern=re.compile(r"\badaptive live trading\b", re.IGNORECASE),
    ),
    Rule(
        name="autonomous execution reference",
        pattern=re.compile(r"\bautonomous execution\b", re.IGNORECASE),
    ),
]


def _is_ignored(path: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.parts)


def _iter_text_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if _is_ignored(path.relative_to(repo_root)):
            continue
        if path.suffix.lower() in SCANNED_SUFFIXES:
            files.append(path)
    return sorted(files)


def _scan() -> tuple[list[str], list[str], list[str]]:
    findings: list[str] = []
    warnings: list[str] = []
    flagged_files: list[str] = []

    for blocked_name in sorted(BLOCKED_FILENAMES):
        matches = [p for p in REPO_ROOT.rglob(blocked_name) if p.is_file() and not _is_ignored(p.relative_to(REPO_ROOT))]
        for matched in matches:
            rel = matched.relative_to(REPO_ROOT).as_posix()
            findings.append(f"`{rel}` (blocked AI-trader filename)")
            flagged_files.append(rel)

    for file_path in _iter_text_files(REPO_ROOT):
        rel_path = file_path.relative_to(REPO_ROOT).as_posix()
        if rel_path in EXCLUDED_SCAN_PATHS:
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for rule in RULES:
            for index, line in enumerate(content.splitlines(), start=1):
                if not rule.pattern.search(line):
                    continue
                entry = f"`{rel_path}:{index}` matched {rule.name}: `{line.strip()[:180]}`"
                if rule.fail_on_match:
                    findings.append(entry)
                    flagged_files.append(rel_path)
                else:
                    warnings.append(entry)

    deduped_files = sorted(set(flagged_files))
    return findings, warnings, deduped_files


def _build_report(findings: list[str], warnings: list[str], flagged_files: list[str]) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    status = "FAIL" if findings else "PASS"
    lines: list[str] = [
        "# Repository Scope Check",
        "",
        f"- Generated: {now}",
        f"- Status: **{status}**",
        "",
        "## Intended repository scope",
        "",
    ]
    lines.extend([f"- {scope}" for scope in ALLOWED_SCOPE_AREAS])
    lines.extend(["", "## Misplaced AI-trader findings"])
    if findings:
        lines.extend([f"- {item}" for item in findings])
    else:
        lines.append("- None detected.")

    lines.extend(["", "## Additional warnings"])
    if warnings:
        lines.extend([f"- {item}" for item in warnings])
    else:
        lines.append("- None.")

    lines.extend(["", "## Flagged files summary"])
    if flagged_files:
        lines.extend([f"- `{path}`" for path in flagged_files])
    else:
        lines.append("- No AI-trader-only files or references found.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    findings, warnings, flagged_files = _scan()
    report = _build_report(findings, warnings, flagged_files)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"Wrote scope report: {REPORT_PATH}")

    if findings:
        print("Repository scope check failed: AI-trader references detected.")
        for finding in findings:
            print(f" - {finding}")
        return 1

    print("Repository scope check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
