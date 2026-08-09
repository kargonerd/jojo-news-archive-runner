from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = (
    REPOSITORY_ROOT
    / ".github"
    / "workflows"
    / "npr-common-crawl-catalog.yml"
)


def test_catalog_is_bounded_checkpointed_and_private() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "build_common_crawl_prefix_manifest.py" in workflow
    assert "sudo apt-get install -y rclone" in workflow
    assert "--collection-from-year 2014" in workflow
    assert '--max-pages "$MAX_PAGES"' in workflow
    assert "--min-request-interval 3" in workflow
    assert "verify_b2_private_bucket.py" in workflow
    assert "checkpoint_capture_state.py" in workflow
    assert "commoncrawl-prefix" in workflow
    assert "discovery.sqlite3.gz" in workflow
    assert "manifest.jsonl.gz" in workflow


def test_catalog_does_not_auto_loop_when_no_page_succeeded() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    dispatch = workflow[workflow.index("Dispatch next bounded run") :]
    assert "steps.discovery.outputs.should_continue == 'true'" in dispatch
    assert "steps.discovery.outputs.pages != '0'" in dispatch
    assert "auto_continue=true" in dispatch
