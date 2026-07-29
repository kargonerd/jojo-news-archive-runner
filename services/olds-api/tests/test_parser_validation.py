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
    failed_completed_parser_validation_files,
    initialize_parser_validation_schema,
    parser_validation_summary,
    pending_completed_parser_validation_files,
    pending_parser_validation_urls,
    record_parser_validation,
)
from jojo_olds_api.raw_archive_capture import (
    completed_raw_capture,
    initialize_capture_schema,
    load_capture_manifest,
    pending_captures,
    record_capture_result,
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


def test_holdout_plan_excludes_every_prior_cohort_url(tmp_path: Path):
    connection = _state_with_years(tmp_path)
    ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2020,
        target_per_year=4,
        reserve_per_year=0,
        maximum_record_attempts=3,
        seed="first-cohort",
    )
    first_urls = {
        str(row[0])
        for row in connection.execute(
            "SELECT canonical_url FROM parser_validation_samples"
        )
    }
    connection.execute("DELETE FROM parser_validation_samples")
    connection.execute("DELETE FROM parser_validation_config")
    connection.executemany(
        """
        INSERT INTO parser_validation_exclusions(
            canonical_url, source_cohort, excluded_at
        )
        VALUES (?, 'validation-v1', '2026-07-28T00:00:00Z')
        """,
        ((url,) for url in first_urls),
    )
    ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2020,
        target_per_year=4,
        reserve_per_year=0,
        maximum_record_attempts=3,
        seed="holdout-v1",
    )
    holdout_urls = {
        str(row[0])
        for row in connection.execute(
            "SELECT canonical_url FROM parser_validation_samples"
        )
    }
    assert len(holdout_urls) == 4
    assert first_urls.isdisjoint(holdout_urls)


