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
    parser_validation_summary,
    pending_completed_parser_validation_files,
    record_parser_validation,
)
from jojo_olds_api.raw_archive_capture import completed_raw_capture
from jojo_olds_api.raw_archive_capture import capture_summary


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_replays < 1 or args.progress_every < 1:
        raise SystemExit("--max-replays and --progress-every must be positive")
    connection = sqlite3.connect(args.state, timeout=60)
    pending = pending_completed_parser_validation_files(
        connection,
        maximum=args.max_replays,
    )
    processed = 0
    parser_errors = 0
    missing: list[str] = []
    for canonical_url, raw_path in pending:
        if not (args.archive_root / raw_path).is_file():
            missing.append(raw_path)
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
        "requested": len(pending),
        "processed": processed,
        "parserErrors": parser_errors,
        "missingRawObjects": len(missing),
        "ready": summary["ready"],
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if missing:
        examples = ", ".join(missing[:5])
        raise RuntimeError(
            f"{len(missing)} validation raw objects were not restored: "
            f"{examples}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
