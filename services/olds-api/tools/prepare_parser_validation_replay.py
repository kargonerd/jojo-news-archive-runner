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
    DEFAULT_SEED,
    ensure_parser_validation_plan,
    failed_completed_parser_validation_files,
    pending_completed_parser_validation_files,
)
from jojo_olds_api.raw_archive_capture import initialize_capture_schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan parser validation and list previously captured raw objects "
            "that must be restored for the current parser version."
        )
    )
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--from-year", type=int, required=True)
    parser.add_argument("--to-year", type=int, required=True)
    parser.add_argument("--target-per-year", type=int, default=500)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--reserve-per-year", type=int)
    parser.add_argument("--max-record-attempts", type=int, default=3)
    parser.add_argument("--max-replays", type=int, default=500)
    parser.add_argument("--files-from", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_replays < 1:
        raise SystemExit("--max-replays must be positive")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.state, timeout=60)
    initialize_capture_schema(
        connection,
        publisher=args.publisher,
        authorization_reference="user-provided-authorization",
    )
    plan = ensure_parser_validation_plan(
        connection,
        publisher=args.publisher,
        from_year=args.from_year,
        to_year=args.to_year,
        target_per_year=args.target_per_year,
        reserve_per_year=args.reserve_per_year,
        maximum_record_attempts=args.max_record_attempts,
        seed=args.seed,
    )
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
    connection.close()

    args.files_from.parent.mkdir(parents=True, exist_ok=True)
    args.files_from.write_text(
        "".join(f"{raw_path}\n" for _, raw_path in pending),
        encoding="utf-8",
    )
    result = {
        "publisher": args.publisher,
        "parserVersion": plan["parserVersion"],
        "replays": len(pending),
        "filesFrom": str(args.files_from),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"replays={len(pending)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
