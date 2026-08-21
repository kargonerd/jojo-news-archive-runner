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
    assert 'supplemental_root="caixin/${supplemental_year}-${supplemental_year}/commoncrawl-prefix"' in workflow
    assert '"ap/2010-2015/legacy-archive"' in workflow
    assert '--include "*manifest-summary.json"' in workflow
    for supplemental_root in (
        '"reuters/2010-2015/commoncrawl-prefix"',
        '"reuters/2016-2020/commoncrawl-prefix"',
        '"reuters/2021-2026/commoncrawl-prefix"',
        '"aljazeera/2010-2015/commoncrawl-prefix"',
        '"aljazeera/2016-2026/commoncrawl-prefix"',
        '"nikkei/2010-2015/commoncrawl-prefix"',
        '"nikkei/2016-2026/commoncrawl-prefix"',
        '"scmp/2010-2015/commoncrawl-prefix"',
        '"scmp/2016-2026/commoncrawl-prefix"',
    ):
        assert supplemental_root in workflow
    assert '"npr/${supplemental_year}-${supplemental_year}/commoncrawl-prefix"' in workflow
    assert "--source-capacity-root" in workflow
    assert "- name: Restore validation summaries\n        timeout-minutes: 15" in workflow
    assert "ready: [" in workflow
    assert "capacityDeficient: [" in workflow
    assert 'object_listing="$(\n              rclone lsl' in workflow
    assert 'rclone lsf "$remote_dir" --files-only \\\n                --timeout 30s --contimeout 10s' in workflow
    assert '&& [ -n "$object_listing" ]; then' in workflow
    assert "--retries 3 --low-level-retries 6" in workflow
    assert "transient 5xx" in workflow
    assert "VALIDATION_PUBLISHERS:" in workflow
    assert (
        "ft wsj nyt ap axios npr nikkei zaobao aljazeera scmp caixin"
        in workflow
    )
    assert "other in-scope publisher eligible" in workflow
    assert "--publishers $VALIDATION_PUBLISHERS" in workflow
    assert "cohort=\"$(jq -r '.cohort'" in workflow
    assert '-f cohort="$cohort"' in workflow
    assert "validation-capacity-probe.json" in workflow
    assert "activeSupersededRunCount" in workflow
    assert "effective_active_count" in workflow
    assert "superseded parser runs exempt" in workflow
    assert 'MAX_SUPERSEDED_REFRESH_DISPATCH: "1"' in workflow
    assert "Reserved one parser refresh slot" in workflow
    assert "catalog_dispatch_limit" in workflow
    assert "Keeping one dispatch slot reserved" in workflow
    assert "--json status,displayTitle,createdAt" in workflow
    assert "fromdateiso8601" in workflow
    assert "$now - 18000" in workflow
    assert "stale" in workflow.lower()


def test_watchdog_dispatches_two_workers_per_validation_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "-f workers=2" in workflow
    assert "-f workers=8" not in workflow
