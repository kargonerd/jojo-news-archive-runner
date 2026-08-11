from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import subprocess
import sys

from jojo_olds_api.parser_validation import initialize_parser_validation_schema


TOOL = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "import_parser_validation_exclusions.py"
)


def test_imports_only_urls_actually_evaluated_by_prior_cohort(
    tmp_path: Path,
):
    source_path = tmp_path / "source.sqlite3"
    target_path = tmp_path / "target.sqlite3"
    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE parser_validation_samples (
            canonical_url TEXT PRIMARY KEY
        );
        CREATE TABLE parser_validation_results (
            canonical_url TEXT PRIMARY KEY
        );
        INSERT INTO parser_validation_samples VALUES
            ('https://apnews.com/article/evaluated'),
            ('https://apnews.com/article/reserve-only');
        INSERT INTO parser_validation_results VALUES
            ('https://apnews.com/article/evaluated');
        """
    )
    source.commit()
    source.close()

    result = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--source-state",
            str(source_path),
            "--target-state",
            str(target_path),
            "--source-cohort",
            "holdout-v1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"sourceTable": "parser_validation_results"' in result.stdout

    target = sqlite3.connect(target_path)
    urls = {
        str(row[0])
        for row in target.execute(
            "SELECT canonical_url FROM parser_validation_exclusions"
        )
    }
    target.close()
    assert urls == {"https://apnews.com/article/evaluated"}


def test_removes_existing_samples_that_overlap_new_exclusions(
    tmp_path: Path,
):
    source_path = tmp_path / "source.sqlite3"
    target_path = tmp_path / "target.sqlite3"
    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE parser_validation_results (
            canonical_url TEXT PRIMARY KEY
        );
        INSERT INTO parser_validation_results VALUES
            ('https://reuters.com/article/overlap');
        """
    )
    source.commit()
    source.close()
    target = sqlite3.connect(target_path)
    initialize_parser_validation_schema(target)
    target.execute(
        """
        INSERT INTO parser_validation_samples(
            canonical_url, sample_year, sample_priority, selected_at
        )
        VALUES (?, 2012, 'priority', 'now')
        """,
        ("https://reuters.com/article/overlap",),
    )
    target.commit()
    target.close()

    result = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--source-state",
            str(source_path),
            "--target-state",
            str(target_path),
            "--source-cohort",
            "validation-v1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["removedSampleOverlap"] == 1
    assert payload["sampleOverlap"] == 0
    target = sqlite3.connect(target_path)
    assert target.execute(
        "SELECT COUNT(*) FROM parser_validation_samples"
    ).fetchone()[0] == 0
    target.close()


def test_can_limit_imported_exclusions_to_one_sample_year(tmp_path: Path):
    source_path = tmp_path / "source.sqlite3"
    target_path = tmp_path / "target.sqlite3"
    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE parser_validation_results (
            canonical_url TEXT PRIMARY KEY,
            sample_year INTEGER NOT NULL
        );
        INSERT INTO parser_validation_results VALUES
            ('https://www.ft.com/content/from-2016', 2016),
            ('https://www.ft.com/content/from-2017', 2017);
        """
    )
    source.commit()
    source.close()

    result = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--source-state",
            str(source_path),
            "--target-state",
            str(target_path),
            "--source-cohort",
            "ft:2016:ft-parser/0.8.29",
            "--sample-year",
            "2016",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["sampleYear"] == 2016
    assert payload["sourceSamples"] == 1
    target = sqlite3.connect(target_path)
    exclusions = target.execute(
        "SELECT canonical_url, source_cohort "
        "FROM parser_validation_exclusions"
    ).fetchall()
    target.close()
    assert exclusions == [
        (
            "https://www.ft.com/content/from-2016",
            "ft:2016:ft-parser/0.8.29",
        )
    ]


def test_normalizes_caixin_page_variants_before_excluding(tmp_path: Path):
    source_path = tmp_path / "source.sqlite3"
    target_path = tmp_path / "target.sqlite3"
    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE parser_validation_results (
            canonical_url TEXT PRIMARY KEY
        );
        INSERT INTO parser_validation_results VALUES
            ('https://magazine.caixin.com/2010-02-07/100116568_all.html'),
            ('https://magazine.caixin.com/2010-02-07/100116568_2.html');
        """
    )
    source.commit()
    source.close()

    result = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--source-state",
            str(source_path),
            "--target-state",
            str(target_path),
            "--source-cohort",
            "preflight-v1",
            "--publisher",
            "caixin",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["sourceSamples"] == 1
    target = sqlite3.connect(target_path)
    exclusions = target.execute(
        "SELECT canonical_url FROM parser_validation_exclusions"
    ).fetchall()
    target.close()
    assert exclusions == [
        ("https://magazine.caixin.com/2010-02-07/100116568.html",)
    ]


