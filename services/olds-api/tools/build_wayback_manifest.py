from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from jojo_olds_api.archive_sources import archive_source_spec
from jojo_olds_api.wayback_manifest import (
    WaybackCDXClient,
    discovery_summary,
    export_capture_manifest,
    initialize_discovery_schema,
    next_discovery_query,
    record_discovery_page,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a resumable raw-capture manifest from Wayback CDX."
    )
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--from-year", type=int, default=2016)
    parser.add_argument("--to-year", type=int, default=2026)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--page-limit", type=int, default=10_000)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--min-request-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.from_year > args.to_year:
        raise SystemExit("--from-year must not be after --to-year")
    if args.page_limit < 1:
        raise SystemExit("--page-limit must be positive")
    spec = archive_source_spec(args.publisher)
    state = args.state or args.output.with_suffix(".sqlite3")
    state.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state, timeout=60)
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=args.from_year,
        to_year=args.to_year,
    )
    client = WaybackCDXClient(
        minimum_interval=args.min_request_interval,
        timeout=args.timeout,
        attempts=args.attempts,
        page_limit=args.page_limit,
    )
    pages_this_run = 0
    try:
        while args.max_pages is None or pages_this_run < args.max_pages:
            query = next_discovery_query(connection)
            if query is None:
                break
            pattern, resume_key = query
            page = client.fetch_page(
                pattern=pattern,
                from_year=args.from_year,
                to_year=args.to_year,
                resume_key=resume_key,
            )
            result = record_discovery_page(
                connection,
                spec=spec,
                pattern=pattern,
                page=page,
            )
            pages_this_run += 1
            print(
                json.dumps(
                    {
                        "event": "discovery-page",
                        "publisher": args.publisher,
                        "pattern": pattern,
                        "page": pages_this_run,
                        **result,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        client.close()

    manifest = export_capture_manifest(
        connection,
        spec=spec,
        destination=args.output,
        from_year=args.from_year,
        to_year=args.to_year,
    )
    summary = {
        **discovery_summary(connection),
        **manifest,
        "state": str(state),
        "pagesThisRun": pages_this_run,
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
