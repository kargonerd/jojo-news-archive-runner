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
    _has_generic_interface_noise,
    _has_publisher_interface_noise,
    ensure_parser_validation_plan,
    failed_completed_parser_validation_files,
    initialize_parser_validation_schema,
    parser_validation_target_reached,
    parser_validation_summary,
    pending_completed_parser_validation_files,
    pending_parser_validation_urls,
    record_parser_validation,
)


def test_generic_interface_noise_requires_standalone_trending_stories():
    assert _has_generic_interface_noise(["trending stories"])
    assert not _has_generic_interface_noise(
        [
            "many local newsrooms use social networks to monitor "
            "trending stories on social media."
        ]
    )
from jojo_olds_api.raw_archive_capture import (
    completed_raw_capture,
    initialize_capture_schema,
    load_capture_manifest,
    pending_captures,
    record_capture_result,
    store_raw_html,
)


def test_publisher_interface_noise_detects_wsj_promo_sequences():
    assert _has_publisher_interface_noise(
        "wsj",
        [
            "buy side from wsj expert recommendations on products "
            "and services, independent from the wall street journal newsroom."
        ],
    )
    assert _has_publisher_interface_noise(
        "wsj",
        [
            "article reporting",
            "stay informed",
            "get a coronavirus briefing six days a week: sign up here.",
        ],
    )
    assert _has_publisher_interface_noise(
        "wsj",
        [
            "article reporting",
            "free resources",
            "live updates",
            "daily video briefing",
        ],
    )
    assert not _has_publisher_interface_noise(
        "wsj",
        ["the article discussed free resources and live updates."],
    )


def test_publisher_interface_noise_detects_ap_terminal_period_block():
    assert _has_publisher_interface_noise(
        "ap",
        ["substantive article reporting.", "."],
    )
    assert not _has_publisher_interface_noise(
        "ap",
        ["substantive article reporting."],
    )


def test_publisher_interface_noise_detects_bloomberg_promos():
    assert _has_publisher_interface_noise(
        "bloomberg",
        ["article reporting", "related stories:"],
    )
    assert _has_publisher_interface_noise(
        "bloomberg",
        ["watch this next"],
    )
    assert _has_publisher_interface_noise(
        "bloomberg",
        [
            "want to receive this post in your inbox every day? sign up "
            "for the terms of trade newsletter."
        ],
    )
    assert _has_publisher_interface_noise(
        "bloomberg",
        [
            "sign up to receive the green daily newsletter in your "
            "inbox every weekday."
        ],
    )
    assert _has_publisher_interface_noise(
        "bloomberg",
        [
            "for even more: subscribe to bloomberg all access for full "
            "global news coverage."
        ],
    )
    assert _has_publisher_interface_noise(
        "bloomberg",
        [
            "sign up to receive the brexit bulletin, a daily briefing "
            "on britain's departure from the eu."
        ],
    )
    assert not _has_publisher_interface_noise(
        "bloomberg",
        ["investors subscribe to several market-data services."],
    )


def test_publisher_interface_noise_detects_nyt_newsletter_embed():
    assert _has_publisher_interface_noise(
        "nyt",
        [
            "sign up for weekly updates on residential real estate news "
            "from the times."
        ],
    )
    assert not _has_publisher_interface_noise(
        "nyt",
        ["the article describes weekly updates on housing data."],
    )


def test_publisher_interface_noise_detects_reuters_legal_suffixes():
    assert _has_publisher_interface_noise(
        "reuters",
        [
            "article reporting",
            "(c) reuters 2010. all rights reserved. republication or "
            "redistribution ofreuters content is prohibited.",
        ],
    )
    assert _has_publisher_interface_noise(
        "reuters",
        ["copyright 2013, marketwire, all rights reserved."],
    )
    assert not _has_publisher_interface_noise(
        "reuters",
        ["the court reserved all rights while considering the appeal."],
    )


