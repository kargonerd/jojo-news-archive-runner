from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

import httpx


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from jojo_olds_api.archive_sources import archive_source_spec
from jojo_olds_api.wayback_manifest import (
    WaybackCDXClient,
    discovery_summary,
    export_capture_manifest,
    initialize_discovery_schema,
    initialize_wsj_bluesky_schema,
    initialize_wsj_google_news_schema,
    initialize_wsj_rss_schema,
    next_discovery_query,
    process_wsj_bluesky_page,
    process_wsj_google_news_feed,
    process_wsj_rss_feeds,
    record_discovery_page,
    wsj_bluesky_should_continue,
    wsj_google_news_should_continue,
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
    parser.add_argument(
        "--collapse",
        choices=("digest", "urlkey"),
        default="digest",
        help="CDX deduplication key; urlkey is the fast unique-URL mode.",
    )
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
        collapse=args.collapse,
    )
    bluesky_pages_this_run = 0
    google_news_items_this_run = 0
    deferred_errors: list[str] = []
    if args.publisher == "wsj" and args.collapse == "urlkey":
        initialize_wsj_bluesky_schema(connection)
        initialize_wsj_google_news_schema(connection)
        initialize_wsj_rss_schema(connection)
        with httpx.Client(
            headers={
                "User-Agent": (
                    "JOJO-News-Archive-Research/0.1 "
                    "(nonprofit academic archive; contact via repository)"
                )
            },
            follow_redirects=True,
            timeout=args.timeout,
        ) as http_client:
            if wsj_google_news_should_continue(
                connection,
                from_year=args.from_year,
                to_year=args.to_year,
            ):
                try:
                    google_news_result = process_wsj_google_news_feed(
                        connection,
                        spec=spec,
                        http_client=http_client,
                        from_year=args.from_year,
                        to_year=args.to_year,
                    )
                    google_news_items_this_run = int(
                        google_news_result["decodesAttempted"]
                    )
                    google_news_errors = google_news_result.pop("errors")
                    deferred_errors.extend(
                        f"WSJ Google News: {error}"
                        for error in google_news_errors
                    )
                    print(
                        json.dumps(
                            {
                                "event": "wsj-google-news-poll",
                                **google_news_result,
                                "errors": len(google_news_errors),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                except Exception as exc:
                    deferred_errors.append(
                        "WSJ Google News: "
                        f"{type(exc).__name__}: {exc}"
                    )
            rss_result = process_wsj_rss_feeds(
                connection,
                spec=spec,
                http_client=http_client,
                from_year=args.from_year,
                to_year=args.to_year,
            )
            rss_errors = rss_result.pop("errors")
            deferred_errors.extend(
                f"WSJ RSS: {error}" for error in rss_errors
            )
            print(
                json.dumps(
                    {
                        "event": "wsj-rss-poll",
                        **rss_result,
                        "errors": len(rss_errors),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            while (
                args.max_pages is None
                or bluesky_pages_this_run < args.max_pages
            ) and wsj_bluesky_should_continue(
                connection,
                from_year=args.from_year,
                to_year=args.to_year,
            ):
                try:
                    result = process_wsj_bluesky_page(
                        connection,
                        spec=spec,
                        http_client=http_client,
                        from_year=args.from_year,
                        to_year=args.to_year,
                    )
                except Exception as exc:
                    deferred_errors.append(
                        f"WSJ Bluesky: {type(exc).__name__}: {exc}"
                    )
                    break
                bluesky_pages_this_run += 1
                print(
                    json.dumps(
                        {
                            "event": "wsj-bluesky-page",
                            "page": bluesky_pages_this_run,
                            **result,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if args.min_request_interval:
                    time.sleep(args.min_request_interval)
    client = WaybackCDXClient(
        minimum_interval=args.min_request_interval,
        timeout=args.timeout,
        attempts=args.attempts,
        page_limit=args.page_limit,
        collapse=args.collapse,
    )
    pages_this_run = 0
    deferred_error = None
    try:
        while (
            args.max_pages is None
            or bluesky_pages_this_run + pages_this_run < args.max_pages
        ):
            query = next_discovery_query(connection)
            if query is None:
                break
            pattern, resume_key = query
            try:
                page = client.fetch_page(
                    pattern=pattern,
                    from_year=args.from_year,
                    to_year=args.to_year,
                    resume_key=resume_key,
                )
            except RuntimeError as exc:
                deferred_error = str(exc)
                deferred_errors.append(deferred_error)
                print(
                    json.dumps(
                        {
                            "event": "discovery-deferred",
                            "publisher": args.publisher,
                            "pattern": pattern,
                            "error": deferred_error,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                break
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
        "blueskyPagesThisRun": bluesky_pages_this_run,
        "googleNewsItemsThisRun": google_news_items_this_run,
        "deferredError": (
            "; ".join(deferred_errors) if deferred_errors else None
        ),
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
            handle.write(
                "capture_ready="
                f"{str(bool(summary['captureReady'])).lower()}\n"
            )
            handle.write(f"articles={summary['articles']}\n")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
