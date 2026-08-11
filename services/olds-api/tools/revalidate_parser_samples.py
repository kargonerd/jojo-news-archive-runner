from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from jojo_olds_api.parser_validation import (
    failed_completed_parser_validation_files,
    parser_validation_summary,
    pending_completed_parser_validation_files,
    record_parser_validation,
)
from jojo_olds_api.raw_archive_capture import (
    capture_summary,
    completed_capture_rejection_reason,
    completed_raw_capture,
    reset_completed_capture_for_retry,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay the current parser against restored deterministic sample "
            "HTML without redownloading it from the source archive."
        )
    )
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--max-replays", type=int, default=500)
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--force-existing",
        action="store_true",
        help=(
            "Reparse existing validation results whose completed raw object "
            "is present locally. This is an end-of-run reproducibility gate."
        ),
    )
    return parser.parse_args()


def forced_replay_candidates(
    connection: sqlite3.Connection,
    *,
    archive_root: Path,
    maximum: int,
) -> tuple[list[str], list[tuple[str, str]]]:
    rows = connection.execute(
        """
        SELECT result.canonical_url, capture.raw_path
        FROM parser_validation_results AS result
        JOIN parser_validation_samples AS sample
          ON sample.canonical_url=result.canonical_url
        JOIN captures AS capture
          ON capture.canonical_url=result.canonical_url
         AND capture.status='complete'
        ORDER BY sample.sample_priority, result.canonical_url
        LIMIT ?
        """,
        (maximum,),
    ).fetchall()
    replayable: list[str] = []
    missing: list[tuple[str, str]] = []
    for canonical_url, raw_path in rows:
        if raw_path and (archive_root / str(raw_path)).is_file():
            replayable.append(str(canonical_url))
        else:
            missing.append(
                (
                    str(canonical_url),
                    str(raw_path)
                    if raw_path
                    else f"<missing raw_path for {canonical_url}>",
                )
            )
    return replayable, missing


def requeue_missing_validation_capture(
    connection: sqlite3.Connection,
    *,
    canonical_url: str,
) -> None:
    reset_completed_capture_for_retry(
        connection,
        canonical_url=canonical_url,
        reason="validation-raw-object-missing",
    )
    with connection:
        connection.execute(
            "DELETE FROM parser_validation_results WHERE canonical_url=?",
            (canonical_url,),
        )


def main() -> int:
    args = parse_args()
    if args.max_replays < 1 or args.progress_every < 1:
        raise SystemExit("--max-replays and --progress-every must be positive")
    connection = sqlite3.connect(args.state, timeout=60)
    forced = 0
    missing: list[str] = []
    requeued = 0
    if args.force_existing:
        replayable, missing_entries = forced_replay_candidates(
            connection,
            archive_root=args.archive_root,
            maximum=args.max_replays,
        )
        with connection:
            connection.executemany(
                """
                DELETE FROM parser_validation_results
                WHERE canonical_url=?
                """,
                ((canonical_url,) for canonical_url in replayable),
            )
        for canonical_url, raw_path in missing_entries:
            requeue_missing_validation_capture(
                connection,
                canonical_url=canonical_url,
            )
            missing.append(raw_path)
            requeued += 1
        forced = len(replayable)
    pending = pending_completed_parser_validation_files(
        connection,
        maximum=args.max_replays,
    )
    failed = failed_completed_parser_validation_files(
        connection,
        maximum=max(0, args.max_replays - len(pending)),
    )
    pending.extend(
        row for row in failed if row[0] not in {item[0] for item in pending}
    )
    processed = 0
    parser_errors = 0
    for canonical_url, raw_path in pending:
        if not (args.archive_root / raw_path).is_file():
            missing.append(raw_path)
            requeue_missing_validation_capture(
                connection,
                canonical_url=canonical_url,
            )
            requeued += 1
            continue
        capture = completed_raw_capture(
            connection,
            canonical_url=canonical_url,
        )
        if capture.publisher != args.publisher:
            raise ValueError(
                f"capture publisher {capture.publisher!r} does not match "
                f"{args.publisher!r}"
            )
        rejection_reason = completed_capture_rejection_reason(
            capture,
            archive_root=args.archive_root,
        )
        if rejection_reason:
            reset_completed_capture_for_retry(
                connection,
                canonical_url=canonical_url,
                reason=rejection_reason,
            )
            with connection:
                connection.execute(
                    """
                    DELETE FROM parser_validation_results
                    WHERE canonical_url=?
                    """,
                    (canonical_url,),
                )
            requeued += 1
            continue
        result = record_parser_validation(
            connection,
            capture=capture,
            archive_root=args.archive_root,
        )
        processed += 1
        parser_errors += result["error"] is not None
        if processed % args.progress_every == 0:
            print(
                json.dumps(
                    {
                        "event": "parser-validation-replay",
                        "processed": processed,
                        "parserErrors": parser_errors,
                        "requeued": requeued,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    summary = parser_validation_summary(connection)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.summary.with_suffix(args.summary.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                capture_summary(
                    connection,
                    output_dir=args.archive_root,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(args.summary)
    connection.close()
    result = {
        "publisher": args.publisher,
        "forced": forced,
        "requested": len(pending),
        "processed": processed,
        "parserErrors": parser_errors,
        "requeued": requeued,
        "missingRawObjects": len(missing),
        "ready": summary["ready"],
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # Missing content is a recoverable archive-state defect. The rows above
    # have been reset to pending and their stale parser results removed, so a
    # successful exit lets the workflow checkpoint and dispatch a repair
    # capture instead of leaving a permanently green-but-unreplayable cohort.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
