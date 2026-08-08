from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Iterable

from .publisher_specs import publisher_spec
from .parser_source_shards import parser_source_manifest_shard


FORMAT_VERSION = "jojo-parser-validation-watchdog/1"
TARGET_YEARS = tuple(range(2010, 2027))
MINIMUM_SAMPLES = 800
MINIMUM_COMPLETE_RATE = 0.95
# A single QA finding requires a parser fix and a fresh holdout. Keep the
# scheduler aligned with parser_validation.py rather than treating a merely
# high pass rate as convergence.
MINIMUM_QA_PASS_RATE = 1.0
PUBLISHER_ORDER = (
    "reuters",
    "bloomberg",
    "ft",
    "wsj",
    "nyt",
    "ap",
    "axios",
    "npr",
    "nikkei",
    "zaobao",
    "aljazeera",
    "scmp",
    "caixin",
)
ACTIVE_TITLE_RE = re.compile(
    r"^parser-(?:qa|validation)-(aljazeera|ap|axios|bloomberg|caixin|ft|nikkei|npr|nyt|reuters|scmp|wsj|zaobao)-(20\d{2})$"
)


def _source_year_is_available(
    publisher: str,
    year: int,
    *,
    available_source_shards: set[str] | None,
) -> bool:
    try:
        source_shard = parser_source_manifest_shard(publisher, year)
    except ValueError:
        return False
    return (
        available_source_shards is None
        or source_shard in available_source_shards
    )


def plan_validation_dispatch(
    *,
    state_root: Path,
    active_titles: Iterable[str],
    max_dispatch: int,
    available_source_shards: Iterable[str] | None = None,
) -> dict[str, object]:
    if max_dispatch < 0:
        raise ValueError("max_dispatch must be non-negative")
    available_shards = (
        None
        if available_source_shards is None
        else {
            shard.strip()
            for shard in available_source_shards
            if shard.strip()
        }
    )
    versions = {
        publisher: publisher_spec(publisher).parser_version
        for publisher in PUBLISHER_ORDER
    }
    progress = {
        (publisher, year): {
            "ready": False,
            "evaluated": 0,
            "replayableEvaluated": 0,
            "target": MINIMUM_SAMPLES,
            "completeRate": 0.0,
            "qaPassRate": 0.0,
            "errors": 0,
            "parserVersion": None,
            "summaryPaths": [],
        }
        for publisher in PUBLISHER_ORDER
        for year in TARGET_YEARS
        if _source_year_is_available(
            publisher,
            year,
            available_source_shards=available_shards,
        )
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
            cell_key = (publisher, year)
            if cell_key not in progress:
                continue
            row = years.get(str(year))
            if not isinstance(row, dict):
                continue
            cell = progress[cell_key]
            cell["replayableEvaluated"] = max(
                int(cell["replayableEvaluated"]),
                _integer(row.get("evaluated")),
            )
            paths = cell["summaryPaths"]
            if isinstance(paths, list):
                paths.append(summary_path.relative_to(state_root).as_posix())
            if row.get("parserVersion") != versions[publisher]:
                continue
            evaluated = _integer(row.get("evaluated"))
            if evaluated >= int(cell["evaluated"]):
                cell["evaluated"] = evaluated
                cell["target"] = max(
                    MINIMUM_SAMPLES,
                    _integer(row.get("target")),
                )
                cell["completeRate"] = float(
                    row.get("completeRate") or 0
                )
                cell["qaPassRate"] = float(
                    row.get("qaPassRate") or 0
                )
                cell["errors"] = _integer(row.get("errors"))
                cell["parserVersion"] = row.get("parserVersion")
            cell["ready"] = bool(cell["ready"]) or _year_ready(row)

    active_cells: set[tuple[str, int]] = set()
    for title in active_titles:
        match = ACTIVE_TITLE_RE.match(title.strip())
        if match is None:
            continue
        publisher, year = match.groups()
        parsed_year = int(year)
        if (publisher, parsed_year) in progress:
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
    cell_progress = [
        {
            "publisher": publisher,
            "year": year,
            "target": int(progress[(publisher, year)]["target"]),
            "evaluated": int(progress[(publisher, year)]["evaluated"]),
            "replayableEvaluated": int(
                progress[(publisher, year)]["replayableEvaluated"]
            ),
            "completeRate": float(
                progress[(publisher, year)]["completeRate"]
            ),
            "qaPassRate": float(progress[(publisher, year)]["qaPassRate"]),
            "errors": int(progress[(publisher, year)]["errors"]),
            "parserVersion": progress[(publisher, year)]["parserVersion"],
            "ready": (publisher, year) in ready_cells,
            "active": (publisher, year) in active_cells,
        }
        for publisher in PUBLISHER_ORDER
        for year in TARGET_YEARS
        if (publisher, year) in progress
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
        "cellProgress": cell_progress,
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
    return {
        "publisher": publisher,
        "year": year,
        "sourceManifestShard": parser_source_manifest_shard(
            publisher,
            year,
        ),
        # macOS 15 hosted runners currently do not ship the pinned 3.12.13
        # interpreter used by the archive workflow.  NYT capture already
        # applies its own slower request cadence on Ubuntu.
        "runnerOs": "ubuntu-latest",
        "currentEvaluated": evaluated,
        "replayableEvaluated": replayable_evaluated,
        "parserVersion": parser_version,
    }
