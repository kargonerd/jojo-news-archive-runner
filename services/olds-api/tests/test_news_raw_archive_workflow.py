from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "news-raw-archive.yml"


def test_live_raw_checkpoints_cannot_block_archive_workers() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    catalog_section = workflow[
        workflow.index("live_catalog_checkpoint() {")
        : workflow.index("run_catalog() {")
    ]
    capture_section = workflow[
        workflow.index("live_checkpoint() {")
        : workflow.index("request_interval=0.5")
    ]

    assert "timeout 30 rclone copyto" in catalog_section
    assert "--timeout 20s --contimeout 10s" in catalog_section
    assert capture_section.count("timeout 120 rclone copy") == 2
    assert capture_section.count("timeout 30 rclone copyto") == 3
    assert "Live object upload failed; state withheld." in capture_section
    assert "Live capture-state upload timed out." in capture_section
