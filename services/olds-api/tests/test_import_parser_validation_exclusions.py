from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys


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
