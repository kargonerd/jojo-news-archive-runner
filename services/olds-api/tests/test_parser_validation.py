from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from jojo_olds_api.news_models import (
    CaptureCandidate,
    CaptureProvider,
    RawCapture,
)
from jojo_olds_api.parser_validation import (
    ensure_parser_validation_plan,
    parser_validation_summary,
    record_parser_validation,
)
from jojo_olds_api.raw_archive_capture import (
    initialize_capture_schema,
    load_capture_manifest,
    pending_captures,
    store_raw_html,
)


def _capture_candidate(year: int, suffix: int) -> CaptureCandidate:
    return CaptureCandidate(
        provider=CaptureProvider.WAYBACK,
        snapshot_url=(
            f"https://web.archive.org/web/{year}01010000{suffix:02d}id_/"
            f"https://apnews.com/article/{year}-{suffix}"
        ),
        captured_at=datetime(year, 1, 1, tzinfo=timezone.utc),
        mime_type="text/html",
        status_code=200,
    )


def _state_with_years(tmp_path: Path) -> sqlite3.Connection:
    manifest = tmp_path / "manifest.jsonl"
    rows = []
    for year in (2020, 2021, 2022):
        for suffix in range(10):
            candidate = _capture_candidate(year, suffix)
            rows.append(
                {
                    "publisher": "ap",
                    "canonical_url": f"https://apnews.com/article/{year}-{suffix}",
                    "published_at": f"{year}-01-01T00:00:00Z",
                    "candidates": [
                        candidate.model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_none=True,
                        )
                    ],
                }
            )
    manifest.write_text(
        "".join(json.dumps(row, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    initialize_capture_schema(
        connection,
        publisher="ap",
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="ap",
    )
    return connection


def test_validation_plan_is_random_reproducible_and_balanced(tmp_path: Path):
    first = _state_with_years(tmp_path)
    plan = ensure_parser_validation_plan(
        first,
        publisher="ap",
        from_year=2020,
        to_year=2022,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    selected = pending_captures(
        first,
        retry_errors=False,
        maximum=6,
        maximum_record_attempts=3,
        prioritize_parser_validation=True,
    )
    selected_urls = [item.canonical_url for item in selected]

    second = _state_with_years(tmp_path)
    ensure_parser_validation_plan(
        second,
        publisher="ap",
        from_year=2020,
        to_year=2022,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    repeated_urls = [
        item.canonical_url
        for item in pending_captures(
            second,
            retry_errors=False,
            maximum=6,
            maximum_record_attempts=3,
            prioritize_parser_validation=True,
        )
    ]

    assert plan["targetPerYear"] == 2
    assert selected_urls == repeated_urls
    assert len(selected_urls) == 6
    assert [item.published_at[:4] for item in selected] == [
        "2020",
        "2021",
        "2022",
        "2020",
        "2021",
        "2022",
    ]
    assert selected_urls != [
        f"https://apnews.com/article/{year}-{suffix}"
        for suffix in range(2)
        for year in (2020, 2021, 2022)
    ]


def test_completed_validation_sample_records_parser_quality(tmp_path: Path):
    connection = _state_with_years(tmp_path)
    ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2022,
        target_per_year=1,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    selected = pending_captures(
        connection,
        retry_errors=False,
        maximum=1,
        maximum_record_attempts=3,
        prioritize_parser_validation=True,
    )[0]
    body = " ".join(["Substantive reporting sentence."] * 30)
    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <script type="application/ld+json">
          {{
            "@type": "NewsArticle",
            "headline": "A complete archived article",
            "datePublished": "2020-01-01T00:00:00Z"
          }}
        </script>
      </head>
      <body>
        <article><p>{body}</p></article>
      </body>
    </html>
    """.encode()
    blob = store_raw_html(tmp_path, html)
    capture = RawCapture(
        article_id=selected.article_id,
        publisher="ap",
        canonical_url=selected.canonical_url,
        published_at=datetime.fromisoformat(selected.published_at),
        selected_candidate=selected.candidates[0],
        candidates_considered=list(selected.candidates),
        retrieved_at=datetime.now(timezone.utc),
        final_url=selected.candidates[0].snapshot_url,
        http_status=200,
        content_type="text/html",
        quality_score=100,
        raw_html=blob,
    )

    result = record_parser_validation(
        connection,
        capture=capture,
        archive_root=tmp_path,
    )
    summary = parser_validation_summary(connection)

    assert result["sample"] is True
    assert result["status"] == "complete"
    assert result["qaPass"] is True
    assert summary["years"]["2020"]["evaluated"] == 1
    assert summary["years"]["2020"]["complete"] == 1
    assert summary["years"]["2020"]["qaPassed"] == 1
