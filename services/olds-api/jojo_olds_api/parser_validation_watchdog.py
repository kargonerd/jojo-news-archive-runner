from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Iterable

from .publisher_specs import publisher_spec


FORMAT_VERSION = "jojo-parser-validation-watchdog/1"
TARGET_YEARS = tuple(range(2016, 2027))
MINIMUM_SAMPLES = 500
MINIMUM_COMPLETE_RATE = 0.95
MINIMUM_QA_PASS_RATE = 0.95
PUBLISHER_ORDER = (
    "reuters",
    "bloomberg",
    "ft",
    "wsj",
    "nyt",
    "ap",
)
SOURCE_SHARDS = {
    "ap": "ap/2016-2026/sitemap-wayback",
    "bloomberg": "bloomberg/2016-2026/sitemap-wayback",
    "ft": "ft/2016-2026/sitemap-wayback",
    "nyt": "nyt/2016-2026/sitemap-wayback",
    "wsj": "wsj/2016-2026/wayback-urlkey",
}
ACTIVE_TITLE_RE = re.compile(
    r"^parser-qa-(ap|bloomberg|ft|nyt|reuters|wsj)-(20\d{2})$"
)


def plan_validation_dispatch(
    *,
    state_root: Path,
    active_titles: Iterable[str],
    max_dispatch: int,
) -> dict[str, object]:
    if max_dispatch < 0:
        raise ValueError("max_dispatch must be non-negative")
    versions = {
        publisher: publisher_spec(publisher).parser_version
        for publisher in PUBLISHER_ORDER
    }
    progress = {
        (publisher, year): {
            "ready": False,
            "evaluated": 0,
            "replayableEvaluated": 0,
            "summaryPaths": [],
        }
        for publisher in PUBLISHER_ORDER
        for year in TARGET_YEARS
    }
    summaries_read = 0
    invalid_summaries: list[str] = []
    for summary_path in sorted(state_root.rglob("summary.json")):
        publisher = _publisher_from_summary_path(
            summary_path,
            state_root=state_root,
        )
        if publisher not in versions:
            continue
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            invalid_summaries.append(
                summary_path.relative_to(state_root).as_posix()
            )
            continue
        summaries_read += 1
        validation = payload.get("parserValidation")
        if not isinstance(validation, dict):
            continue
        years = validation.get("years")
        if not isinstance(years, dict):
            continue
        for year in TARGET_YEARS:
            row = years.get(str(year))
            if not isinstance(row, dict):
                continue
            cell = progress[(publisher, year)]
            cell["replayableEvaluated"] = max(
                int(cell["replayableEvaluated"]),
                _integer(row.get("evaluated")),
            )
            paths = cell["summaryPaths"]
            if isinstance(paths, list):
                paths.append(summary_path.relative_to(state_root).as_posix())
            if row.get("parserVersion") != versions[publisher]:
                continue
            cell["evaluated"] = max(
                int(cell["evaluated"]),
                _integer(row.get("evaluated")),
            )
            cell["ready"] = bool(cell["ready"]) or _year_ready(row)

    active_cells: set[tuple[str, int]] = set()
    for title in active_titles:
        match = ACTIVE_TITLE_RE.match(title.strip())
        if match is None:
            continue
        publisher, year = match.groups()
        parsed_year = int(year)
        if parsed_year in TARGET_YEARS:
            active_cells.add((publisher, parsed_year))

    ready_cells = {
        cell for cell, values in progress.items() if values["ready"]
    }
    candidates = [
        cell
        for cell in progress
        if cell not in ready_cells and cell not in active_cells
    ]
    order = {
        publisher: index
        for index, publisher in enumerate(PUBLISHER_ORDER)
    }
    candidates.sort(
        key=lambda cell: (
            -int(progress[cell]["replayableEvaluated"]),
            -int(progress[cell]["evaluated"]),
            order[cell[0]],
            cell[1],
        )
    )
    tasks = [
        _task(
            publisher=publisher,
            year=year,
            evaluated=int(progress[(publisher, year)]["evaluated"]),
            replayable_evaluated=int(
                progress[(publisher, year)]["replayableEvaluated"]
            ),
            parser_version=versions[publisher],
        )
        for publisher, year in candidates[:max_dispatch]
    ]
    return {
        "formatVersion": FORMAT_VERSION,
        "targetCells": len(progress),
        "readyCells": len(ready_cells),
        "activeCells": len(active_cells),
        "pendingCells": len(progress) - len(ready_cells),
        "summariesRead": summaries_read,
        "invalidSummaries": invalid_summaries,
        "currentParserVersions": versions,
        "tasks": tasks,
    }


def _publisher_from_summary_path(
    summary_path: Path,
    *,
    state_root: Path,
) -> str | None:
    try:
        parts = summary_path.relative_to(state_root).parts
    except ValueError:
        return None
    if not parts:
        return None
    if parts[0] == "validation":
        return parts[1] if len(parts) > 1 else None
    return parts[0]


def _year_ready(row: dict[str, object]) -> bool:
    evaluated = _integer(row.get("evaluated"))
    target = max(MINIMUM_SAMPLES, _integer(row.get("target")))
    return bool(
        evaluated >= target
        and float(row.get("completeRate") or 0) >= MINIMUM_COMPLETE_RATE
        and float(row.get("qaPassRate") or 0) >= MINIMUM_QA_PASS_RATE
        and _integer(row.get("errors")) == 0
    )


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _task(
    *,
    publisher: str,
    year: int,
    evaluated: int,
    replayable_evaluated: int,
    parser_version: str,
) -> dict[str, object]:
    if publisher == "reuters":
        shard = (
            "reuters/2016-2020/wayback-urlkey"
            if year <= 2020
            else "reuters/2021-2026/reuters-sitemap-wayback"
        )
    else:
        shard = SOURCE_SHARDS[publisher]
    return {
        "publisher": publisher,
        "year": year,
        "sourceManifestShard": shard,
        "runnerOs": (
            "macos-15-intel" if publisher == "nyt" else "ubuntu-latest"
        ),
        "currentEvaluated": evaluated,
        "replayableEvaluated": replayable_evaluated,
        "parserVersion": parser_version,
    }
