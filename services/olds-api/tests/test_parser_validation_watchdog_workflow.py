from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "parser-validation-watchdog.yml"
)


def test_watchdog_recurs_and_reads_v2_validation_state() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'cron: "17,47 * * * *"' in workflow
    assert "timeout 120 sudo apt-get update" in workflow
    assert "https://downloads.rclone.org/rclone-current-linux-amd64.zip" in workflow
    assert "news-archive/v2/validation-state" in workflow
    assert '--include "validation/*/*/state/summary.json"' in workflow
    assert '--include "holdout-v*/*/*/state/rotation-audit.json"' in workflow
    assert "copy_summary()" in workflow
    assert "restore_source_root()" in workflow
    assert "source_restore_parallelism=8" in workflow
    assert "wait_for_source_restore_batch" in workflow
    assert 'sort -u "$available_source_shards"' in workflow
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
        '"wsj/2010-2015/commoncrawl-prefix"',
        '"wsj/2016-2026/commoncrawl-prefix"',
        '"scmp/2010-2015/commoncrawl-prefix"',
        '"scmp/2016-2026/commoncrawl-prefix"',
    ):
        assert supplemental_root in workflow
    assert '"npr/${supplemental_year}-${supplemental_year}/commoncrawl-prefix"' in workflow
    assert "--source-capacity-root" in workflow
    assert "Dispatch supplemental Common Crawl catalog" in workflow
    assert "capacity_deficient_cells=$(jq -r '.capacityDeficientCells // 0'" in workflow
    assert "CAPACITY_DEFICIENT_CELLS:" in workflow
    assert 'steps.plan.outputs.capacity_deficient_cells != \'0\'' in workflow
    assert '[ "$capacity_deficient_cells" -eq 0 ]' in workflow
    assert "nikkei-common-crawl-catalog.yml" in workflow
    assert "caixin-common-crawl-catalog.yml" in workflow
    assert '{"kind":"caixin","year":"2010"}' in workflow
    assert '"publisher":"wsj","fromYear":"2010","toYear":"2015","collectionFromYear":"2014","collectionToYear":"2016","collectionOrder":"newest"' in workflow
    assert '"publisher":"wsj","fromYear":"2016","toYear":"2026","collectionFromYear":"2017","collectionToYear":"2026","collectionOrder":"newest"' in workflow
    assert "wsj-common-crawl-|caixin-common-crawl-|aljazeera-common-crawl-|scmp-common-crawl-" in workflow
    assert 'MAX_CATALOG_CONCURRENCY: "2"' in workflow
    assert "active_catalog_count" in workflow
    assert "Supplemental Common Crawl concurrency is full" in workflow
    assert "hydrations=200" in workflow
    assert 'wsj|aljazeera|axios|nyt|ap|zaobao|caixin)' in workflow
    assert '-f max_hydrations="$hydrations"' in workflow
    assert "queries=8" in workflow
    assert "queries=32" in workflow
    assert "pages=1" in workflow
    assert "pages=32" in workflow
    assert '-f max_pages="$pages"' in workflow
    assert '-f max_queries="$queries"' in workflow
    assert 'jq -r \'.collectionOrder // "oldest"\'' in workflow
    assert "Both standard parser slots are occupied" in workflow
    assert 'jq -e \'.shouldContinue == false\'' in workflow
    assert "- name: Restore validation summaries\n        timeout-minutes: 25" in workflow
    assert "ready: [" in workflow
    assert "capacityDeficient: [" in workflow
    assert 'object_listing="$(\n              rclone lsl' in workflow
    assert 'rclone lsl "$summary_remote" \\\n                --timeout 30s --contimeout 10s' in workflow
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
    assert "only the" in workflow
    assert "test(\"^parser-(validation|holdout-v[0-9]+|smoke-v[0-9]+)-\")" in workflow
    assert 'MAX_SUPERSEDED_REFRESH_DISPATCH: "1"' in workflow
    assert "Reserved one parser refresh slot" in workflow
    assert "Keeping one dispatch slot reserved for parser validation." in workflow
    assert "catalog_dispatch_limit" in workflow
    assert "Keeping one dispatch slot reserved" in workflow
    assert "--json status,displayTitle,createdAt" in workflow
    assert 'gh run list --branch "$DISPATCH_REF" --limit 1000' in workflow
    assert "--workflow parser-validation-accelerator.yml" in workflow
    assert 'parser_runs="$RUNNER_TEMP/parser-runs.json"' in workflow
    assert "fromdateiso8601" in workflow
    assert "$now - 18000" in workflow
    assert "stale" in workflow.lower()


def test_watchdog_dispatches_two_workers_per_validation_job() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "workers=2" in workflow
    assert 'if [ "$publisher" = "ft" ] || [ "$publisher" = "wsj" ]; then' in workflow
    assert 'current_evaluated="$(jq -r \'.currentEvaluated // 0\'' in workflow
    assert '[ "$current_evaluated" -lt 200 ]' in workflow
    assert "Enabling bounded FT Infini-News discovery" in workflow
    assert '-f enable_ft_infini_direct_discovery="$enable_ft_infini_direct_discovery"' in workflow
    assert '-f workers="$workers"' in workflow
    assert "-f workers=8" not in workflow
