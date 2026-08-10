from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Iterable

from .publisher_specs import publisher_spec
from .parser_qa_policy import qa_policy_revision
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
# WSJ's current validation states were migrated/replayed from earlier parser
# cohorts. They remain useful exclusion sources, but validation/source alone is
# not independent convergence evidence even when its parser version is current.
REQUIRED_HOLDOUT_PUBLISHERS = {"wsj"}
# These validation-named cells were independently audited against their older
# source cohort and proved zero-overlap. Keep the exception narrow and
# evidence-backed; every other stale-version baseline automatically rotates to
# a new numbered holdout.
PROVEN_ROTATED_VALIDATION_CELLS = {
    ("ft", 2016),
    ("npr", 2010),
    ("npr", 2011),
    ("npr", 2012),
    ("npr", 2013),
    ("npr", 2026),
}
ACTIVE_TITLE_RE = re.compile(
    r"^parser-(?:qa|validation|holdout-v[1-9][0-9]*)-"
    r"(aljazeera|ap|axios|bloomberg|caixin|ft|nikkei|npr|nyt|reuters|scmp|wsj|zaobao)-"
    r"(20\d{2})$"
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
    publishers: Iterable[str] | None = None,
) -> dict[str, object]:
    if max_dispatch < 0:
        raise ValueError("max_dispatch must be non-negative")
    requested_publishers = (
        set(PUBLISHER_ORDER)
        if publishers is None
        else {publisher.strip() for publisher in publishers if publisher.strip()}
    )
    unsupported_publishers = requested_publishers - set(PUBLISHER_ORDER)
    if unsupported_publishers:
        raise ValueError(
            "unsupported watchdog publishers: "
            + ", ".join(sorted(unsupported_publishers))
        )
    publisher_order = tuple(
        publisher
        for publisher in PUBLISHER_ORDER
        if publisher in requested_publishers
    )
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
        for publisher in publisher_order
    }
    qa_revisions = {
        publisher: qa_policy_revision(publisher)
        for publisher in publisher_order
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
            "unboundCaptureInputs": 0,
            "qaRevision": None,
            "parserVersion": None,
            "summaryPaths": [],
            "cohortRows": {},
            "observedRows": [],
        }
        for publisher in publisher_order
        for year in TARGET_YEARS
        if _source_year_is_available(
            publisher,
            year,
            available_source_shards=available_shards,
        )
    }
    summaries_read = 0
    invalid_summaries: list[str] = []
    invalid_rotation_audits: list[str] = []
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
        rotation_audit: dict[str, object] | None = None
        rotation_audit_path = summary_path.with_name("rotation-audit.json")
        if rotation_audit_path.exists():
            try:
                candidate_audit = json.loads(
                    rotation_audit_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                invalid_rotation_audits.append(
                    rotation_audit_path.relative_to(state_root).as_posix()
                )
            else:
                if isinstance(candidate_audit, dict):
                    rotation_audit = candidate_audit
                else:
                    invalid_rotation_audits.append(
                        rotation_audit_path.relative_to(state_root).as_posix()
                    )
        cohort = _cohort_from_summary_path(
            summary_path,
            state_root=state_root,
        )
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
            evidence_row = {
                **row,
                "_rotationAudit": rotation_audit,
            }
            cell = progress[cell_key]
            cell["replayableEvaluated"] = max(
                int(cell["replayableEvaluated"]),
                _integer(row.get("evaluated")),
            )
            paths = cell["summaryPaths"]
            if isinstance(paths, list):
                paths.append(summary_path.relative_to(state_root).as_posix())
            if (
                _integer(row.get("evaluated")) > 0
            ):
                observed = cell["observedRows"]
                assert isinstance(observed, list)
                observed.append({"cohort": cohort, "row": evidence_row})
            if (
                row.get("parserVersion") != versions[publisher]
                or _integer(row.get("qaRevision"))
                != qa_revisions[publisher]
            ):
                continue
            evaluated = _integer(row.get("evaluated"))
            cohort_rows = cell["cohortRows"]
            assert isinstance(cohort_rows, dict)
            previous = cohort_rows.get(cohort)
            if not isinstance(previous, dict) or evaluated >= _integer(
                previous.get("evaluated")
            ):
                cohort_rows[cohort] = evidence_row

    for (publisher, year), cell in progress.items():
        cohort_rows = cell["cohortRows"]
        assert isinstance(cohort_rows, dict)
        observed_rows = cell["observedRows"]
        assert isinstance(observed_rows, list)
        required_cohort = _required_holdout_cohort(
            publisher=publisher,
            year=year,
            observed_rows=observed_rows,
            current_rows=cohort_rows,
            parser_version=versions[publisher],
            qa_revision=qa_revisions[publisher],
        )
        selected_cohort = required_cohort
        if selected_cohort is None and cohort_rows:
            selected_cohort = _select_current_cohort(cohort_rows)
        selected = cohort_rows.get(selected_cohort, {})
        if isinstance(selected, dict) and selected:
            cell["evaluated"] = _integer(selected.get("evaluated"))
            cell["target"] = max(
                MINIMUM_SAMPLES,
                _integer(selected.get("target")),
            )
            cell["completeRate"] = float(
                selected.get("completeRate") or 0
            )
            cell["qaPassRate"] = float(selected.get("qaPassRate") or 0)
            cell["errors"] = _integer(selected.get("errors"))
            cell["unboundCaptureInputs"] = _integer(
                selected.get("unboundCaptureInputs")
            )
            cell["qaRevision"] = _integer(selected.get("qaRevision"))
            cell["parserVersion"] = selected.get("parserVersion")
            cell["ready"] = _year_ready(
                selected,
                cohort=selected_cohort,
                publisher=publisher,
                year=year,
            )
        cell["requiredCohort"] = required_cohort
        cell["selectedCohort"] = selected_cohort

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
        for index, publisher in enumerate(publisher_order)
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
            cohort=_dispatch_cohort(progress[(publisher, year)]),
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
            "unboundCaptureInputs": int(
                progress[(publisher, year)]["unboundCaptureInputs"]
            ),
            "qaRevision": progress[(publisher, year)]["qaRevision"],
            "parserVersion": progress[(publisher, year)]["parserVersion"],
            "requiredCohort": progress[(publisher, year)]["requiredCohort"],
            "selectedCohort": progress[(publisher, year)]["selectedCohort"],
            "ready": (publisher, year) in ready_cells,
            "active": (publisher, year) in active_cells,
        }
        for publisher in publisher_order
        for year in TARGET_YEARS
        if (publisher, year) in progress
    ]
    return {
        "formatVersion": FORMAT_VERSION,
        "publishers": list(publisher_order),
        "targetCells": len(progress),
        "readyCells": len(ready_cells),
        "activeCells": len(active_cells),
        "pendingCells": len(progress) - len(ready_cells),
        "summariesRead": summaries_read,
        "invalidSummaries": invalid_summaries,
        "invalidRotationAudits": invalid_rotation_audits,
        "currentParserVersions": versions,
        "currentQaRevisions": qa_revisions,
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
    if parts[0] == "validation" or re.fullmatch(
        r"holdout-v[1-9][0-9]*", parts[0]
    ):
        return parts[1] if len(parts) > 1 else None
    return parts[0]


def _cohort_from_summary_path(
    summary_path: Path,
    *,
    state_root: Path,
) -> str:
    parts = summary_path.relative_to(state_root).parts
    if parts and (
        parts[0] == "validation"
        or re.fullmatch(r"holdout-v[1-9][0-9]*", parts[0])
    ):
        return parts[0]
    return "source"


def _quality_gates_ready(row: dict[str, object]) -> bool:
    evaluated = _integer(row.get("evaluated"))
    target = max(MINIMUM_SAMPLES, _integer(row.get("target")))
    return bool(
        evaluated >= target
        and float(row.get("completeRate") or 0) >= MINIMUM_COMPLETE_RATE
        and float(row.get("qaPassRate") or 0) >= MINIMUM_QA_PASS_RATE
        and _integer(row.get("errors")) == 0
        and _integer(row.get("unboundCaptureInputs")) == 0
    )


def _year_ready(
    row: dict[str, object],
    *,
    cohort: str | None,
    publisher: str,
    year: int,
) -> bool:
    if not _quality_gates_ready(row):
        return False
    if not isinstance(cohort, str) or not cohort.startswith("holdout-v"):
        return True
    return _holdout_audit_passes(
        row,
        publisher=publisher,
        year=year,
    )


def _holdout_audit_passes(
    row: dict[str, object],
    *,
    publisher: str,
    year: int,
) -> bool:
    audit = row.get("_rotationAudit")
    if not isinstance(audit, dict):
        return False
    years = audit.get("years")
    year_row = years.get(str(year)) if isinstance(years, dict) else None
    target = max(MINIMUM_SAMPLES, _integer(row.get("target")))
    return bool(
        audit.get("formatVersion")
        == "jojo-parser-validation-holdout-audit/1"
        and audit.get("passed") is True
        and audit.get("publisher") == publisher
        and audit.get("expectedParserVersion") == row.get("parserVersion")
        and audit.get("requireComplete") is True
        and _integer(audit.get("targetPerYear")) == target
        and isinstance(year_row, dict)
        and _integer(year_row.get("previousUniqueEvaluated")) > 0
        and _integer(year_row.get("currentEvaluated")) >= target
        and _integer(year_row.get("priorCohortOverlap")) == 0
        and _integer(year_row.get("exclusionOverlap")) == 0
        and _integer(year_row.get("missingPriorExclusions")) == 0
        and _integer(year_row.get("wrongExclusionCohortLabels")) == 0
    )


def _dispatch_cohort(cell: dict[str, object]) -> str:
    required = cell.get("requiredCohort")
    if isinstance(required, str) and required:
        return required
    selected = cell.get("selectedCohort")
    if isinstance(selected, str) and re.fullmatch(
        r"holdout-v[1-9][0-9]*", selected
    ):
        return selected
    return "validation"


def _required_holdout_cohort(
    *,
    publisher: str,
    year: int,
    observed_rows: list[object],
    current_rows: dict[str, object],
    parser_version: str,
    qa_revision: int,
) -> str | None:
    if (
        (publisher, year) in PROVEN_ROTATED_VALIDATION_CELLS
        and any(name in {"source", "validation"} for name in current_rows)
    ):
        return None
    stale_numbers: list[int] = []
    observed_numbers: list[int] = []
    observed_baseline = False
    for item in observed_rows:
        if not isinstance(item, dict):
            continue
        cohort = item.get("cohort")
        row = item.get("row")
        if not isinstance(cohort, str) or not isinstance(row, dict):
            continue
        number = _cohort_number(cohort)
        if number is None:
            continue
        observed_numbers.append(number)
        observed_baseline = observed_baseline or cohort in {
            "source",
            "validation",
        }
        if (
            row.get("parserVersion") != parser_version
            or _integer(row.get("qaRevision")) != qa_revision
        ):
            stale_numbers.append(number)
    current_holdouts = {
        number: cohort
        for cohort in current_rows
        if (number := _cohort_number(cohort)) is not None and number > 0
    }
    for number, cohort in current_holdouts.items():
        row = current_rows.get(cohort)
        if (
            isinstance(row, dict)
            and _quality_gates_ready(row)
            and not _holdout_audit_passes(
                row,
                publisher=publisher,
                year=year,
            )
        ):
            stale_numbers.append(number)
    if stale_numbers:
        newest_stale = max(stale_numbers)
        if current_holdouts and max(current_holdouts) > newest_stale:
            return current_holdouts[max(current_holdouts)]
        next_number = max(observed_numbers, default=0) + 1
        return f"holdout-v{max(1, next_number)}"
    if publisher in REQUIRED_HOLDOUT_PUBLISHERS and observed_baseline:
        if current_holdouts:
            return current_holdouts[max(current_holdouts)]
        return "holdout-v1"
    return None


def _select_current_cohort(current_rows: dict[str, object]) -> str:
    holdouts = {
        number: cohort
        for cohort in current_rows
        if (number := _cohort_number(cohort)) is not None and number > 0
    }
    if holdouts:
        return holdouts[max(holdouts)]
    for cohort in ("validation", "source"):
        if cohort in current_rows:
            return cohort
    return max(
        current_rows,
        key=lambda name: _integer(
            current_rows[name].get("evaluated")
            if isinstance(current_rows[name], dict)
            else 0
        ),
    )


def _cohort_number(cohort: str) -> int | None:
    if cohort in {"source", "validation"}:
        return 0
    match = re.fullmatch(r"holdout-v([1-9][0-9]*)", cohort)
    return int(match.group(1)) if match is not None else None


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
    cohort: str,
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
        "cohort": cohort,
    }
