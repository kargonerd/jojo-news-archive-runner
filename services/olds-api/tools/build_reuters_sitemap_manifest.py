from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from jojo_olds_api.bloomberg_archive_download import ArchiveClient
from jojo_olds_api.reuters_sitemap_manifest import (
    discover_reuters_sitemap_captures,
    export_reuters_manifest,
    initialize_reuters_sitemap_schema,
    pending_reuters_sitemaps,
    process_reuters_sitemap,
    reuters_sitemap_summary,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover Reuters article URLs from archived rolling sitemaps."
    )
    parser.add_argument("--publisher", choices=["reuters"], default="reuters")
    parser.add_argument("--from-year", type=int, default=2021)
    parser.add_argument("--to-year", type=int, default=2026)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--max-sitemaps", type=int, default=25)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--min-request-interval", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.from_year > args.to_year:
        raise SystemExit("--from-year must not be after --to-year")
    if args.max_sitemaps < 1 or args.max_attempts < 1:
        raise SystemExit("--max-sitemaps and --max-attempts must be positive")
    state = args.state or args.output.with_suffix(".sqlite3")
    state.parent.mkdir(parents=True, exist_ok=True)
    captures = discover_reuters_sitemap_captures(
        from_year=args.from_year,
        to_year=args.to_year,
        timeout=args.timeout,
    )
    connection = sqlite3.connect(state, timeout=60)
    initialize_reuters_sitemap_schema(
        connection,
        from_year=args.from_year,
        to_year=args.to_year,
        captures=captures,
    )
    pending = pending_reuters_sitemaps(
        connection,
        maximum=args.max_sitemaps,
        maximum_attempts=args.max_attempts,
    )
    archive_client = ArchiveClient(
        timeout=args.timeout,
        minimum_interval=args.min_request_interval,
        attempts=args.attempts,
    )
    processed = 0
    errors = 0
    try:
        for snapshot_url, _ in pending:
            result = process_reuters_sitemap(
                connection,
                snapshot_url=snapshot_url,
                archive_client=archive_client,
                from_year=args.from_year,
                to_year=args.to_year,
            )
            processed += 1
            errors += result["status"] == "error"
            print(
                json.dumps(
                    {
                        "event": "reuters-sitemap",
                        "processedThisRun": processed,
                        "errorsThisRun": errors,
                        **result,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        archive_client.close()
    manifest = export_reuters_manifest(
        connection,
        destination=args.output,
        from_year=args.from_year,
        to_year=args.to_year,
        maximum_attempts=args.max_attempts,
    )
    summary = {
        **reuters_sitemap_summary(connection),
        **manifest,
        "state": str(state),
        "sitemapsThisRun": processed,
        "errorsThisRun": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(
                f"should_continue={str(bool(summary['shouldContinue'])).lower()}\n"
            )
            handle.write(
                f"complete={str(bool(summary['complete'])).lower()}\n"
            )
            handle.write(f"articles={summary['articles']}\n")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
