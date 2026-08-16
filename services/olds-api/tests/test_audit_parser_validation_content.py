from __future__ import annotations

import sqlite3

import pytest

from tools.audit_parser_validation_content import (
    _INTERFACE_TEXT_RE,
    _NYT_DEAD_INTERACTIVE_CONTROL_RE,
    _suspicious_selected_image,
    image_identity,
    nyt_raw_interactive_prose_characters,
    normalize_text,
    selected_validation_urls,
    url_year_mismatch,
)


def test_normalizes_text_and_image_identity() -> None:
    assert normalize_text("  Hello\n WORLD ") == "hello world"
    assert image_identity("HTTPS://IMG.EXAMPLE/a.jpg?width=1200#x") == (
        "https://img.example/a.jpg"
    )


def test_interface_text_detector_does_not_match_ordinary_prose() -> None:
    assert _INTERFACE_TEXT_RE.search("subscribe") is not None
    assert _INTERFACE_TEXT_RE.search("Related") is not None
    assert _INTERFACE_TEXT_RE.search("RSS") is not None
    assert _INTERFACE_TEXT_RE.search("The reports are closely related.") is None
    assert _INTERFACE_TEXT_RE.search("subscribe to our daily newsletter") is not None
    assert _INTERFACE_TEXT_RE.search("terms of use") is not None
    assert _INTERFACE_TEXT_RE.search("Download the app") is not None
    assert _INTERFACE_TEXT_RE.search("Download our app today!") is not None
    assert _INTERFACE_TEXT_RE.search(
        "Download the app and sign in for the deal."
    ) is None
    assert _INTERFACE_TEXT_RE.search("01 第1页 02 第2页") is not None
    assert _INTERFACE_TEXT_RE.search(
        "MarketWatch拥有位于三大洲的100多名记者，为世界各地读者提供新闻。"
    ) is not None
    assert _INTERFACE_TEXT_RE.search(
        "The court considered whether violating the terms of use was illegal."
    ) is None
    assert _INTERFACE_TEXT_RE.search(
        "Terms and Conditions in free software says certain provisions can be "
        "ignored in the case of a widespread viral infection."
    ) is None
    assert _INTERFACE_TEXT_RE.search(
        "Kafka users can publish data streams or subscribe to them in real time."
    ) is None
    assert _NYT_DEAD_INTERACTIVE_CONTROL_RE.fullmatch("Read full answer")
    assert _NYT_DEAD_INTERACTIVE_CONTROL_RE.fullmatch("Next: Another Candidate")
    assert not _NYT_DEAD_INTERACTIVE_CONTROL_RE.fullmatch(
        "The next section explains the result."
    )


def test_measures_unique_raw_nyt_interactive_prose() -> None:
    paragraph = "A detailed reported paragraph with useful context. " * 12
    html = (
        "<div class='interactive-graphic'><p>"
        + paragraph
        + "</p><p>"
        + paragraph
        + "</p></div>"
    ).encode()

    assert nyt_raw_interactive_prose_characters(
        html,
        "https://www.nytimes.com/interactive/2019/example.html",
    ) == len(normalize_text(paragraph))
    assert nyt_raw_interactive_prose_characters(
        html,
        "https://www.nytimes.com/2019/example.html",
    ) == 0


def test_suspicious_image_detector_distinguishes_movie_from_user_avatar() -> None:
    assert _suspicious_selected_image(
        "https://media.example/authors/default-avatar.png"
    )
    assert not _suspicious_selected_image(
        "https://media.npr.org/assets/movies/2009/12/avatar/"
        "humanandavatar2-f44c267a.jpg"
    )
    assert not _suspicious_selected_image(
        "https://media.npr.org/assets/blogs/13.7/images/2009/12/"
        "avatar-blue_wide.jpg"
    )
    assert _suspicious_selected_image(
        "https://www.ft.com/__assets/creatives/brand-ft/icons/"
        "v2/open-graph.png"
    )
    assert not _suspicious_selected_image(
        "https://media.npr.org/assets/img/2015/12/10/"
        "transparent_204_00647_wide-4703ef85e0e4fa056c3f19b9204070d151fa2584.jpg"
    )
    assert not _suspicious_selected_image(
        "https://media.npr.org/assets/bakertaylor/covers/i/"
        "icon/9781481425155_custom-8876aa5ad899201e6e17f23bd38d750f4ea8f0ea-s1200.jpg"
    )


def test_url_year_mismatch_detects_misdated_nyt_interactive() -> None:
    assert url_year_mismatch(
        "nyt",
        "https://www.nytimes.com/interactive/2016/obituaries/notable-deaths/x",
        2018,
    ) == 2016
    assert url_year_mismatch(
        "nyt",
        "https://www.nytimes.com/interactive/2018/world/example.html",
        2018,
    ) is None
    assert not _suspicious_selected_image(
        "https://media.npr.org/assets/news/2010/02/19/"
        "logo_custom-3257db8ff3898e2259e954abba1d1a766a03f557.jpg"
    )