def test_publisher_interface_noise_detects_ft_newsletter_promos():
    assert _has_publisher_interface_noise(
        "ft",
        [
            "how is coronavirus taking its toll on markets? stay "
            "briefed with our coronavirus newsletter"
        ],
    )
    assert _has_publisher_interface_noise(
        "ft",
        [
            "sign up to scoreboard, our new must-read weekly briefing "
            "on the business of sport."
        ],
    )
    assert not _has_publisher_interface_noise(
        "ft",
        ["the article analysed the business of sport."],
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


def _state_with_years(
    tmp_path: Path,
    *,
    publisher: str = "ap",
) -> sqlite3.Connection:
    manifest = tmp_path / "manifest.jsonl"
    rows = []
    for year in (2020, 2021, 2022):
        for suffix in range(10):
            if publisher == "ap":
                canonical_url = (
                    f"https://apnews.com/article/{year}-{suffix}"
                )
            elif publisher == "bloomberg":
                canonical_url = (
                    "https://www.bloomberg.com/news/articles/"
                    f"{year}-01-{suffix + 1:02d}/sample-{suffix}"
                )
            elif publisher == "wsj":
                timestamp = int(
                    datetime(
                        year,
                        1,
                        1,
                        tzinfo=timezone.utc,
                    ).timestamp()
                )
                canonical_url = (
                    "https://www.wsj.com/articles/"
                    f"sample-{suffix}-{timestamp + suffix}"
                )
            else:
                raise AssertionError(f"unsupported fixture: {publisher}")
            candidate = CaptureCandidate(
                provider=CaptureProvider.WAYBACK,
                snapshot_url=(
                    f"https://web.archive.org/web/"
                    f"{year}01010000{suffix:02d}id_/{canonical_url}"
                ),
                captured_at=datetime(year, 1, 1, tzinfo=timezone.utc),
                mime_type="text/html",
                status_code=200,
            )
            rows.append(
                {
                    "publisher": publisher,
                    "canonical_url": canonical_url,
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
        publisher=publisher,
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher=publisher,
    )
    return connection


def test_validation_target_requires_qa_passes_and_keeps_replacement_pending(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path)
    ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2020,
        target_per_year=1,
        reserve_per_year=1,
        maximum_record_attempts=3,
    )
    first_url, second_url = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT canonical_url
            FROM parser_validation_samples
            WHERE sample_year=2020
            ORDER BY sample_priority
            """
        ).fetchall()
    ]
    parser_version = str(
        connection.execute(
            "SELECT parser_version FROM parser_validation_config "
            "WHERE sample_year=2020"
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO parser_validation_results(
            canonical_url, publisher, sample_year, parser_version,
            extraction_status, qa_pass, body_characters, block_count,
            images_referenced, images_selected, duplicate_text_blocks,
            headline_present, published_at_present, source_link_preserved,
            warnings_json, issues_json, error, parsed_at, content_type,
            source_raw_sha256, source_capture_sha256
        ) VALUES (?, 'ap', 2020, ?, 'partial', 0,
                  100, 1, 0, 0, 0, 1, 1, 1, '[]',
                  '["extraction-partial"]', NULL, '2026-01-01T00:00:00+00:00',
                  'article', ?, ?)
        """,
        (first_url, parser_version, "a" * 64, "b" * 64),
    )
    connection.commit()

    assert not parser_validation_target_reached(connection)
    assert pending_parser_validation_urls(
        connection,
        maximum=10,
        maximum_record_attempts=3,
    ) == [second_url]

    replacement = ensure_parser_validation_plan(
        connection,
        publisher="ap",
        from_year=2020,
        to_year=2020,
        target_per_year=1,
        reserve_per_year=1,
        maximum_record_attempts=3,
    )
    assert replacement["years"]["2020"]["qaPassed"] == 0
    assert replacement["years"]["2020"]["addedToPlan"] == 1


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


def test_pending_validation_can_focus_on_one_year(tmp_path: Path):
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

    selected = pending_captures(
        connection,
        retry_errors=False,
        maximum=2,
        maximum_record_attempts=3,
        prioritize_parser_validation=True,
        validation_from_year=2021,
        validation_to_year=2021,
    )

    assert len(selected) == 2
    assert {item.published_at[:4] for item in selected} == {"2021"}


