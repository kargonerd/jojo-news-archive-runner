from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "news-archive-watchdog.yml"
)


def test_archive_watchdog_has_one_global_two_slot_dispatcher() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'MAX_STANDARD_CONCURRENCY: "2"' in workflow
    assert 'MAX_DISPATCH_PER_RUN: "2"' in workflow
    assert "strategy:" not in workflow
    assert "matrix:" not in workflow
    assert 'startswith("news-raw-") or startswith("parser-")' in workflow
    assert "available=$((MAX_STANDARD_CONCURRENCY - active_count))" in workflow
    assert 'if [ "$dispatched" -ge "$available" ]' in workflow


def test_archive_watchdog_is_catalog_only_and_skips_complete_shards() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert '-f max_captures=0' in workflow
    assert "formal validation stores only its selected canonical raw samples" in workflow
    assert "manifest-summary.json" in workflow
    assert "jq -e '.complete == true'" in workflow


def test_archive_watchdog_prioritizes_required_legacy_catalogs() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    wsj = workflow.index(
        '{"publisher":"wsj","fromYear":"2010","toYear":"2015"'
    )
    nikkei = workflow.index(
        '{"publisher":"nikkei","fromYear":"2010","toYear":"2015"'
    )
    scmp = workflow.index(
        '{"publisher":"scmp","fromYear":"2010","toYear":"2015"'
    )
    assert wsj < nikkei < scmp
    assert '"publisher":"zaobao"' in workflow
    assert '"publisher":"aljazeera"' in workflow
    assert '"publisher":"caixin"' in workflow