def test_selects_only_active_qa_passing_complete_sample() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE parser_validation_config (
          sample_year INTEGER PRIMARY KEY,
          target_size INTEGER NOT NULL,
          parser_version TEXT NOT NULL,
          qa_revision INTEGER NOT NULL
        );
        CREATE TABLE parser_validation_samples (
          canonical_url TEXT PRIMARY KEY,
          sample_year INTEGER NOT NULL,
          sample_priority TEXT NOT NULL
        );
        CREATE TABLE parser_validation_results (
          canonical_url TEXT PRIMARY KEY,
          publisher TEXT NOT NULL,
          sample_year INTEGER NOT NULL,
          parser_version TEXT NOT NULL,
          qa_revision INTEGER NOT NULL,
          qa_pass INTEGER NOT NULL
        );
        CREATE TABLE captures (
          canonical_url TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          raw_path TEXT
        );
        INSERT INTO parser_validation_config VALUES (2010, 2, 'caixin-parser/1', 3);
        INSERT INTO parser_validation_samples VALUES
          ('https://example.test/b', 2010, '02'),
          ('https://example.test/a', 2010, '01'),
          ('https://example.test/rejected', 2010, '00');
        INSERT INTO parser_validation_results VALUES
          ('https://example.test/b', 'caixin', 2010, 'caixin-parser/1', 3, 1),
          ('https://example.test/a', 'caixin', 2010, 'caixin-parser/1', 3, 1),
          ('https://example.test/rejected', 'caixin', 2010, 'caixin-parser/1', 3, 0);
        INSERT INTO captures VALUES
          ('https://example.test/b', 'complete', 'objects/b.gz'),
          ('https://example.test/a', 'complete', 'objects/a.gz'),
          ('https://example.test/rejected', 'complete', 'objects/r.gz');
        """
    )
    version, revision, urls = selected_validation_urls(
        connection,
        publisher="caixin",
        year=2010,
        target=2,
    )
    assert version == "caixin-parser/1"
    assert revision == 3
    assert urls == ["https://example.test/a", "https://example.test/b"]


def test_selects_sample_from_checkpoint_before_qa_revisions() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE parser_validation_config (
          sample_year INTEGER PRIMARY KEY,
          target_size INTEGER NOT NULL,
          parser_version TEXT NOT NULL
        );
        CREATE TABLE parser_validation_samples (
          canonical_url TEXT PRIMARY KEY,
          sample_year INTEGER NOT NULL,
          sample_priority TEXT NOT NULL
        );
        CREATE TABLE parser_validation_results (
          canonical_url TEXT PRIMARY KEY,
          publisher TEXT NOT NULL,
          sample_year INTEGER NOT NULL,
          parser_version TEXT NOT NULL,
          qa_pass INTEGER NOT NULL
        );
        CREATE TABLE captures (
          canonical_url TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          raw_path TEXT
        );
        INSERT INTO parser_validation_config
          VALUES (2018, 1, 'nyt-parser/legacy');
        INSERT INTO parser_validation_samples
          VALUES ('https://example.test/nyt', 2018, '01');
        INSERT INTO parser_validation_results VALUES (
          'https://example.test/nyt', 'nyt', 2018,
          'nyt-parser/legacy', 1
        );
        INSERT INTO captures VALUES (
          'https://example.test/nyt', 'complete', 'objects/nyt.gz'
        );
        """
    )

    version, revision, urls = selected_validation_urls(
        connection, publisher="nyt", year=2018, target=1
    )

    assert version == "nyt-parser/legacy"
    assert revision == 0
    assert urls == ["https://example.test/nyt"]


def test_rejects_incomplete_target() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE parser_validation_config (
          sample_year INTEGER PRIMARY KEY,
          target_size INTEGER NOT NULL,
          parser_version TEXT NOT NULL,
          qa_revision INTEGER NOT NULL
        );
        CREATE TABLE parser_validation_samples (
          canonical_url TEXT PRIMARY KEY,
          sample_year INTEGER NOT NULL,
          sample_priority TEXT NOT NULL
        );
        CREATE TABLE parser_validation_results (
          canonical_url TEXT PRIMARY KEY,
          publisher TEXT NOT NULL,
          sample_year INTEGER NOT NULL,
          parser_version TEXT NOT NULL,
          qa_revision INTEGER NOT NULL,
          qa_pass INTEGER NOT NULL
        );
        CREATE TABLE captures (
          canonical_url TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          raw_path TEXT
        );
        INSERT INTO parser_validation_config VALUES (2010, 2, 'caixin-parser/1', 3);
        """
    )
    with pytest.raises(ValueError, match="has 0 rows, expected 2"):
        selected_validation_urls(
            connection,
            publisher="caixin",
            year=2010,
            target=2,
        )


def test_partial_audit_selects_available_rows_without_lowering_target() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE parser_validation_config (
          sample_year INTEGER PRIMARY KEY,
          target_size INTEGER NOT NULL,
          parser_version TEXT NOT NULL,
          qa_revision INTEGER NOT NULL
        );
        CREATE TABLE parser_validation_samples (
          canonical_url TEXT PRIMARY KEY,
          sample_year INTEGER NOT NULL,
          sample_priority TEXT NOT NULL
        );
        CREATE TABLE parser_validation_results (
          canonical_url TEXT PRIMARY KEY,
          publisher TEXT NOT NULL,
          sample_year INTEGER NOT NULL,
          parser_version TEXT NOT NULL,
          qa_revision INTEGER NOT NULL,
          qa_pass INTEGER NOT NULL
        );
        CREATE TABLE captures (
          canonical_url TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          raw_path TEXT
        );
        INSERT INTO parser_validation_config
          VALUES (2017, 800, 'ft-parser/current', 0);
        INSERT INTO parser_validation_samples
          VALUES ('https://example.test/ft', 2017, '01');
        INSERT INTO parser_validation_results VALUES (
          'https://example.test/ft', 'ft', 2017,
          'ft-parser/current', 0, 1
        );
        INSERT INTO captures VALUES (
          'https://example.test/ft', 'complete', 'objects/ft.gz'
        );
        """
    )

    version, revision, urls = selected_validation_urls(
        connection,
        publisher="ft",
        year=2017,
        target=800,
        allow_partial=True,
    )

    assert version == "ft-parser/current"
    assert revision == 0
    assert urls == ["https://example.test/ft"]
