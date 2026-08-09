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


def test_wayback_discovery_retries_across_bounded_runs() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    wayback_section = workflow[
        workflow.index('elif [ "$MANIFEST_MODE" = "wayback-urlkey" ]; then')
        : workflow.index("else", workflow.index('elif [ "$MANIFEST_MODE" = "wayback-urlkey" ]; then'))
    ]

    assert "--timeout 30" in wayback_section
    assert "--attempts 2" in wayback_section
    assert "runner slot for the command defaults (90 seconds x 6)" in wayback_section


def test_archive_continuation_drains_actionable_captures_before_discovery() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    continuation_section = workflow[
        workflow.index("- name: Dispatch next bounded run") :
    ]

    assert 'actionable="${{ steps.after.outputs.actionable }}"' in continuation_section
    assert 'if [[ "$actionable" =~ ^[1-9][0-9]*$ ]]; then' in continuation_section
    assert "next_discovery_pages=0" in continuation_section
    assert '-f max_discovery_pages="$next_discovery_pages"' in continuation_section


def test_validation_only_archive_chain_releases_runner_at_ready_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    capture_section = workflow[
        workflow.index("- name: Capture bounded raw HTML batch") :
        workflow.index("- name: Checkpoint capture state")
    ]
    continuation_section = workflow[
        workflow.index("- name: Dispatch next bounded run") :
    ]

    assert "stop_when_validation_ready:" in workflow
    assert "--stop-when-validation-ready" in capture_section
    assert "--stop-when-validation-target-reached" in capture_section
    assert "steps.after.outputs.validation_ready != 'true'" in continuation_section
    assert (
        '-f stop_when_validation_ready="${{ inputs.stop_when_validation_ready }}"'
        in continuation_section
    )
