from __future__ import annotations

import sqlite3

import pytest

from tools.audit_parser_validation_content import (
    _INTERFACE_TEXT_RE,
    _suspicious_selected_image,
    image_identity,
    normalize_text,
    selected_validation_urls,
)


def test_normalizes_text_and_image_identity() -> None:
    assert normalize_text("  Hello\n WORLD ") == "hello world"
    assert image_identity("HTTPS://IMG.EXAMPLE/a.jpg?width=1200#x") == (
        "https://img.example/a.jpg"
    )


def test_interface_text_detector_does_not_match_ordinary_prose() -> None:
    assert _INTERFACE_TEXT_RE.search("subscribe") is not None
    assert _INTERFACE_TEXT_RE.search("subscribe to our daily newsletter") is not None
    assert _INTERFACE_TEXT_RE.search("terms of use") is not None
    assert _INTERFACE_TEXT_RE.search("01 第1页 02 第2页") is not None
    assert _INTERFACE_TEXT_RE.search(
        "MarketWatch拥有位于三大洲的100多名记者，为世界各地读者提供新闻。"
    ) is not None
    assert _INTERFACE_TEXT_RE.search(
        "The court considered whether violating the terms of use was illegal."
    ) is None
    assert _INTERFACE_TEXT_RE.search(
        "Kafka users can publish data streams or subscribe to them in real time."
    ) is None


def test_suspicious_image_detector_distinguishes_movie_from_user_avatar() -> None:
    assert _suspicious_selected_image(
        "https://media.example/authors/default-avatar.png"
    )
    assert not _suspicious_selected_image(
        "https://media.npr.org/assets/movies/2009/12/avatar/"
        "humanandavatar2-f44c267a.jpg"
    )
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
