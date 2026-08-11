from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = (
    REPOSITORY_ROOT
    / ".github"
    / "workflows"
    / "nikkei-common-crawl-catalog.yml"
)


def test_catalog_hydrates_dates_and_checkpoints_private_state() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "build_common_crawl_prefix_manifest.py" in workflow
    assert "--publisher nikkei" in workflow
    assert '--collection-from-year "$COLLECTION_FROM_YEAR"' in workflow
    assert '--collection-to-year "$COLLECTION_TO_YEAR"' in workflow
    assert "--collection-order oldest" in workflow
    assert '--target-articles-per-year "$TARGET_ARTICLES_PER_YEAR"' in workflow
    assert '--max-date-hydrations "$MAX_HYDRATIONS"' in workflow
    assert "--data-min-request-interval 0.5" in workflow
    assert "--page-size 1000" in workflow
    assert "verify_b2_private_bucket.py" in workflow
    assert "checkpoint_capture_state.py" in workflow
    assert "commoncrawl-prefix" in workflow
    assert "discovery.sqlite3.gz" in workflow
    assert "manifest.jsonl.gz" in workflow


def test_catalog_continues_after_discovery_or_hydration_progress() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    dispatch = workflow[workflow.index("Dispatch next bounded run") :]
    assert "steps.discovery.outputs.should_continue == 'true'" in dispatch
    assert "steps.discovery.outputs.advances != '0'" in dispatch
    assert "steps.discovery.outputs.hydration_attempted != '0'" in dispatch
    assert '-f max_hydrations="$MAX_HYDRATIONS"' in dispatch
    assert (
        '-f target_articles_per_year="$TARGET_ARTICLES_PER_YEAR"'
        in dispatch
    )
    assert "auto_continue=true" in dispatch
    assert '--ref "$GITHUB_REF_NAME"' in dispatch