def test_bloomberg_plan_randomly_prefers_exact_wayback_captures(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path, publisher="bloomberg")
    exact_urls: set[str] = set()
    for year in (2020, 2021, 2022):
        rows = connection.execute(
            """
            SELECT canonical_url, candidates_json
            FROM captures
            WHERE published_at >= ? AND published_at < ?
            ORDER BY canonical_url
            LIMIT 3
            """,
            (f"{year}-01-01", f"{year + 1}-01-01"),
        ).fetchall()
        for canonical_url, candidates_json in rows:
            candidates = json.loads(candidates_json)
            candidates.insert(
                0,
                CaptureCandidate(
                    provider=CaptureProvider.WAYBACK,
                    snapshot_url=(
                        f"https://web.archive.org/web/{year}0201000000id_/"
                        f"{canonical_url}"
                    ),
                    captured_at=datetime(
                        year,
                        2,
                        1,
                        tzinfo=timezone.utc,
                    ),
                    digest=f"exact-{year}-{len(exact_urls)}",
                    mime_type="text/html",
                    status_code=200,
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
            exact_urls.add(canonical_url)
    connection.commit()

    plan = ensure_parser_validation_plan(
        connection,
        publisher="bloomberg",
        from_year=2020,
        to_year=2022,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    selected = pending_captures(
        connection,
        retry_errors=False,
        maximum=6,
        maximum_record_attempts=3,
        prioritize_parser_validation=True,
    )

    assert all(item.canonical_url in exact_urls for item in selected)
    assert all(
        plan["years"][str(year)]["addedExactWaybackToPlan"] == 2
        for year in (2020, 2021, 2022)
    )


def test_parser_version_change_excludes_only_evaluated_samples(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path, publisher="bloomberg")
    ensure_parser_validation_plan(
        connection,
        publisher="bloomberg",
        from_year=2020,
        to_year=2020,
        target_per_year=1,
        reserve_per_year=1,
        maximum_record_attempts=3,
    )
    original = {
        str(row[0])
        for row in connection.execute(
        """
        SELECT canonical_url
        FROM parser_validation_samples
        WHERE sample_year=2020
        """
        )
    }
    evaluated = sorted(original)[0]
    connection.execute(
        """
        INSERT INTO parser_validation_results(
            canonical_url,
            publisher,
            sample_year,
            parser_version,
            extraction_status,
            qa_pass,
            warnings_json,
            issues_json,
            parsed_at
        )
        VALUES (?, 'bloomberg', 2020, 'bloomberg-parser/old',
                'complete', 1, '[]', '[]', ?)
        """,
        (evaluated, datetime.now(timezone.utc).isoformat()),
    )
    connection.execute(
        """
        UPDATE parser_validation_config
        SET parser_version='bloomberg-parser/old'
        WHERE sample_year=2020
        """
    )
    connection.commit()

    refreshed = ensure_parser_validation_plan(
        connection,
        publisher="bloomberg",
        from_year=2020,
        to_year=2020,
        target_per_year=1,
        reserve_per_year=1,
        maximum_record_attempts=3,
    )
    replacement = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT canonical_url
            FROM parser_validation_samples
            WHERE sample_year=2020
            """
        )
    }

    assert refreshed["years"]["2020"]["refreshedForParserVersion"] == 1
    assert evaluated not in replacement
    assert (original - {evaluated}) <= replacement
    assert connection.execute(
        """
        SELECT source_cohort
        FROM parser_validation_exclusions
        WHERE canonical_url=?
        """,
        (evaluated,),
    ).fetchone() == ("bloomberg:2020:bloomberg-parser/old",)
    assert connection.execute(
        "SELECT COUNT(*) FROM parser_validation_exclusions"
    ).fetchone() == (1,)


def test_validation_plan_prunes_legacy_reserve_only_exclusions(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path, publisher="bloomberg")
    ensure_parser_validation_plan(
        connection,
        publisher="bloomberg",
        from_year=2020,
        to_year=2020,
        target_per_year=1,
        reserve_per_year=1,
        maximum_record_attempts=3,
    )
    selected = sorted(
        str(row[0])
        for row in connection.execute(
            "SELECT canonical_url FROM parser_validation_samples"
        )
    )
    evaluated, reserve_only = selected
    connection.execute(
        """
        INSERT INTO parser_validation_results(
            canonical_url,
            publisher,
            sample_year,
            parser_version,
            extraction_status,
            qa_pass,
            warnings_json,
            issues_json,
            parsed_at
        )
        VALUES (?, 'bloomberg', 2020, 'bloomberg-parser/old',
                'complete', 1, '[]', '[]', ?)
        """,
        (evaluated, datetime.now(timezone.utc).isoformat()),
    )
    connection.executemany(
        """
        INSERT INTO parser_validation_exclusions(
            canonical_url, source_cohort, excluded_at
        ) VALUES (?, 'bloomberg:2020:bloomberg-parser/old', ?)
        """,
        (
            (url, datetime.now(timezone.utc).isoformat())
            for url in selected
        ),
    )
    connection.commit()

    ensure_parser_validation_plan(
        connection,
        publisher="bloomberg",
        from_year=2020,
        to_year=2020,
        target_per_year=1,
        reserve_per_year=1,
        maximum_record_attempts=3,
    )

    exclusions = {
        str(row[0])
        for row in connection.execute(
            "SELECT canonical_url FROM parser_validation_exclusions"
        )
    }
    assert exclusions == {evaluated}
    assert reserve_only not in exclusions


def test_qa_revision_change_replays_without_replacing_cohort(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path, publisher="wsj")
    ensure_parser_validation_plan(
        connection,
        publisher="wsj",
        from_year=2020,
        to_year=2020,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    original = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT canonical_url
            FROM parser_validation_samples
            WHERE sample_year=2020
            """
        )
    }
    previously_evaluated = sorted(original)[0]
    connection.execute(
        """
        UPDATE parser_validation_config
        SET qa_revision=0
        WHERE sample_year=2020
        """
    )
    connection.execute(
        """
        INSERT INTO parser_validation_results(
            canonical_url,
            publisher,
            sample_year,
            parser_version,
            qa_revision,
            extraction_status,
            qa_pass,
            warnings_json,
            issues_json,
            parsed_at
        )
        VALUES (?, 'wsj', 2020, 'wsj-parser/0.8.45', 0,
                'complete', 1, '[]', '[]', ?)
        """,
        (previously_evaluated, datetime.now(timezone.utc).isoformat()),
    )
    connection.commit()

    refreshed = ensure_parser_validation_plan(
        connection,
        publisher="wsj",
        from_year=2020,
        to_year=2020,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    current = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT canonical_url
            FROM parser_validation_samples
            WHERE sample_year=2020
            """
        )
    }
    pending = set(
        pending_parser_validation_urls(
            connection,
            maximum=None,
            maximum_record_attempts=3,
            from_year=2020,
            to_year=2020,
        )
    )

    assert refreshed["parserVersion"] == "wsj-parser/0.8.45"
    assert refreshed["qaRevision"] == 1
    assert refreshed["years"]["2020"]["evaluated"] == 0
    assert refreshed["years"]["2020"]["refreshedForParserVersion"] == 0
    assert current == original
    assert original <= pending
    assert connection.execute(
        "SELECT COUNT(*) FROM parser_validation_exclusions"
    ).fetchone() == (0,)


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


def test_validation_plan_retries_server_placeholder_before_fresh_sample(
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
        SET status='error', attempts=1,
            last_error='reject-server-placeholder-shell'
        WHERE canonical_url=?
        """,
        (error_url,),
    )
    connection.execute(
        """
        UPDATE parser_validation_samples
        SET sample_priority=CASE canonical_url
            WHEN ? THEN 'ffff'
            ELSE '0000'
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

    assert selected == [error_url]
    assert selected != [pending_url]


def test_validation_plan_prioritizes_high_yield_wsj_snapshots(
    tmp_path: Path,
):
    manifest = tmp_path / "wsj-manifest.jsonl"
    urls = {
        "small": "https://www.wsj.com/articles/small-shell-1472582355",
        "full": "https://www.wsj.com/articles/full-text-1472582356",
        "tpl": "https://www.wsj.com/articles/template-shell-1472582357",
    }
    candidates = {
        "small": CaptureCandidate(
            provider=CaptureProvider.WAYBACK,
            snapshot_url=(
                "https://web.archive.org/web/20160830191501id_/"
                f"{urls['small']}"
            ),
            captured_at=datetime(2016, 8, 30, tzinfo=timezone.utc),
            mime_type="text/html",
            status_code=200,
            byte_count=18_000,
        ),
        "full": CaptureCandidate(
            provider=CaptureProvider.WAYBACK,
            snapshot_url=(
                "https://web.archive.org/web/20160830192758id_/"
                f"{urls['full']}?mod=rss_opinion_main"
            ),
            captured_at=datetime(2016, 8, 30, tzinfo=timezone.utc),
            mime_type="text/html",
            status_code=200,
            byte_count=36_000,
        ),
        "tpl": CaptureCandidate(
            provider=CaptureProvider.WAYBACK,
            snapshot_url=(
                "https://web.archive.org/web/20160830193539id_/"
                f"{urls['tpl']}?tpl=centralbanking"
            ),
            captured_at=datetime(2016, 8, 30, tzinfo=timezone.utc),
            mime_type="text/html",
            status_code=200,
            byte_count=40_000,
        ),
    }
    manifest.write_text(
        "".join(
            json.dumps(
                {
                    "publisher": "wsj",
                    "canonicalUrl": urls[name],
                    "publishedAt": "2016-08-30T00:00:00Z",
                    "candidates": [
                        candidates[name].model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_none=True,
                        )
                    ],
                },
                default=str,
            )
            + "\n"
            for name in ("small", "full", "tpl")
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
        from_year=2016,
        to_year=2016,
        target_per_year=3,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    connection.execute(
        """
        UPDATE parser_validation_samples
        SET sample_priority=CASE canonical_url
            WHEN ? THEN '0000'
            WHEN ? THEN '1111'
            ELSE 'ffff'
        END
        """,
        (urls["small"], urls["tpl"]),
    )
    connection.commit()

    selected = pending_parser_validation_urls(
        connection,
        maximum=3,
        maximum_record_attempts=3,
    )

    assert selected == [urls["full"], urls["small"], urls["tpl"]]


def test_validation_plan_prioritizes_large_modern_wsj_snapshots(
    tmp_path: Path,
):
    manifest = tmp_path / "wsj-modern-manifest.jsonl"
    sizes = {
        "largest": 250_000,
        "large": 150_000,
        "tesla": 80_000,
        "medium": 75_000,
        "legacy_shell": 40_000,
        "tpl": 250_000,
    }
    urls = {
        name: f"https://www.wsj.com/articles/{name}-modern-1580000000"
        for name in sizes
    }
    manifest.write_text(
        "".join(
            json.dumps(
                {
                    "publisher": "wsj",
                    "canonicalUrl": urls[name],
                    "publishedAt": "2020-08-30T00:00:00Z",
                    "candidates": [
                        CaptureCandidate(
                            provider=CaptureProvider.WAYBACK,
                            snapshot_url=(
                                "https://web.archive.org/web/"
                                f"2020083019000{index}id_/{urls[name]}"
                                + (
                                    "?tesla=y"
                                    if name == "tesla"
                                    else (
                                        "?tpl=centralbanking"
                                        if name == "tpl"
                                        else ""
                                    )
                                )
                            ),
                            captured_at=datetime(
                                2020,
                                8,
                                30,
                                tzinfo=timezone.utc,
                            ),
                            mime_type="text/html",
                            status_code=200,
                            byte_count=sizes[name],
                        ).model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_none=True,
                        )
                    ],
                },
                default=str,
            )
            + "\n"
            for index, name in enumerate(sizes)
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
        from_year=2020,
        to_year=2020,
        target_per_year=len(sizes),
        reserve_per_year=0,
        maximum_record_attempts=3,
    )

    selected = pending_parser_validation_urls(
        connection,
        maximum=len(sizes),
        maximum_record_attempts=3,
    )

    assert selected == [
        urls["largest"],
        urls["large"],
        urls["tesla"],
        urls["medium"],
        urls["legacy_shell"],
        urls["tpl"],
    ]


def test_validation_plan_removes_misdated_wsj_samples(
    tmp_path: Path,
):
    manifest = tmp_path / "wsj-misdated-manifest.jsonl"
    wrong_year_url = (
        "https://www.wsj.com/articles/"
        "afghans-mourn-for-bombing-victims-1416846693"
    )
    current_year_url = (
        "https://www.wsj.com/articles/"
        "accenture-looks-to-boost-ai-capabilities-through-"
        "mergers-11592818200"
    )
    manifest.write_text(
        "".join(
            json.dumps(
                {
                    "publisher": "wsj",
                    "canonicalUrl": url,
                    "publishedAt": "2020-06-22T00:00:00+00:00",
                    "candidates": [
                        CaptureCandidate(
                            provider=CaptureProvider.WAYBACK,
                            snapshot_url=(
                                "https://web.archive.org/web/"
                                f"20200622120000id_/{url}"
                            ),
                            captured_at=datetime(
                                2020,
                                6,
                                22,
                                tzinfo=timezone.utc,
                            ),
                            mime_type="text/html",
                            status_code=200,
                            byte_count=40_000,
                        ).model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_none=True,
                        )
                    ],
                },
                default=str,
            )
            + "\n"
            for url in (wrong_year_url, current_year_url)
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
    initialize_parser_validation_schema(connection)
    connection.execute(
        """
        INSERT INTO parser_validation_samples(
            canonical_url, sample_year, sample_priority, selected_at
        ) VALUES (?, 2020, '0000', '2020-01-01T00:00:00+00:00')
        """,
        (wrong_year_url,),
    )
    connection.commit()

    ensure_parser_validation_plan(
        connection,
        publisher="wsj",
        from_year=2020,
        to_year=2020,
        target_per_year=2,
        reserve_per_year=0,
        maximum_record_attempts=3,
    )
    selected = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT canonical_url
            FROM parser_validation_samples
            WHERE sample_year=2020
            """
        )
    }

    assert wrong_year_url not in selected
    assert selected == {current_year_url}


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
            VALUES (2020, 1, 'test', 'nyt-parser/0.8.54', 'now')
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
    assert summary["gates"]["minimumQaPassRate"] == 1.0
    assert summary["ready"] is False


def test_validation_accepts_wsj_business_wire_source_attribution(
    tmp_path: Path,
):
    connection = _state_with_years(tmp_path, publisher="wsj")
    ensure_parser_validation_plan(
        connection,
        publisher="wsj",
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
    body = " ".join(
        ["Substantive archived earnings-release sentence."] * 30
    )
    html = f"""
    <html><head>
      <script type="application/ld+json">{{
        "@type": "NewsArticle",
        "headline": "A complete company earnings release",
        "datePublished": "2020-01-01T00:00:00Z"
      }}</script>
    </head><body><article>
      <p>{body}</p>
      <p>SOURCE: Example Company Copyright Business Wire 2020</p>
    </article></body></html>
    """.encode()
    blob = store_raw_html(tmp_path, html)
    capture = RawCapture(
        article_id=selected.article_id,
        publisher="wsj",
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
    assert result["qaPass"] is True
    assert result["issues"] == []
    assert summary["formatVersion"] == "jojo-parser-validation/2"
    assert summary["years"]["2020"]["qaRevision"] == 1
    assert summary["years"]["2020"]["qaPassed"] == 1
    assert summary["years"]["2020"]["issueCounts"] == {}


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
