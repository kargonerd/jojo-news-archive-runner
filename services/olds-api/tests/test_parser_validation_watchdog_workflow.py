from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "parser-validation-watchdog.yml"
)


def test_watchdog_recurs_and_reads_v2_validation_state() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'cron: "17,47 * * * *"' in workflow
    assert "news-archive/v2/validation-state" in workflow
    assert '--include "validation/*/*/state/summary.json"' in workflow
    assert '--include "holdout-v*/*/*/state/rotation-audit.json"' in workflow
    assert "copy_summary()" in workflow
    assert '"wsj/2016-2026/wayback"' in workflow
    assert "available-source-shards.txt" in workflow
    assert "--available-source-shards" in workflow
    assert "manifest-summary.json" in workflow
    assert "--source-capacity-root" in workflow
    assert 'object_listing="$(\n              rclone lsl' in workflow
    assert '&& [ -n "$object_listing" ]; then' in workflow
    assert "VALIDATION_PUBLISHERS:" in workflow
    assert "ft wsj nyt ap axios npr nikkei zaobao aljazeera scmp caixin" in workflow
    assert "--publishers $VALIDATION_PUBLISHERS" in workflow
    assert "cohort=\"$(jq -r '.cohort'" in workflow
    assert '-f cohort="$cohort"' in workflow


def test_watchdog_dispatches_two_workers_per_validation_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "-f workers=2" in workflow
    assert "-f workers=8" not in workflow
