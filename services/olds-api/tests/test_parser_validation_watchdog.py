from __future__ import annotations

import json
from pathlib import Path

from jojo_olds_api.parser_validation_watchdog import (
    plan_validation_dispatch,
)
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
    parser_version: str | None = None,
) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "parserValidation": {
                    "years": {
                        str(year): {
                            "target": 500,
                            "parserVersion": (
                                parser_version
                                or publisher_spec(publisher).parser_version
                            ),
                            "evaluated": evaluated,
                            "completeRate": complete_rate,
                            "qaPassRate": qa_rate,
                            "errors": errors,
                        }
                    }
                }
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
        evaluated=500,
    )
    _write_summary(
        tmp_path,
        "validation/bloomberg/2017/state/summary.json",
        publisher="bloomberg",
        year=2017,
        evaluated=500,
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

    assert plan["targetCells"] == 66
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
        "target": 500,
        "evaluated": 500,
        "replayableEvaluated": 500,
        "completeRate": 1.0,
        "qaPassRate": 1.0,
        "errors": 0,
        "parserVersion": "ap-parser/0.6.16",
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
            "parser-qa-ft-2018",
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
                "parserVersion": "reuters-parser/0.7.20",
        }
    ]


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


def test_watchdog_requires_rates_and_zero_errors(tmp_path: Path):
    for year, complete_rate, qa_rate, errors in (
        (2021, 0.9499, 1.0, 0),
        (2022, 1.0, 0.9499, 0),
        (2023, 1.0, 1.0, 1),
    ):
        _write_summary(
            tmp_path,
            f"validation/wsj/{year}/state/summary.json",
            publisher="wsj",
            year=year,
            evaluated=500,
            complete_rate=complete_rate,
            qa_rate=qa_rate,
            errors=errors,
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