def test_plan_prunes_reuters_non_article_endpoints(tmp_path: Path):
    manifest = tmp_path / "reuters-manifest.jsonl"
    invalid_url = (
        "https://www.reuters.com/article/comments/idUS12320140101"
    )
    malformed_url = (
        "https://www.reuters.com/article/idUSN0927394120090709%7C"
    )
    wrong_year_url = (
        "https://www.reuters.com/article/idUSTRES57D23Q20090816"
    )
    valid_url = "https://www.reuters.com/article/idUS12320140101"
    manifest.write_text(
        "".join(
            json.dumps(
                {
                    "publisher": "reuters",
                    "canonical_url": url,
                    "published_at": "2014-01-01T00:00:00Z",
                    "candidates": [],
                }
            )
            + "\n"
            for url in (invalid_url, valid_url)
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    initialize_capture_schema(
        connection,
        publisher="reuters",
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="reuters",
    )
    initialize_parser_validation_schema(connection)
    connection.executemany(
        """
        INSERT INTO parser_validation_samples(
            canonical_url, sample_year, sample_priority, selected_at
        ) VALUES (?, 2014, '0000', '2026-07-28T00:00:00Z')
        """,
        (
            (invalid_url,),
            (malformed_url,),
            (wrong_year_url,),
            (valid_url,),
        ),
    )
    connection.execute(
        """
        INSERT INTO parser_validation_results(
            canonical_url, publisher, sample_year, parser_version,
            extraction_status, content_type, qa_pass, warnings_json,
            issues_json, parsed_at
        ) VALUES (
            ?, 'reuters', 2014, 'reuters-parser/0.7.0',
            'unsupported', 'article', 0, '[]', '[]',
            '2026-07-28T00:00:00Z'
        )
        """,
        (invalid_url,),
    )

    ensure_parser_validation_plan(
        connection,
        publisher="reuters",
        from_year=2014,
        to_year=2014,
        target_per_year=1,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )

    assert connection.execute(
        """
        SELECT canonical_url FROM parser_validation_samples
        """
    ).fetchall() == [(valid_url,)]
    assert connection.execute(
        """
        SELECT COUNT(*) FROM parser_validation_results
        WHERE canonical_url=?
        """,
        (invalid_url,),
    ).fetchone()[0] == 0


def test_ft_infini_samples_are_added_even_when_random_plan_is_full(
    tmp_path: Path,
):
    manifest = tmp_path / "ft-manifest.jsonl"
    rows = []
    for suffix in range(8):
        canonical_url = (
            "https://www.ft.com/content/"
            f"00000000-0000-0000-0000-{suffix:012d}"
        )
        candidates = [
            CaptureCandidate(
                provider=CaptureProvider.WAYBACK,
                snapshot_url=(
                    "https://web.archive.org/web/20240328000000id_/"
                    + canonical_url
                ),
            )
        ]
        rows.append(
            {
                "publisher": "ft",
                "canonicalUrl": canonical_url,
                "publishedAt": "2024-03-28T00:00:00Z",
                "candidates": [
                    item.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=True,
                    )
                    for item in candidates
                ],
            }
        )
    manifest.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    initialize_capture_schema(
        connection,
        publisher="ft",
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="ft",
    )
    ensure_parser_validation_plan(
        connection,
        publisher="ft",
        from_year=2024,
        to_year=2024,
        target_per_year=4,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    initial_count = connection.execute(
        "SELECT COUNT(*) FROM parser_validation_samples"
    ).fetchone()[0]
    unsampled = connection.execute(
        """
        SELECT capture.canonical_url, capture.candidates_json
        FROM captures AS capture
        LEFT JOIN parser_validation_samples AS sample
          ON sample.canonical_url=capture.canonical_url
        WHERE sample.canonical_url IS NULL
        ORDER BY capture.canonical_url
        LIMIT 2
        """
    ).fetchall()
    for index, (canonical_url, candidates_json) in enumerate(unsampled):
        candidates = json.loads(candidates_json)
        candidates.insert(
            0,
            CaptureCandidate(
                provider=CaptureProvider.INFINI_NEWS,
                snapshot_url=(
                    "https://datasets-server.huggingface.co/rows?"
                    "dataset=ruggsea%2Finfini-news-corpus&"
                    "config=year_2024&split=train&"
                    f"offset={index}&length=1"
                ),
                source_url=canonical_url,
                expected_headline=(
                    f"A complete Financial Times article {index}"
                ),
                warc_filename=(
                    "CC-NEWS-20240328120000-00001.warc.gz"
                ),
            ).model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
        )
        connection.execute(
            """
            UPDATE captures
            SET candidates_json=?
            WHERE canonical_url=?
            """,
            (
                json.dumps(candidates, separators=(",", ":")),
                canonical_url,
            ),
        )
    connection.commit()
    plan = ensure_parser_validation_plan(
        connection,
        publisher="ft",
        from_year=2024,
        to_year=2024,
        target_per_year=4,
        reserve_per_year=2,
        maximum_record_attempts=3,
    )
    direct_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM parser_validation_samples AS sample
        JOIN captures AS capture
          ON capture.canonical_url=sample.canonical_url
        WHERE capture.candidates_json LIKE '%"provider":"infini-news"%'
        """
    ).fetchone()[0]
    pending = pending_parser_validation_urls(
        connection,
        maximum=2,
        maximum_record_attempts=3,
    )

    assert initial_count == 4
    assert plan["years"]["2024"]["addedDirectToPlan"] == 2
    assert direct_count == 2
    assert all(
        "infini-news"
        in connection.execute(
            "SELECT candidates_json FROM captures WHERE canonical_url=?",
            (url,),
        ).fetchone()[0]
        for url in pending
    )


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


def test_validation_plan_expands_reserve_without_replacing_samples(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path)
    ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2022,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    original = {
        str(row[0])
        for row in connection.execute(
            "SELECT canonical_url FROM parser_validation_samples"
        )
    }

    expanded = ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2022,
        target_per_year=2,
        reserve_per_year=3,
        maximum_record_attempts=3,
    )
    expanded_urls = {
        str(row[0])
        for row in connection.execute(
            "SELECT canonical_url FROM parser_validation_samples"
        )
    }

    assert original < expanded_urls
    assert len(original) == 6
    assert len(expanded_urls) == 15
    assert expanded["reservePerYear"] == 3
    assert all(
        year["addedToPlan"] == 3
        for year in expanded["years"].values()
    )


def test_validation_plan_tries_fresh_samples_before_retrying_errors(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path)
    ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2020,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    samples = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT canonical_url
            FROM parser_validation_samples
            WHERE sample_year=2020
            ORDER BY canonical_url
            """
        ).fetchall()
    ]
    error_url, pending_url = samples
    connection.execute(
        """
        UPDATE captures
        SET status='error', attempts=1
        WHERE canonical_url=?
        """,
        (error_url,),
    )
    connection.execute(
        """
        UPDATE parser_validation_samples
        SET sample_priority=CASE canonical_url
            WHEN ? THEN '0000'
            ELSE 'ffff'
        END
        WHERE sample_year=2020
        """,
        (error_url,),
    )
    connection.commit()

    selected = pending_parser_validation_urls(
        connection,
        maximum=1,
        maximum_record_attempts=3,
    )

    assert selected == [pending_url]


