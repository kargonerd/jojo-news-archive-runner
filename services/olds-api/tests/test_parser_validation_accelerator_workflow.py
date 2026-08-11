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


def test_accelerator_uses_all_original_validation_states_as_exclusions() -> None:
    workflow = _workflow_text()
    restore_section = workflow[
        workflow.index("Restore filtered manifest and validation checkpoint")
        : workflow.index(
            '"${SOURCE_ROOT}/state/completed-captures.sqlite3.gz"'
        )
    ]

    assert (
        '"$RUNNER_TEMP/exclusion-validation-v2.sqlite3.gz"'
        in restore_section
    )
    assert '"${SOURCE_ROOT}/state/capture.sqlite3.gz"' in restore_section
    assert (
        '"$RUNNER_TEMP/exclusion-validation-v1.sqlite3.gz"'
        in restore_section
    )
    assert '"${LEGACY_SOURCE_ROOT}/state/capture.sqlite3.gz"' in restore_section
    assert (
        '"$RUNNER_TEMP/exclusion-validation-legacy.sqlite3.gz"'
        in restore_section
    )
    assert (
        'if [ ! -f "$RUNNER_TEMP/exclusion-validation-v1.sqlite3.gz" ]'
        not in restore_section
    )
    import_section = workflow[
        workflow.index("Import original-cohort exclusions")
        : workflow.index("Seed validation from source archive")
    ]
    assert '--sample-year "$SAMPLE_YEAR"' in import_section


def test_accelerator_does_not_silently_relax_exclusions() -> None:
    workflow = _workflow_text()

    assert "relax_parser_validation_exclusions" not in workflow


def test_completed_holdout_requires_union_rotation_audit_before_publish() -> None:
    workflow = _workflow_text()
    audit = workflow[
        workflow.index("Audit completed holdout rotation")
        : workflow.index("Checkpoint validation state")
    ]

    assert "audit_parser_validation_holdout.py" in audit
    assert '("$RUNNER_TEMP"/exclusion-*.sqlite3)' in audit
    assert '--previous-state "${label}=${previous_state}"' in audit
    assert "--require-complete" in audit
    assert '--target-per-year "$VALIDATION_TARGET"' in audit
    assert 'outputs.validation_ready == \'true\'' in audit
    assert "rotation-audit.json" in audit
    assert 'PYTHONPATH: ${{ github.workspace }}/services/olds-api' in workflow


def test_rotation_audit_failure_blocks_checkpoint_publish_and_chaining() -> None:
    workflow = _workflow_text()

    assert workflow.count(
        "steps.rotation_readiness.outcome != 'failure'"
    ) == 4
    assert workflow.count(
        "steps.rotation_audit.outcome == 'success'"
    ) == 4
    publish = workflow[
        workflow.index("Publish validation objects and checkpoint")
        : workflow.index("Report validation state")
    ]
    assert '"${REMOTE_ROOT}/state/rotation-audit.json"' in publish


def test_accelerator_enables_archive_fallbacks_for_ft_wsj_and_nikkei() -> None:
    workflow = _workflow_text()

    fallback_section = workflow[
        workflow.index('if [ "$PUBLISHER" = "ft" ] ||') :
        workflow.index("set +e", workflow.index('if [ "$PUBLISHER" = "ft" ] ||'))
    ]
    assert '[ "$PUBLISHER" = "wsj" ] ||' in fallback_section
    assert '[ "$PUBLISHER" = "nikkei" ]; then' in fallback_section
    assert "--enable-arquivo-pt-fallback" in workflow
    assert "--enable-common-crawl-fallback" in workflow


def test_accelerator_preindexes_bounded_wsj_arquivo_catalog_nonfatally() -> None:
    workflow = _workflow_text()
    section = workflow[
        workflow.index("Pre-index WSJ Arquivo.pt prefix candidates")
        : workflow.index("Pre-index validated FT mirror candidates")
    ]

    assert "inputs.publisher == 'wsj'" in section
    assert "preindex_arquivo_pt_catalog.py" in section
    assert '--year "$SAMPLE_YEAR"' in section
    assert '--state "$LOCAL_ROOT/raw/capture.sqlite3"' in section
    assert "if ! python" in section
    assert "continuing with exact URL fallbacks" in section


def test_accelerator_retains_existing_content_addressed_raw_objects() -> None:
    workflow = _workflow_text()

    assert "--checksum --ignore-existing" in workflow
    assert "--checksum --immutable" not in workflow


