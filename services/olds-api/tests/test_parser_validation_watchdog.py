from __future__ import annotations

import json
from pathlib import Path

import pytest

from jojo_olds_api.parser_validation_watchdog import (
    plan_validation_dispatch,
)
from jojo_olds_api.parser_validation import qa_policy_revision
from jojo_olds_api.publisher_specs import publisher_spec


def _write_summary(
    root: Path,
    relative_path: str,
    *,
    publisher: str,
    year: int,
    evaluated: int,
    complete_rate: float = 1.0,
    qa_rate: float = 1.0,
    errors: int = 0,
    unbound_capture_inputs: int = 0,
    qa_revision: int | None = None,
    parser_version: str | None = None,
) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    effective_parser_version = (
        parser_version or publisher_spec(publisher).parser_version
    )
    payload = {
                "parserValidation": {
                    "years": {
                        str(year): {
                            "target": 800,
                            "parserVersion": (
                                effective_parser_version
                            ),
                            "evaluated": evaluated,
                            "completeRate": complete_rate,
                            "qaPassRate": qa_rate,
                            "errors": errors,
                            "unboundCaptureInputs": (
                                unbound_capture_inputs
                            ),
                            "qaRevision": (
                                qa_revision
                                if qa_revision is not None
                                else qa_policy_revision(publisher)
                            ),
                        }
                    }
                }
            }
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    if (
        relative_path.startswith("holdout-v")
        and evaluated >= 800
        and complete_rate >= 0.95
        and qa_rate == 1.0
        and errors == 0
        and unbound_capture_inputs == 0
    ):
        audit_path = path.with_name("rotation-audit.json")
        audit_path.write_text(
            json.dumps(
                {
                    "formatVersion": (
                        "jojo-parser-validation-holdout-audit/1"
                    ),
                    "publisher": publisher,
                    "expectedParserVersion": effective_parser_version,
                    "targetPerYear": 800,
                    "requireComplete": True,
                    "passed": True,
                    "issues": [],
                    "years": {
                        str(year): {
                            "previousUniqueEvaluated": 800,
                            "currentEvaluated": evaluated,
                            "priorCohortOverlap": 0,
                            "exclusionOverlap": 0,
                            "missingPriorExclusions": 0,
                            "wrongExclusionCohortLabels": 0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )


def test_watchdog_accepts_ready_full_or_accelerator_summary(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "ap/2016-2026/sitemap-wayback/state/summary.json",
        publisher="ap",
        year=2016,
        evaluated=800,
    )
    _write_summary(
        tmp_path,
        "validation/bloomberg/2017/state/summary.json",
        publisher="bloomberg",
        year=2017,
        evaluated=800,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=66,
    )
    cells = {
        (task["publisher"], task["year"])
        for task in plan["tasks"]
    }

    assert plan["targetCells"] == 214
    assert ("axios", 2016) not in {
        (row["publisher"], row["year"])
        for row in plan["cellProgress"]
    }
    assert ("axios", 2017) in {
        (row["publisher"], row["year"])
        for row in plan["cellProgress"]
    }
    assert plan["readyCells"] == 2
    assert ("ap", 2016) not in cells
    assert ("bloomberg", 2017) not in cells
    ap_2016 = next(
        row
        for row in plan["cellProgress"]
        if row["publisher"] == "ap" and row["year"] == 2016
    )
    assert ap_2016 == {
        "publisher": "ap",
        "year": 2016,
        "target": 800,
        "evaluated": 800,
        "replayableEvaluated": 800,
        "completeRate": 1.0,
        "qaPassRate": 1.0,
        "errors": 0,
        "unboundCaptureInputs": 0,
        "qaRevision": 0,
        "parserVersion": "ap-parser/0.6.21",
        "requiredCohort": None,
        "selectedCohort": "source",
        "ready": True,
        "active": False,
    }


def test_watchdog_ignores_old_parser_and_active_cell(tmp_path: Path):
    _write_summary(
        tmp_path,
        "ft/2016-2026/sitemap-wayback/state/summary.json",
        publisher="ft",
        year=2018,
        evaluated=500,
        parser_version="ft-parser/0.7.0",
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[
            "parser-validation-ft-2018",
            "unrelated workflow title",
        ],
        max_dispatch=66,
    )
    cells = {
        (task["publisher"], task["year"])
        for task in plan["tasks"]
    }

    assert plan["readyCells"] == 0
    assert plan["activeCells"] == 1
    assert ("ft", 2018) not in cells
    ft_2018 = next(
        row
        for row in plan["cellProgress"]
        if row["publisher"] == "ft" and row["year"] == 2018
    )
    assert ft_2018["evaluated"] == 0
    assert ft_2018["replayableEvaluated"] == 500
    assert ft_2018["parserVersion"] is None
    assert ft_2018["active"] is True


def test_watchdog_accepts_ready_holdout_and_tracks_active_holdout(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "holdout-v3/nyt/2018/state/summary.json",
        publisher="nyt",
        year=2018,
        evaluated=800,
    )
    _write_summary(
        tmp_path,
        "smoke-v1/nyt/2019/state/summary.json",
        publisher="nyt",
        year=2019,
        evaluated=800,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=["parser-holdout-v4-nyt-2020"],
        max_dispatch=66,
        publishers=["nyt"],
    )
    tasks = {
        (task["publisher"], task["year"])
        for task in plan["tasks"]
    }

    assert plan["readyCells"] == 1
    assert plan["activeCells"] == 1
    assert ("nyt", 2018) not in tasks
    assert ("nyt", 2019) in tasks
    assert ("nyt", 2020) not in tasks


def test_watchdog_rejects_ready_stale_parser_or_qa_revision(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "validation/wsj/2020/state/summary.json",
        publisher="wsj",
        year=2020,
        evaluated=800,
        parser_version="wsj-parser/0.8.44",
    )
    _write_summary(
        tmp_path,
        "validation/wsj/2021/state/summary.json",
        publisher="wsj",
        year=2021,
        evaluated=800,
        qa_revision=0,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=66,
        available_source_shards={"wsj/2016-2026/wayback"},
    )
    tasks = {
        (task["publisher"], task["year"])
        for task in plan["tasks"]
    }

    assert plan["readyCells"] == 0
    assert ("wsj", 2020) in tasks
    assert ("wsj", 2021) in tasks
    for year in (2020, 2021):
        row = next(
            item
            for item in plan["cellProgress"]
            if item["publisher"] == "wsj" and item["year"] == year
        )
        assert row["evaluated"] == 0
        assert row["replayableEvaluated"] == 800
        assert row["ready"] is False


def test_watchdog_rotates_stale_nyt_parser_to_next_holdout(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "validation/nyt/2018/state/summary.json",
        publisher="nyt",
        year=2018,
        evaluated=800,
        parser_version="nyt-parser/0.8.52",
    )
    _write_summary(
        tmp_path,
        "holdout-v3/nyt/2018/state/summary.json",
        publisher="nyt",
        year=2018,
        evaluated=800,
        parser_version="nyt-parser/0.8.54",
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
        publishers=["nyt"],
        available_source_shards={"nyt/2016-2026/sitemap-wayback"},
    )

    assert plan["tasks"][0]["cohort"] == "holdout-v4"
    cell = next(
        row
        for row in plan["cellProgress"]
        if row["publisher"] == "nyt" and row["year"] == 2018
    )
    assert cell["requiredCohort"] == "holdout-v4"
    assert cell["evaluated"] == 0


def test_watchdog_accepts_current_holdout_after_stale_cohorts(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "holdout-v2/axios/2017/state/summary.json",
        publisher="axios",
        year=2017,
        evaluated=327,
        parser_version="axios-parser/0.1.6",
    )
    _write_summary(
        tmp_path,
        "holdout-v3/axios/2017/state/summary.json",
        publisher="axios",
        year=2017,
        evaluated=800,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
        publishers=["axios"],
        available_source_shards={"axios/2017-2026/wayback-urlkey"},
    )

    assert plan["readyCells"] == 1
    cell = next(
        row
        for row in plan["cellProgress"]
        if row["publisher"] == "axios" and row["year"] == 2017
    )
    assert cell["requiredCohort"] == "holdout-v3"
    assert cell["selectedCohort"] == "holdout-v3"
    assert cell["ready"] is True


def test_watchdog_rotates_ready_holdout_with_missing_or_failed_audit(
    tmp_path: Path,
):
    relative = "holdout-v1/nyt/2019/state/summary.json"
    _write_summary(
        tmp_path,
        relative,
        publisher="nyt",
        year=2019,
        evaluated=800,
    )
    (tmp_path / relative).with_name("rotation-audit.json").unlink()

    missing = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
        publishers=["nyt"],
        available_source_shards={"nyt/2016-2026/sitemap-wayback"},
    )
    assert missing["readyCells"] == 0
    assert missing["tasks"][0]["cohort"] == "holdout-v2"

    _write_summary(
        tmp_path,
        relative,
        publisher="nyt",
        year=2019,
        evaluated=800,
    )
    audit_path = (tmp_path / relative).with_name("rotation-audit.json")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["passed"] = False
    audit["issues"] = ["2019:prior-cohort-overlap"]
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    failed = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
        publishers=["nyt"],
        available_source_shards={"nyt/2016-2026/sitemap-wayback"},
    )
    assert failed["readyCells"] == 0
    assert failed["tasks"][0]["cohort"] == "holdout-v2"


def test_watchdog_requires_npr_holdout_for_unaudited_stale_source(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "npr/2010-2015/wayback-urlkey/state/summary.json",
        publisher="npr",
        year=2014,
        evaluated=800,
        parser_version="npr-parser/0.1.17",
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
        publishers=["npr"],
        available_source_shards={"npr/2010-2015/wayback-urlkey"},
    )

    assert plan["tasks"][0]["year"] == 2014
    assert plan["tasks"][0]["cohort"] == "holdout-v1"


def test_watchdog_retains_audited_rotated_validation_cell(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "ft/2016-2026/sitemap-wayback/state/summary.json",
        publisher="ft",
        year=2016,
        evaluated=524,
        parser_version="ft-parser/0.8.29",
    )
    _write_summary(
        tmp_path,
        "validation/ft/2016/state/summary.json",
        publisher="ft",
        year=2016,
        evaluated=800,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
        publishers=["ft"],
        available_source_shards={"ft/2016-2026/sitemap-wayback"},
    )

    cell = next(
        row
        for row in plan["cellProgress"]
        if row["publisher"] == "ft" and row["year"] == 2016
    )
    assert cell["requiredCohort"] is None
    assert cell["selectedCohort"] == "validation"
    assert cell["ready"] is True


def test_watchdog_only_plans_cells_with_readable_source_manifests(
    tmp_path: Path,
):
    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=66,
        available_source_shards={"axios/2017-2026/wayback-urlkey"},
    )

    assert plan["targetCells"] == 10
    assert {
        (task["publisher"], task["year"])
        for task in plan["tasks"]
    } == {("axios", year) for year in range(2017, 2027)}


def test_watchdog_filters_to_explicit_pending_publishers(tmp_path: Path):
    _write_summary(
        tmp_path,
        "validation/reuters/2024/state/summary.json",
        publisher="reuters",
        year=2024,
        evaluated=799,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=66,
        publishers=["wsj", "nyt"],
    )

    assert plan["publishers"] == ["wsj", "nyt"]
    assert plan["targetCells"] == 34
    assert {
        row["publisher"] for row in plan["cellProgress"]
    } == {"wsj", "nyt"}
    assert all(
        task["publisher"] in {"wsj", "nyt"}
        for task in plan["tasks"]
    )


def test_watchdog_rejects_unknown_explicit_publisher(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported watchdog publishers"):
        plan_validation_dispatch(
            state_root=tmp_path,
            active_titles=[],
            max_dispatch=1,
            publishers=["unknown-news"],
        )


def test_watchdog_prioritizes_nearly_complete_current_sample(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "validation/reuters/2023/state/summary.json",
        publisher="reuters",
        year=2023,
        evaluated=499,
        complete_rate=1.0,
        qa_rate=1.0,
    )
    _write_summary(
        tmp_path,
        "validation/bloomberg/2020/state/summary.json",
        publisher="bloomberg",
        year=2020,
        evaluated=300,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
    )

    assert plan["tasks"] == [
        {
            "publisher": "reuters",
            "year": 2023,
            "sourceManifestShard": (
                "reuters/2021-2026/reuters-sitemap-wayback"
            ),
            "runnerOs": "ubuntu-latest",
            "currentEvaluated": 499,
            "replayableEvaluated": 499,
            "parserVersion": "reuters-parser/0.7.25",
            "cohort": "validation",
        }
    ]


def test_watchdog_requires_disjoint_wsj_holdout_after_validation(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "validation/wsj/2022/state/summary.json",
        publisher="wsj",
        year=2022,
        evaluated=800,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
        publishers=["wsj"],
        available_source_shards={"wsj/2016-2026/wayback"},
    )

    assert plan["readyCells"] == 0
    assert plan["tasks"][0]["cohort"] == "holdout-v1"
    cell = next(
        row
        for row in plan["cellProgress"]
        if row["publisher"] == "wsj" and row["year"] == 2022
    )
    assert cell["requiredCohort"] == "holdout-v1"
    assert cell["selectedCohort"] == "holdout-v1"
    assert cell["evaluated"] == 0
    assert cell["replayableEvaluated"] == 800
    assert cell["ready"] is False

    _write_summary(
        tmp_path,
        "holdout-v1/wsj/2022/state/summary.json",
        publisher="wsj",
        year=2022,
        evaluated=800,
    )
    complete = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
        publishers=["wsj"],
        available_source_shards={"wsj/2016-2026/wayback"},
    )
    complete_cell = next(
        row
        for row in complete["cellProgress"]
        if row["publisher"] == "wsj" and row["year"] == 2022
    )
    assert complete_cell["evaluated"] == 800
    assert complete_cell["ready"] is True


def test_watchdog_requires_wsj_holdout_for_legacy_source_summary(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "wsj/2010-2015/wayback-urlkey/state/summary.json",
        publisher="wsj",
        year=2013,
        evaluated=835,
        qa_revision=0,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
        publishers=["wsj"],
        available_source_shards={"wsj/2010-2015/wayback-urlkey"},
    )

    assert plan["readyCells"] == 0
    assert plan["tasks"][0]["year"] == 2013
    assert plan["tasks"][0]["cohort"] == "holdout-v1"
    cell = next(
        row
        for row in plan["cellProgress"]
        if row["year"] == 2013
    )
    assert cell["requiredCohort"] == "holdout-v1"
    assert cell["evaluated"] == 0
    assert cell["replayableEvaluated"] == 835


def test_watchdog_excludes_years_below_manifest_capacity(tmp_path: Path):
    shard = "caixin/2010-2015/wayback-urlkey"
    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=10,
        publishers=["caixin"],
        available_source_shards={shard},
        source_year_capacities={
            shard: {
                2010: 1069,
                2011: 1268,
                2012: 1238,
                2013: 1103,
                2014: 2837,
                2015: 1,
            }
        },
    )

    assert plan["targetCells"] == 5
    assert {
        (row["publisher"], row["year"])
        for row in plan["cellProgress"]
    } == {
        ("caixin", 2010),
        ("caixin", 2011),
        ("caixin", 2012),
        ("caixin", 2013),
        ("caixin", 2014),
    }


