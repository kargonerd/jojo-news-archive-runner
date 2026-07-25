from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or report a resumable raw-capture Actions batch."
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--max-record-attempts", type=int, default=3)
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def action_state(
    state_path: Path,
    *,
    maximum_record_attempts: int,
) -> dict[str, object]:
    if maximum_record_attempts < 1:
        raise ValueError("maximum_record_attempts must be positive")
    if not state_path.exists():
        return {
            "stateExists": False,
            "capturesByStatus": {},
            "retryErrors": False,
            "actionable": 1,
            "terminalUnresolved": 0,
            "shouldContinue": True,
        }
    connection = sqlite3.connect(
        f"file:{state_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    try:
        counts = dict(
            connection.execute(
                "SELECT status, COUNT(*) FROM captures GROUP BY status"
            ).fetchall()
        )
        recoverable = connection.execute(
            """
            SELECT COUNT(*)
            FROM captures
            WHERE status='error' AND attempts < ?
            """,
            (maximum_record_attempts,),
        ).fetchone()[0]
        validation_replays = 0
        validation_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                  AND name IN (
                    'parser_validation_config',
                    'parser_validation_samples',
                    'parser_validation_results'
                  )
                """
            ).fetchall()
        }
        if len(validation_tables) == 3:
            validation_replays = int(
                connection.execute(
                    """
                    WITH active_years AS (
                        SELECT
                            config.sample_year,
                            config.target_size,
                            config.parser_version
                        FROM parser_validation_config AS config
                        LEFT JOIN parser_validation_results AS result
                          ON result.sample_year=config.sample_year
                         AND result.parser_version=config.parser_version
                        GROUP BY
                            config.sample_year,
                            config.target_size,
                            config.parser_version
                        HAVING COUNT(result.canonical_url)
                             < config.target_size
                    )
                    SELECT COUNT(*)
                    FROM parser_validation_samples AS sample
                    JOIN active_years
                      ON active_years.sample_year=sample.sample_year
                    JOIN captures AS capture
                      ON capture.canonical_url=sample.canonical_url
                    LEFT JOIN parser_validation_results AS result
                      ON result.canonical_url=sample.canonical_url
                     AND result.parser_version=active_years.parser_version
                    WHERE result.canonical_url IS NULL
                      AND capture.status='complete'
                      AND capture.raw_path IS NOT NULL
                    """
                ).fetchone()[0]
            )
    finally:
        connection.close()
    pending = counts.get("pending", 0)
    downloading = counts.get("downloading", 0)
    unresolved = counts.get("error", 0)
    actionable = pending + downloading + recoverable + validation_replays
    return {
        "stateExists": True,
        "capturesByStatus": counts,
        "retryErrors": pending == 0 and downloading == 0 and recoverable > 0,
        "actionable": actionable,
        "validationReplays": validation_replays,
        "terminalUnresolved": max(0, unresolved - recoverable),
        "shouldContinue": actionable > 0,
    }


def write_github_output(path: Path, result: dict[str, object]) -> None:
    values = {
        "retry_errors": str(bool(result["retryErrors"])).lower(),
        "should_continue": str(bool(result["shouldContinue"])).lower(),
        "actionable": str(result["actionable"]),
        "terminal_unresolved": str(result["terminalUnresolved"]),
        "validation_replays": str(result.get("validationReplays", 0)),
    }
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    args = parse_args()
    result = action_state(
        args.state,
        maximum_record_attempts=args.max_record_attempts,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.github_output:
        write_github_output(args.github_output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
