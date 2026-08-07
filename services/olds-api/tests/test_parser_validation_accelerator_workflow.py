from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "parser-validation-accelerator.yml"
)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_accelerator_uses_every_available_prior_holdout_exclusion() -> None:
    workflow = _workflow_text()

    assert 'seq $((cohort_number - 1)) -1 1' in workflow
    assert "Using ${previous_cohort} as a holdout exclusion." in workflow
    exclusion_section = workflow[
        workflow.index('if [ "$cohort_number" -gt 1 ]; then')
        : workflow.index('"${SOURCE_ROOT}/state/completed-captures.sqlite3.gz"')
    ]
    assert "break" not in exclusion_section


def test_accelerator_never_uses_source_capture_as_validation_exclusion() -> None:
    workflow = _workflow_text()
    restore_section = workflow[
        workflow.index("Restore filtered manifest and validation checkpoint")
        : workflow.index(
            '"${SOURCE_ROOT}/state/completed-captures.sqlite3.gz"'
        )
    ]

    assert '"${SOURCE_ROOT}/state/capture.sqlite3.gz"' not in restore_section


def test_accelerator_does_not_silently_relax_exclusions() -> None:
    workflow = _workflow_text()

    assert "relax_parser_validation_exclusions" not in workflow