def test_validation_plan_can_add_previously_completed_raw_captures(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path)
    connection.execute(
        """
        UPDATE captures
        SET status='complete',
            raw_path='objects/html/aa/already-captured.html.gz',
            raw_sha256=?,
            raw_bytes=1000,
            stored_bytes=500
        WHERE published_at >= '2020-01-01'
          AND published_at < '2021-01-01'
          AND canonical_url != 'https://apnews.com/article/2020-0'
        """,
        ("a" * 64,),
    )
    connection.commit()

    plan = ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2020,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    planned = connection.execute(
        """
        SELECT COUNT(*)
        FROM parser_validation_samples
        WHERE sample_year=2020
        """
    ).fetchone()[0]
    completed_planned = connection.execute(
        """
        SELECT COUNT(*)
        FROM parser_validation_samples AS sample
        JOIN captures AS capture
          ON capture.canonical_url=sample.canonical_url
        WHERE sample.sample_year=2020
          AND capture.status='complete'
          AND capture.raw_path IS NOT NULL
        """
    ).fetchone()[0]

    assert plan["years"]["2020"]["addedCompletedToPlan"] == 2
    assert plan["years"]["2020"]["addedToPlan"] == 2
    assert planned == 2
    assert completed_planned == 2


def test_nyt_parser_upgrade_refreshes_plan_and_prefers_direct_copies(
    tmp_path: Path,
):
    manifest = tmp_path / "nyt-manifest.jsonl"
    rows = []
    for suffix in range(10):
        canonical_url = (
            "https://www.nytimes.com/2026/01/01/world/"
            f"sample-{suffix}.html"
        )
        if suffix < 5:
            candidates = [
                {
                    "provider": "other",
                    "snapshotUrl": (
                        "https://example.com/licensed/"
                        f"sample-{suffix}"
                    ),
                }
            ]
        else:
            candidates = [
                {
                    "provider": "wayback",
                    "snapshotUrl": (
                        "https://web.archive.org/web/20260102000000id_/"
                        + canonical_url
                    ),
                }
            ]
        rows.append(
            {
                "publisher": "nyt",
                "canonicalUrl": canonical_url,
                "publishedAt": "2026-01-01T00:00:00Z",
                "candidates": candidates,
            }
        )
    manifest.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    initialize_capture_schema(
        connection,
        publisher="nyt",
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="nyt",
    )
    ensure_parser_validation_plan(
        connection,
        publisher="nyt",
        from_year=2026,
        to_year=2026,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    connection.execute(
        """
        UPDATE parser_validation_config
        SET parser_version='nyt-parser/0.7.0'
        WHERE sample_year=2026
        """
    )
    connection.execute("DELETE FROM parser_validation_samples")
    connection.executemany(
        """
        INSERT INTO parser_validation_samples(
            canonical_url,
            sample_year,
            sample_priority,
            selected_at
        ) VALUES (?, 2026, ?, '2026-01-01T00:00:00Z')
        """,
        (
            (
                f"https://www.nytimes.com/2026/01/01/world/sample-{suffix}.html",
                f"old-{suffix}",
            )
            for suffix in (8, 9)
        ),
    )
    connection.commit()

    plan = ensure_parser_validation_plan(
        connection,
        publisher="nyt",
        from_year=2026,
        to_year=2026,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    selected = pending_captures(
        connection,
        retry_errors=False,
        maximum=2,
        maximum_record_attempts=3,
        prioritize_parser_validation=True,
    )

    year_plan = plan["years"]["2026"]
    assert year_plan["refreshedForParserVersion"] == 1
    assert year_plan["addedDirectToPlan"] == 2
    assert len(selected) == 2
    assert all(
        item.candidates[0].provider == CaptureProvider.OTHER
        for item in selected
    )


def test_wsj_parser_upgrade_refreshes_plan_and_excludes_asset_urls(
    tmp_path: Path,
):
    valid_url = (
        "https://www.wsj.com/articles/"
        "a-valid-wsj-article-12345678"
    )
    asset_url = (
        "https://www.wsj.com/articles/"
        "B3-BY423_health_PREVIEW_20181003165352.jpg"
    )
    manifest = tmp_path / "wsj-manifest.jsonl"
    manifest.write_text(
        "".join(
            json.dumps(
                {
                    "publisher": "wsj",
                    "canonicalUrl": canonical_url,
                    "publishedAt": "2023-01-01T00:00:00Z",
                    "candidates": [
                        {
                            "provider": "wayback",
                            "snapshotUrl": (
                                "https://web.archive.org/web/"
                                "20230102000000id_/"
                                + canonical_url
                            ),
                        }
                    ],
                }
            )
            + "\n"
            for canonical_url in (valid_url, asset_url)
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    initialize_capture_schema(
        connection,
        publisher="wsj",
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="wsj",
    )
    ensure_parser_validation_plan(
        connection,
        publisher="wsj",
        from_year=2023,
        to_year=2023,
        target_per_year=1,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    connection.execute(
        """
        UPDATE parser_validation_config
        SET parser_version='wsj-parser/0.4.0'
        WHERE sample_year=2023
        """
    )
    connection.execute("DELETE FROM parser_validation_samples")
    connection.execute(
        """
        INSERT INTO parser_validation_samples(
            canonical_url,
            sample_year,
            sample_priority,
            selected_at
        ) VALUES (?, 2023, 'old', '2023-01-01T00:00:00Z')
        """,
        (asset_url,),
    )
    connection.commit()

    plan = ensure_parser_validation_plan(
        connection,
        publisher="wsj",
        from_year=2023,
        to_year=2023,
        target_per_year=1,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    selected = connection.execute(
        """
        SELECT canonical_url
        FROM parser_validation_samples
        WHERE sample_year=2023
        """
    ).fetchall()

    assert plan["years"]["2023"]["refreshedForParserVersion"] == 1
    assert selected == [(valid_url,)]


def test_validation_plan_adds_completed_samples_to_an_existing_pending_plan(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path)
    first = ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2020,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    assert first["years"]["2020"]["addedCompletedToPlan"] == 0
    initially_planned = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT canonical_url
            FROM parser_validation_samples
            WHERE sample_year=2020
            """
        ).fetchall()
    }
    placeholders = ",".join("?" for _ in initially_planned)
    connection.execute(
        f"""
        UPDATE captures
        SET status='complete',
            raw_path='objects/html/aa/already-captured.html.gz',
            raw_sha256=?,
            raw_bytes=1000,
            stored_bytes=500
        WHERE published_at >= '2020-01-01'
          AND published_at < '2021-01-01'
          AND canonical_url NOT IN ({placeholders})
        """,
        ("a" * 64, *sorted(initially_planned)),
    )
    connection.commit()

    second = ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2020,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    completed_planned = connection.execute(
        """
        SELECT COUNT(*)
        FROM parser_validation_samples AS sample
        JOIN captures AS capture
          ON capture.canonical_url=sample.canonical_url
        WHERE sample.sample_year=2020
          AND capture.status='complete'
          AND capture.raw_path IS NOT NULL
        """
    ).fetchone()[0]

    assert second["years"]["2020"]["addedCompletedToPlan"] == 2
    assert second["years"]["2020"]["addedToPlan"] == 2
    assert completed_planned == 2


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
        <article>
          <p>{body}</p>
          <figure>
            <img
              src="https://dims.apnews.com/dims4/default/example.jpg"
              width="1200"
              height="800"
              alt="An editorial test image"
            />
          </figure>
        </article>
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
    result_hashes = connection.execute(
        """
        SELECT source_raw_sha256, source_capture_sha256
        FROM parser_validation_results
        WHERE canonical_url=?
        """,
        (selected.canonical_url,),
    ).fetchone()
    assert result_hashes[0] == blob.sha256
    assert len(result_hashes[1]) == 64
    changed_capture = capture.model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=capture.selected_candidate.snapshot_url,
            )
        }
    )
    record_parser_validation(
        connection,
        capture=changed_capture,
        archive_root=tmp_path,
    )
    changed_hashes = connection.execute(
        """
        SELECT source_raw_sha256, source_capture_sha256
        FROM parser_validation_results
        WHERE canonical_url=?
        """,
        (selected.canonical_url,),
    ).fetchone()
    assert changed_hashes[0] == result_hashes[0]
    assert changed_hashes[1] != result_hashes[1]
    assert summary["years"]["2020"]["evaluated"] == 1
    assert summary["years"]["2020"]["complete"] == 1
    assert summary["years"]["2020"]["qaPassed"] == 1
    assert summary["years"]["2020"]["planned"] == 1
    assert summary["years"]["2020"]["imagesReferenced"] == 1
    assert summary["years"]["2020"]["imagesSelected"] == 1
    assert summary["years"]["2020"]["articlesWithImagesReferenced"] == 1
    assert summary["years"]["2020"]["articlesWithImagesSelected"] == 1
    assert summary["years"]["2020"]["imageSelectionRate"] == 1.0
    assert summary["years"]["2020"]["issueCounts"] == {}
    assert summary["years"]["2020"]["failureExamples"] == []


def test_nontext_interactive_is_not_a_false_article_body_failure(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.nytimes.com/interactive/2020/10/25/"
        "us/politics/example.html"
    )
    connection = sqlite3.connect(":memory:")
    initialize_parser_validation_schema(connection)
    connection.execute(
        """
        INSERT INTO parser_validation_config(
            sample_year, target_size, seed, parser_version, updated_at
        )
            VALUES (2020, 1, 'test', 'nyt-parser/0.8.25', 'now')
        """
    )
    connection.execute(
        """
        INSERT INTO parser_validation_samples(
            canonical_url, sample_year, sample_priority, selected_at
        )
        VALUES (?, 2020, 'priority', 'now')
        """,
        (canonical_url,),
    )
    html = b"""
    <html>
      <head>
        <meta property="og:title" content="Interactive election result">
        <meta property="article:published_time"
              content="2020-10-25T00:00:00Z">
      </head>
      <body><main data-interactive-root="result"></main></body>
    </html>
    """
    blob = store_raw_html(tmp_path, html)
    capture = RawCapture(
        article_id="nyt:" + ("a" * 64),
        publisher="nyt",
        canonical_url=canonical_url,
        published_at=datetime(2020, 10, 25, tzinfo=timezone.utc),
        selected_candidate=CaptureCandidate(
            provider=CaptureProvider.WAYBACK,
            snapshot_url=(
                "https://web.archive.org/web/20201026000000id_/"
                + canonical_url
            ),
        ),
        retrieved_at=datetime.now(timezone.utc),
        final_url=canonical_url,
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

    assert result["status"] == "unsupported"
    assert result["qaPass"] is True
    assert result["issues"] == []
    assert summary["years"]["2020"]["nonTextContent"] == 1
    assert summary["years"]["2020"]["qaPassed"] == 1
    assert summary["years"]["2020"]["unsupported"] == 1


def test_validation_rejects_interface_noise_inside_complete_body(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path)
    ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2020,
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
    body = " ".join(["Substantive archived reporting sentence."] * 30)
    html = f"""
    <html><head>
      <script type="application/ld+json">{{
        "@type": "NewsArticle",
        "headline": "A report contaminated by a recommendation module",
        "datePublished": "2020-01-01T00:00:00Z"
      }}</script>
    </head><body><article>
      <p>{body}</p>
      <aside><p>From Around the Web Promoted by Taboola</p></aside>
    </article></body></html>
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

    assert result["status"] == "complete"
    assert result["qaPass"] is False
    assert result["issues"] == ["interface-noise-in-body"]
    assert summary["years"]["2020"]["issueCounts"] == {
        "interface-noise-in-body": 1
    }


def test_validation_uses_parsed_publication_year_not_capture_year(
    tmp_path: Path,
):
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
    body = " ".join(["Cross-year reporting sentence."] * 30)
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">
          {{
            "@type": "NewsArticle",
            "headline": "A cross-year archived article",
            "datePublished": "2021-06-15T00:00:00Z"
          }}
        </script>
      </head>
      <body><article><p>{body}</p></article></body>
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
    stored_year = connection.execute(
        """
        SELECT sample_year
        FROM parser_validation_results
        WHERE canonical_url=?
        """,
        (selected.canonical_url,),
    ).fetchone()[0]

    assert result["plannedYear"] == 2020
    assert result["year"] == 2021
    assert stored_year == 2021


def test_completed_sample_can_be_replayed_from_capture_state(tmp_path: Path):
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
    body = " ".join(["Replayable reporting sentence."] * 40)
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">
          {{
            "@type": "NewsArticle",
            "headline": "A replayed archived article",
            "datePublished": "2020-01-01T00:00:00Z"
          }}
        </script>
      </head>
      <body><article><p>{body}</p></article></body>
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
    record_capture_result(
        connection,
        {
            "canonicalUrl": selected.canonical_url,
            "status": "complete",
            "capture": capture,
            "recordPath": None,
            "error": None,
        },
    )

    pending = pending_completed_parser_validation_files(
        connection,
        maximum=10,
    )
    restored = completed_raw_capture(
        connection,
        canonical_url=selected.canonical_url,
    )
    result = record_parser_validation(
        connection,
        capture=restored,
        archive_root=tmp_path,
    )

    assert pending == [(selected.canonical_url, blob.path)]
    assert restored == capture
    assert result["qaPass"] is True
    assert pending_completed_parser_validation_files(
        connection,
        maximum=10,
    ) == []
    connection.execute(
        """
        UPDATE parser_validation_results
        SET qa_pass=0
        WHERE canonical_url=?
        """,
        (selected.canonical_url,),
    )
    assert failed_completed_parser_validation_files(
        connection,
        maximum=10,
    ) == [(selected.canonical_url, blob.path)]
    connection.execute(
        """
        UPDATE captures
        SET raw_sha256=?
        WHERE canonical_url=?
        """,
        ("b" * 64, selected.canonical_url),
    )
    initialize_parser_validation_schema(connection)
    assert connection.execute(
        """
        SELECT COUNT(*)
        FROM parser_validation_results
        WHERE canonical_url=?
        """,
        (selected.canonical_url,),
    ).fetchone()[0] == 0
