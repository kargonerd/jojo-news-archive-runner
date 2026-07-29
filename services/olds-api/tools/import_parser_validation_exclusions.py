from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from jojo_olds_api.parser_validation import initialize_parser_validation_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exclude every URL selected by an earlier parser-QA cohort."
    )
    parser.add_argument("--source-state", type=Path, required=True)
    parser.add_argument("--target-state", type=Path, required=True)
    parser.add_argument("--source-cohort", default="validation-v1")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.source_state.exists():
        raise SystemExit(f"source state not found: {args.source_state}")
    args.target_state.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(args.source_state, timeout=60)
    target = sqlite3.connect(args.target_state, timeout=60)
    try:
        initialize_parser_validation_schema(target)
        results_table = source.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='parser_validation_results'
            """
        ).fetchone()
        samples_table = source.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='parser_validation_samples'
            """
        ).fetchone()
        if results_table is not None:
            source_table = "parser_validation_results"
        elif samples_table is not None:
            # Compatibility fallback for a plan-only legacy checkpoint.
            source_table = "parser_validation_samples"
        else:
            raise SystemExit("source state has no parser validation URL table")
        urls = [
            str(row[0])
            for row in source.execute(
                f"SELECT DISTINCT canonical_url FROM {source_table}"
            )
        ]
        now = datetime.now(timezone.utc).isoformat()
        with target:
            target.executemany(
                """
                INSERT INTO parser_validation_exclusions(
                    canonical_url, source_cohort, excluded_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET
                    source_cohort=excluded.source_cohort,
                    excluded_at=excluded.excluded_at
                """,
                ((url, args.source_cohort, now) for url in urls),
            )
        overlap = int(
            target.execute(
                """
                SELECT COUNT(*)
                FROM parser_validation_samples AS sample
                JOIN parser_validation_exclusions AS exclusion USING(canonical_url)
                """
            ).fetchone()[0]
        )
        if overlap:
            with target:
                target.execute(
                    """
                    DELETE FROM parser_validation_results
                    WHERE canonical_url IN (
                        SELECT sample.canonical_url
                        FROM parser_validation_samples AS sample
                        JOIN parser_validation_exclusions AS exclusion
                          USING(canonical_url)
                    )
                    """
                )
                target.execute(
                    """
                    DELETE FROM parser_validation_samples
                    WHERE canonical_url IN (
                        SELECT canonical_url
                        FROM parser_validation_exclusions
                    )
                    """
                )
        remaining_overlap = int(
            target.execute(
                """
                SELECT COUNT(*)
                FROM parser_validation_samples AS sample
                JOIN parser_validation_exclusions AS exclusion USING(canonical_url)
                """
            ).fetchone()[0]
        )
        result = {
            "formatVersion": "jojo-parser-validation-exclusions/1",
            "sourceCohort": args.source_cohort,
            "sourceTable": source_table,
            "sourceSamples": len(urls),
            "excluded": int(
                target.execute(
                    "SELECT COUNT(*) FROM parser_validation_exclusions"
                ).fetchone()[0]
            ),
            "removedSampleOverlap": overlap,
            "sampleOverlap": remaining_overlap,
        }
    finally:
        source.close()
        target.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if remaining_overlap == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
