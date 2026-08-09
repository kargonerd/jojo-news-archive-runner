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

from jojo_olds_api.archive_sources import archive_source_spec
from jojo_olds_api.common_crawl_prefix_manifest import (
    CommonCrawlPrefixClient,
    export_prefix_manifest,
    initialize_prefix_schema,
    next_prefix_query,
    prefix_summary,
    record_prefix_error,
    record_prefix_page,
    record_prefix_page_count,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a resumable Common Crawl prefix manifest for archive "
            "URLs missing from the primary catalog."
        )
    )
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--from-year", type=int, required=True)
    parser.add_argument("--to-year", type=int, required=True)
    parser.add_argument("--collection-from-year", type=int, default=2012)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--max-errors", type=int, default=3)
    parser.add_argument("--min-request-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.from_year > args.to_year:
        raise SystemExit("--from-year must not be after --to-year")
    if args.max_pages < 1 or args.max_errors < 1:
        raise SystemExit("--max-pages and --max-errors must be positive")
    spec = archive_source_spec(args.publisher)
    client = CommonCrawlPrefixClient(
        minimum_interval=args.min_request_interval,
        timeout=args.timeout,
        attempts=args.attempts,
    )
    args.state.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.state, timeout=60)
    pages = 0
    errors = 0
    try:
        collections = tuple(
            collection
            for collection in client.collections()
            if collection.to_at.year >= args.collection_from_year
            and collection.from_at
            <= datetime.now(timezone.utc)
        )
        initialize_prefix_schema(
            connection,
            spec=spec,
            from_year=args.from_year,
            to_year=args.to_year,
            collections=collections,
        )
        while pages < args.max_pages and errors < args.max_errors:
            query = next_prefix_query(connection)
            if query is None:
                break
            collection_id, index_url, pattern, total_pages, next_page = query
            try:
                if total_pages is None:
                    total_pages = client.page_count(
                        index_url=index_url,
                        pattern=pattern,
                    )
                    record_prefix_page_count(
                        connection,
                        collection_id=collection_id,
                        pattern=pattern,
                        total_pages=total_pages,
                    )
                    if total_pages == 0:
                        continue
                page = client.page(
                    index_url=index_url,
                    pattern=pattern,
                    page=next_page,
                )
                result = record_prefix_page(
                    connection,
                    spec=spec,
                    collection_id=collection_id,
                    pattern=pattern,
                    page_number=next_page,
                    total_pages=total_pages,
                    page=page,
                )
                pages += 1
                print(
                    json.dumps(
                        {
                            "event": "common-crawl-prefix-page",
                            "collection": collection_id,
                            "pattern": pattern,
                            "page": next_page,
                            **result,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:
                errors += 1
                record_prefix_error(
                    connection,
                    collection_id=collection_id,
                    pattern=pattern,
                    error=f"{type(exc).__name__}: {exc}",
                )
                print(
                    json.dumps(
                        {
                            "event": "common-crawl-prefix-error",
                            "collection": collection_id,
                            "pattern": pattern,
                            "error": type(exc).__name__,
                        }
                    ),
                    flush=True,
                )
        manifest = export_prefix_manifest(
            connection,
            spec=spec,
            destination=args.output,
        )
        summary = {
            **prefix_summary(connection),
            "pagesThisRun": pages,
            "errorsThisRun": errors,
            "manifest": manifest,
        }
        if args.summary is not None:
            args.summary.parent.mkdir(parents=True, exist_ok=True)
            args.summary.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        if args.github_output is not None:
            with args.github_output.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"should_continue={str(summary['shouldContinue']).lower()}\n"
                )
                handle.write(f"pages={pages}\n")
                handle.write(f"errors={errors}\n")
        return 0
    finally:
        connection.close()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
