"""The timing record of the full test suite (`guardrail perf-gate`).

Not a lock: it never blocks by itself. After a full run (the push suite), it appends one
line to a history and compares the run with the previous line of the same label, so a suite
that is slowly getting slower is seen when it grows and not when it has become a problem.
Functions here return data; `guardrail/cli.py` prints it and chooses the exit code.
"""

from __future__ import annotations

import json
import os

from . import core, locks

# The decisions of the comparison. A run warns when the total grows MORE than TOTAL_GROWTH
# (a fraction of the previous total) or a file grows MORE than FILE_FACTOR times its previous
# duration. FILE_FLOOR_MS keeps a file that takes a few milliseconds from warning when it
# triples: below it a duration is noise, not a trend.
TOTAL_GROWTH = 0.10
FILE_FACTOR = 2.0
FILE_FLOOR_MS = 100
TOP_FILES = 10
DEFAULT_LABEL = "default"


def summarise(root: str, report: str, label: str, commit: str | None, date: str) -> dict:
    """One history line from a Jest-shaped report: how many tests, the total duration and the
    duration of each test file (the sum of its tests), paths relative to the repository."""
    outcomes = locks.read_jest_report(report)
    if outcomes is None:
        raise core.GuardrailError(f"{report} is not a Jest-shaped JSON report")
    if not outcomes:
        raise core.GuardrailError(f"{report} has no tests, so there is nothing to record")
    files: dict[str, float] = {}
    for outcome in outcomes:
        path = core.repo_path(root, outcome.file)
        files[path] = files.get(path, 0.0) + (outcome.duration or 0.0)
    files = {path: round(ms, 3) for path, ms in sorted(files.items())}
    return {"date": date, "commit": commit, "label": label, "tests": len(outcomes), "totalMs": round(sum(files.values()), 3), "files": files}


def _is_record(value) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("label"), str)
        and isinstance(value.get("tests"), int)
        and isinstance(value.get("totalMs"), (int, float))
        and isinstance(value.get("files"), dict)
    )


def read_history(path: str) -> tuple[list[dict], list[str]]:
    """The records of the history file, oldest first, and a note for each line that is not one.
    A missing file is an empty history. A damaged line is skipped, never a reason to stop the
    suite that is being recorded."""
    if not os.path.exists(path):
        return [], []
    records, notes = [], []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle.read().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if _is_record(record):
                records.append(record)
            else:
                notes.append(f"{path}:{number}: not a record of this tool, skipped")
    return records, notes


def previous_run(history: list[dict], label: str) -> dict | None:
    """The last record of this label, or None."""
    for record in reversed(history):
        if record["label"] == label:
            return record
    return None


def append(path: str, entry: dict) -> None:
    """Add one line to the history, creating the file and its folder."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def compare(current: dict, previous: dict | None) -> tuple[list[str], list[str]]:
    """Compare a run with the previous one of its label. Returns (warnings, lines for a person)."""
    label = current["label"]
    if previous is None:
        return [], [f"perf-gate [{label}]: {current['tests']} tests, {core.format_ms(current['totalMs'])} ms in total; no previous run of this label to compare with"]
    warnings = []
    before, now = previous["totalMs"], current["totalMs"]
    change = f"{(now - before) / before * 100:+.1f}%" if before else "n/a"
    lines = [
        f"perf-gate [{label}]: {current['tests']} tests (previous {previous['tests']}), "
        f"{core.format_ms(now)} ms in total (previous {core.format_ms(before)} ms, {change})"
    ]
    if before and now > before * (1 + TOTAL_GROWTH):
        warnings.append(f"WARNING: the total duration grew {change} (from {core.format_ms(before)} ms to {core.format_ms(now)} ms), over the {TOTAL_GROWTH:.0%} limit.")
    grown = []  # (growth in ms, path, before or None for a new file, now)
    for path, ms in current["files"].items():
        old = previous["files"].get(path)
        if old is None:
            grown.append((ms, path, None, ms))
            continue
        if ms > old:
            grown.append((ms - old, path, old, ms))
        if old > 0 and ms > FILE_FLOOR_MS and ms > old * FILE_FACTOR:
            warnings.append(f"WARNING: {path} grew {ms / old:.1f}x (from {core.format_ms(old)} ms to {core.format_ms(ms)} ms), over the {FILE_FACTOR:g}x limit.")
    grown.sort(key=lambda row: (-row[0], row[1]))
    if grown:
        lines.append(f"files that grew most (top {TOP_FILES}):")
        for growth, path, old, ms in grown[:TOP_FILES]:
            if old is None:
                lines.append(f"  {path}  {core.format_ms(ms)} ms (new)")
            else:
                times = f", {ms / old:.1f}x" if old else ""
                lines.append(f"  {path}  {core.format_ms(old)} ms -> {core.format_ms(ms)} ms  (+{core.format_ms(growth)} ms{times})")
    return warnings, lines