def test_live_checkpoint_uploads_are_bounded() -> None:
    workflow = _workflow_text()
    checkpoint_section = workflow[
        workflow.index("live_checkpoint() {")
        : workflow.index("request_interval=0.5")
    ]

    assert checkpoint_section.count("timeout 120 rclone copy") == 2
    assert checkpoint_section.count("timeout 30 rclone copyto") == 2
    assert "--timeout 30s --contimeout 10s" in checkpoint_section
    assert "Live object upload failed; state withheld." in checkpoint_section


def test_accelerator_reads_wsj_legacy_raw_without_copying_it() -> None:
    workflow = _workflow_text()

    assert 'legacy_source_root="${B2_REMOTE}:${B2_ARCHIVE_BUCKET}/news-archive/v1/wsj/2016-2026/wayback-urlkey"' in workflow
    assert 'LEGACY_SOURCE_ROOT: ${{ steps.paths.outputs.legacy_source_root }}' in workflow
    assert "merge_archive_manifests.py" in workflow
    assert '"${LEGACY_SOURCE_ROOT}/catalog/manifest.jsonl.gz"' in workflow
    assert '"${LEGACY_SOURCE_ROOT}/state/completed-captures.sqlite3.gz"' in workflow
    assert '"${LEGACY_SOURCE_ROOT}/raw"' in workflow
    assert '"$RUNNER_TEMP/legacy-source-import-files.txt"' in workflow
    assert workflow.count(
        '--exclude-from "$RUNNER_TEMP/restored-object-excludes.txt"'
    ) == 2


def test_accelerator_merges_npr_common_crawl_supplemental_manifest() -> None:
    workflow = _workflow_text()

    assert 'if [ "$PUBLISHER" = "npr" ]; then' in workflow
    assert "commoncrawl-prefix" in workflow
    assert (
        'SUPPLEMENTAL_SOURCE_ROOT: '
        '${{ steps.paths.outputs.supplemental_source_root }}'
    ) in workflow
    assert (
        '"${SUPPLEMENTAL_SOURCE_ROOT}/catalog/manifest.jsonl.gz"'
        in workflow
    )
    assert '--input "$supplemental_source_manifest"' in workflow


def test_accelerator_merges_axios_common_crawl_supplemental_manifest() -> None:
    workflow = _workflow_text()

    assert 'elif [ "$PUBLISHER" = "axios" ]; then' in workflow
    assert (
        "news-archive/v1/axios/${source_window}/commoncrawl-prefix"
        in workflow
    )


def test_accelerator_merges_nikkei_common_crawl_supplemental_manifest() -> None:
    workflow = _workflow_text()

    assert 'elif [ "$PUBLISHER" = "nikkei" ]; then' in workflow
    assert (
        "news-archive/v1/nikkei/${source_window}/commoncrawl-prefix"
        in workflow
    )
    assert (
        '"${SUPPLEMENTAL_SOURCE_ROOT}/catalog/manifest.jsonl.gz"'
        in workflow
    )
    assert (
        '"${SUPPLEMENTAL_SOURCE_ROOT}/catalog/manifest.jsonl.gz"'
        in workflow
    )


def test_accelerator_merges_caixin_single_year_common_crawl_manifest() -> None:
    workflow = _workflow_text()

    assert 'elif [ "$PUBLISHER" = "caixin" ]; then' in workflow
    assert (
        "news-archive/v1/caixin/${SAMPLE_YEAR}-${SAMPLE_YEAR}/"
        "commoncrawl-prefix"
        in workflow
    )
    assert (
        '"${SUPPLEMENTAL_SOURCE_ROOT}/catalog/manifest.jsonl.gz"'
        in workflow
    )


def test_accelerator_merges_ap_legacy_supplemental_manifest() -> None:
    workflow = _workflow_text()

    assert 'elif [ "$PUBLISHER" = "ap" ]; then' in workflow
    assert "news-archive/v1/ap/${source_window}/legacy-archive" in workflow
    assert (
        '"${SUPPLEMENTAL_SOURCE_ROOT}/catalog/manifest.jsonl.gz"'
        in workflow
    )
    assert '--input "$supplemental_source_manifest"' in workflow
    assert (
        '"${SUPPLEMENTAL_SOURCE_ROOT}/catalog/'
        'wayback-yahoo-manifest.jsonl.gz"'
        in workflow
    )
    assert '--input "$ap_wayback_yahoo_manifest"' in workflow
    assert (
        '"${SUPPLEMENTAL_SOURCE_ROOT}/catalog/'
        'wayback-bigstory-manifest.jsonl.gz"'
        in workflow
    )
    assert '--input "$ap_wayback_bigstory_manifest"' in workflow