def test_inherits_transitive_exclusions_from_prior_validation_state(
    tmp_path: Path,
):
    source_path = tmp_path / "source.sqlite3"
    target_path = tmp_path / "target.sqlite3"
    source = sqlite3.connect(source_path)
    source.executescript(
        """
        CREATE TABLE parser_validation_results (
            canonical_url TEXT PRIMARY KEY,
            sample_year INTEGER NOT NULL
        );
        CREATE TABLE parser_validation_exclusions (
            canonical_url TEXT PRIMARY KEY,
            source_cohort TEXT NOT NULL,
            excluded_at TEXT NOT NULL
        );
        INSERT INTO parser_validation_results VALUES
            ('https://magazine.caixin.com/2010-01-01/evaluated.html', 2010);
        INSERT INTO parser_validation_exclusions VALUES
            ('https://magazine.caixin.com/2010-01-02/preflight.html',
             'preflight-v1', 'now');
        """
    )
    source.commit()
    source.close()

    result = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--source-state",
            str(source_path),
            "--target-state",
            str(target_path),
            "--source-cohort",
            "validation-v1",
            "--publisher",
            "caixin",
            "--sample-year",
            "2010",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["sourceSamples"] == 2
    assert payload["evaluatedSourceSamples"] == 1
    assert payload["inheritedSourceExclusions"] == 1
    target = sqlite3.connect(target_path)
    exclusions = {
        row[0]: row[1]
        for row in target.execute(
            "SELECT canonical_url, source_cohort "
            "FROM parser_validation_exclusions"
        )
    }
    target.close()
    assert exclusions == {
        "https://magazine.caixin.com/2010-01-01/evaluated.html": (
            "validation-v1"
        ),
        "https://magazine.caixin.com/2010-01-02/preflight.html": (
            "preflight-v1"
        ),
    }


def test_direct_cohort_import_repairs_stale_inherited_label(tmp_path: Path):
    inherited_path = tmp_path / "inherited.sqlite3"
    direct_path = tmp_path / "direct.sqlite3"
    target_path = tmp_path / "target.sqlite3"
    inherited = sqlite3.connect(inherited_path)
    inherited.executescript(
        """
        CREATE TABLE parser_validation_results (
            canonical_url TEXT PRIMARY KEY,
            sample_year INTEGER NOT NULL
        );
        CREATE TABLE parser_validation_exclusions (
            canonical_url TEXT PRIMARY KEY,
            source_cohort TEXT NOT NULL,
            excluded_at TEXT NOT NULL
        );
        INSERT INTO parser_validation_results VALUES
            ('https://magazine.caixin.com/2010-01-02/new.html', 2010);
        INSERT INTO parser_validation_exclusions VALUES
            ('https://magazine.caixin.com/2010-01-01/old.html',
             'wrong-later-cohort', 'now');
        """
    )
    inherited.commit()
    inherited.close()
    direct = sqlite3.connect(direct_path)
    direct.executescript(
        """
        CREATE TABLE parser_validation_results (
            canonical_url TEXT PRIMARY KEY,
            sample_year INTEGER NOT NULL
        );
        INSERT INTO parser_validation_results VALUES
            ('https://magazine.caixin.com/2010-01-01/old.html', 2010);
        """
    )
    direct.commit()
    direct.close()

    for source_path, source_cohort in (
        (inherited_path, "holdout-v2"),
        (direct_path, "holdout-v1"),
    ):
        subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "--source-state",
                str(source_path),
                "--target-state",
                str(target_path),
                "--source-cohort",
                source_cohort,
                "--publisher",
                "caixin",
                "--sample-year",
                "2010",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    target = sqlite3.connect(target_path)
    exclusions = dict(
        target.execute(
            "SELECT canonical_url, source_cohort "
            "FROM parser_validation_exclusions"
        )
    )
    target.close()
    assert exclusions == {
        "https://magazine.caixin.com/2010-01-01/old.html": "holdout-v1",
        "https://magazine.caixin.com/2010-01-02/new.html": "holdout-v2",
    }