def test_watchdog_prioritizes_stale_corpus_for_parser_replay(
    tmp_path: Path,
):
    _write_summary(
        tmp_path,
        "bloomberg/2016-2026/sitemap-wayback/state/summary.json",
        publisher="bloomberg",
        year=2016,
        evaluated=519,
        parser_version="bloomberg-parser/0.8.0",
    )
    _write_summary(
        tmp_path,
        "validation/reuters/2024/state/summary.json",
        publisher="reuters",
        year=2024,
        evaluated=41,
    )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=1,
    )

    assert plan["tasks"][0]["publisher"] == "bloomberg"
    assert plan["tasks"][0]["year"] == 2016
    assert plan["tasks"][0]["currentEvaluated"] == 0
    assert plan["tasks"][0]["replayableEvaluated"] == 519
    assert plan["tasks"][0]["cohort"] == "holdout-v1"


def test_watchdog_requires_all_quality_gates(tmp_path: Path):
    for year, complete_rate, qa_rate, errors, unbound in (
        (2021, 0.9499, 1.0, 0, 0),
        (2022, 1.0, 0.9999, 0, 0),
        (2023, 1.0, 1.0, 1, 0),
        (2024, 1.0, 1.0, 0, 1),
    ):
        _write_summary(
            tmp_path,
            f"validation/wsj/{year}/state/summary.json",
            publisher="wsj",
            year=year,
            evaluated=800,
            complete_rate=complete_rate,
            qa_rate=qa_rate,
            errors=errors,
            unbound_capture_inputs=unbound,
        )

    plan = plan_validation_dispatch(
        state_root=tmp_path,
        active_titles=[],
        max_dispatch=66,
    )
    cells = {
        (task["publisher"], task["year"])
        for task in plan["tasks"]
    }

    assert ("wsj", 2021) in cells
    assert ("wsj", 2022) in cells
    assert ("wsj", 2023) in cells
    assert ("wsj", 2024) in cells
