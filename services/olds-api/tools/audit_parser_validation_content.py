from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from jojo_olds_api.archive_sources import (
    archive_source_spec,
    article_deduplication_key,
    article_url_publication_year,
    normalize_article_url,
)
from jojo_olds_api.news_parser import parse_article
from jojo_olds_api.parser_validation import (
    _read_capture_html,
    _read_dependent_resources,
    is_axios_internal_test_entry,
)
from jojo_olds_api.raw_archive_capture import completed_raw_capture


_SPACE_RE = re.compile(r"\s+")
_SUSPICIOUS_IMAGE_RE = re.compile(
    r"(?i)(?:^|[/_.-])(?:advert(?:isement)?|icon|pixel|"
    r"spacer|sprite|transparent)(?:[/_.-]|$)|"
    r"(?:doubleclick|googlesyndication|scorecardresearch)"
)
_SUSPICIOUS_AVATAR_FILENAME_RE = re.compile(
    r"(?i)(?:^|[_.-])avatar(?:[_.-]|$)"
)
_INTERFACE_TEXT_RE = re.compile(
    r"(?i)^(?:advertisement|back to top|click here|follow us|more from axios:?|read more:?|related|rss|"
    r"related stories|share this article|sign in|subscribe|trending stories)$|"
    r"^(?:\d{2}\s*第\d+页\s*){2,}$|"
    r"^marketwatch拥有位于三大洲的100多名记者|"
    r"^(?:accept all cookies|all rights reserved|"
    r"download (?:our|the) app(?:\s+(?:now|today))?[.!]?$|"
    r"sign up for (?:our|the)|subscribe to (?:axios|our|the)|terms (?:of use|and conditions))"
)
_INTERACTIVE_TAGS = {"button", "form", "input", "nav", "script", "style"}
_NYT_DEAD_INTERACTIVE_CONTROL_RE = re.compile(
    r"(?i)^(?:read full answer|next:\s+.{1,120})$"
)


def nyt_raw_interactive_prose_characters(
    html_bytes: bytes,
    canonical_url: str,
) -> int:
    """Measure substantial paragraph prose available in an NYT interactive."""
    if "/interactive/" not in canonical_url.casefold():
        return 0
    soup = BeautifulSoup(html_bytes, "html.parser")
    candidates = soup.select(
        ".interactive-graphic, .interactive-body, "
        "section.interactive-content"
    )
    unique_paragraphs = {
        normalize_text(paragraph.get_text(" ", strip=True))
        for candidate in candidates
        for paragraph in candidate.select("p")
        if normalize_text(paragraph.get_text(" ", strip=True))
    }
    return sum(len(text) for text in unique_paragraphs)


def _suspicious_selected_image(value: str) -> bool:
    filename = urlsplit(value).path.rsplit("/", 1)[-1]
    return bool(
        _SUSPICIOUS_IMAGE_RE.search(value)
        or _SUSPICIOUS_AVATAR_FILENAME_RE.search(filename)
        or (
            "/__assets/creatives/brand-ft/icons/v2/open-graph.png"
            in value.casefold()
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reparse a completed validation cell and report content-level "
            "cross-article anomalies missed by row-level QA."
        )
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--target", type=int, default=800)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Audit the currently available QA-passing rows before the formal "
            "target is reached. Partial audits can pass content checks but "
            "never satisfy the formal convergence gate."
        ),
    )
    parser.add_argument("--expected-parser-version")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def normalize_text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", value or "").strip().casefold()


def image_identity(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path,
            "",
            "",
        )
    )


def url_year_mismatch(
    publisher: str,
    canonical_url: str,
    expected_year: int,
) -> int | None:
    embedded_year = article_url_publication_year(
        archive_source_spec(publisher),
        canonical_url,
    )
    return (
        embedded_year
        if embedded_year is not None and embedded_year != expected_year
        else None
    )


def selected_validation_urls(
    connection: sqlite3.Connection,
    *,
    publisher: str,
    year: int,
    target: int,
    allow_partial: bool = False,
) -> tuple[str, int, list[str]]:
    config_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(parser_validation_config)"
        )
    }
    result_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(parser_validation_results)"
        )
    }
    has_qa_revision = (
        "qa_revision" in config_columns
        and "qa_revision" in result_columns
    )
    config = connection.execute(
        (
            "SELECT parser_version, qa_revision, target_size "
            if has_qa_revision
            else "SELECT parser_version, 0, target_size "
        )
        + "FROM parser_validation_config WHERE sample_year=?",
        (year,),
    ).fetchone()
    if config is None:
        raise ValueError(f"validation config missing for {publisher}/{year}")
    parser_version, qa_revision, configured_target = config
    if int(configured_target) != target:
        raise ValueError(
            f"configured target is {configured_target}, expected {target}"
        )
    qa_revision_clause = (
        "AND result.qa_revision=?" if has_qa_revision else ""
    )
    parameters: list[object] = [
        year,
        publisher,
        year,
        str(parser_version),
    ]
    if has_qa_revision:
        parameters.append(int(qa_revision))
    parameters.append(target)
    rows = connection.execute(
        """
        SELECT sample.canonical_url
        FROM parser_validation_samples AS sample
        JOIN parser_validation_results AS result
          ON result.canonical_url=sample.canonical_url
        JOIN captures AS capture
          ON capture.canonical_url=sample.canonical_url
        WHERE sample.sample_year=?
          AND result.publisher=?
          AND result.sample_year=?
          AND result.parser_version=?
          {qa_revision_clause}
          AND result.qa_pass=1
          AND capture.status='complete'
          AND capture.raw_path IS NOT NULL
        ORDER BY sample.sample_priority
        LIMIT ?
        """.format(qa_revision_clause=qa_revision_clause),
        parameters,
    ).fetchall()
    urls = [str(row[0]) for row in rows]
    if allow_partial and not urls:
        raise ValueError("completed QA-passing sample has no rows")
    if not allow_partial and len(urls) != target:
        raise ValueError(
            f"completed QA-passing sample has {len(urls)} rows, expected {target}"
        )
    return str(parser_version), int(qa_revision), urls


def audit_content(
    *,
    state: Path,
    archive_root: Path,
    publisher: str,
    year: int,
    target: int,
    expected_parser_version: str | None = None,
    allow_partial: bool = False,
) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{state.resolve().as_posix()}?mode=ro", uri=True)
    try:
        parser_version, qa_revision, urls = selected_validation_urls(
            connection,
            publisher=publisher,
            year=year,
            target=target,
            allow_partial=allow_partial,
        )
        expected_version = expected_parser_version or parser_version
        hard_anomalies: list[dict[str, object]] = []
        review_candidates: list[dict[str, object]] = []
        block_articles: dict[str, set[str]] = defaultdict(set)
        selected_image_articles: dict[str, set[str]] = defaultdict(set)
        identity_articles: dict[str, set[str]] = defaultdict(set)
        body_lengths: list[int] = []
        selected_images = 0
        extraction_statuses: Counter[str] = Counter()
        for index, canonical_url in enumerate(urls, start=1):
            source_spec = archive_source_spec(publisher)
            normalized_url = normalize_article_url(source_spec, canonical_url)
            if normalized_url != canonical_url:
                hard_anomalies.append(
                    {
                        "type": "noncanonical-sample-url",
                        "url": canonical_url,
                        "detail": normalized_url,
                    }
                )
            identity = article_deduplication_key(source_spec, canonical_url)
            if identity is not None:
                identity_articles[identity].add(canonical_url)
            mismatched_year = url_year_mismatch(
                publisher,
                canonical_url,
                year,
            )
            if mismatched_year is not None:
                hard_anomalies.append(
                    {
                        "type": "url-publication-year-mismatch",
                        "url": canonical_url,
                        "detail": mismatched_year,
                    }
                )
            try:
                capture = completed_raw_capture(
                    connection,
                    canonical_url=canonical_url,
                )
                raw_html = _read_capture_html(capture, archive_root)
                article = parse_article(
                    raw_html,
                    publisher=publisher,
                    canonical_url=canonical_url,
                    raw_capture=capture,
                    dependent_resources=_read_dependent_resources(
                        capture,
                        archive_root,
                    ),
                    parsed_at=datetime.now(timezone.utc),
                )
            except Exception as exc:
                hard_anomalies.append(
                    {
                        "type": "reparse-error",
                        "url": canonical_url,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            extraction_statuses[article.quality.status.value] += 1
            if article.extraction.parser_version != expected_version:
                hard_anomalies.append(
                    {
                        "type": "parser-version-mismatch",
                        "url": canonical_url,
                        "detail": article.extraction.parser_version,
                    }
                )
            if (
                article.content_type.value == "article"
                and article.quality.status.value != "complete"
            ):
                hard_anomalies.append(
                    {
                        "type": "extraction-not-complete",
                        "url": canonical_url,
                        "detail": article.quality.status.value,
                    }
                )
            if publisher == "axios" and is_axios_internal_test_entry(
                canonical_url,
                article.headline,
            ):
                hard_anomalies.append(
                    {
                        "type": "internal-test-entry",
                        "url": canonical_url,
                        "detail": article.headline,
                    }
                )
            body_lengths.append(len(article.plain_text))
            normalized_blocks = [
                normalize_text(block.text)
                for block in article.blocks
                if normalize_text(block.text)
            ]
            if publisher == "nyt":
                raw_interactive_prose = nyt_raw_interactive_prose_characters(
                    raw_html,
                    canonical_url,
                )
                if (
                    raw_interactive_prose >= 1_000
                    and article.quality.body_characters < 200
                ):
                    hard_anomalies.append(
                        {
                            "type": "interactive-prose-collapse",
                            "url": canonical_url,
                            "detail": {
                                "rawParagraphCharacters": raw_interactive_prose,
                                "parsedBodyCharacters": (
                                    article.quality.body_characters
                                ),
                            },
                        }
                    )
                for text in normalized_blocks:
                    if _NYT_DEAD_INTERACTIVE_CONTROL_RE.fullmatch(text):
                        hard_anomalies.append(
                            {
                                "type": "dead-interactive-control",
                                "url": canonical_url,
                                "detail": text,
                            }
                        )
            for text in set(normalized_blocks):
                if 4 <= len(text) <= 500:
                    block_articles[text].add(canonical_url)
                if _INTERFACE_TEXT_RE.search(text):
                    hard_anomalies.append(
                        {
                            "type": "interface-text",
                            "url": canonical_url,
                            "detail": text[:500],
                        }
                    )
            duplicate_count = len(normalized_blocks) - len(set(normalized_blocks))
            if duplicate_count:
                hard_anomalies.append(
                    {
                        "type": "duplicate-text-blocks",
                        "url": canonical_url,
                        "detail": duplicate_count,
                    }
                )
            tags = {
                node.name
                for node in BeautifulSoup(article.body_html, "html.parser").find_all(True)
                if node.name in _INTERACTIVE_TAGS
            }
            if tags:
                hard_anomalies.append(
                    {
                        "type": "interactive-tags",
                        "url": canonical_url,
                        "detail": sorted(tags),
                    }
                )
            for image in article.images:
                if not image.should_archive:
                    continue
                selected_images += 1
                identity = image_identity(image.original_url)
                selected_image_articles[identity].add(canonical_url)
                if _suspicious_selected_image(identity):
                    hard_anomalies.append(
                        {
                            "type": "suspicious-selected-image",
                            "url": canonical_url,
                            "detail": image.original_url,
                        }
                    )
                if (
                    image.width is not None
                    and image.height is not None
                    and max(image.width, image.height) <= 160
                ):
                    review_candidates.append(
                        {
                            "type": "small-selected-image",
                            "url": canonical_url,
                            "detail": {
                                "image": image.original_url,
                                "width": image.width,
                                "height": image.height,
                            },
                        }
                    )
            if index % 100 == 0:
                print(json.dumps({"audited": index, "target": target}))
    finally:
        connection.close()

    repeated_threshold = max(5, math.ceil(target * 0.01))
    duplicate_identities = [
        {
            "type": "duplicate-article-identity",
            "url": sorted(article_urls)[0],
            "detail": {
                "identity": identity,
                "sampleUrls": sorted(article_urls),
            },
        }
        for identity, article_urls in identity_articles.items()
        if len(article_urls) > 1
    ]
    duplicate_identities.sort(key=lambda item: str(item["url"]))
    hard_anomalies.extend(duplicate_identities)
    repeated_blocks = [
        {
            "text": text,
            "articleCount": len(article_urls),
            "sampleUrls": sorted(article_urls)[:5],
        }
        for text, article_urls in block_articles.items()
        if len(article_urls) >= repeated_threshold
    ]
    repeated_blocks.sort(key=lambda item: (-int(item["articleCount"]), str(item["text"])))
    repeated_images = [
        {
            "image": identity,
            "articleCount": len(article_urls),
            "sampleUrls": sorted(article_urls)[:5],
        }
        for identity, article_urls in selected_image_articles.items()
        if len(article_urls) >= 2
    ]
    repeated_images.sort(key=lambda item: (-int(item["articleCount"]), str(item["image"])))
    if repeated_blocks:
        review_candidates.append(
            {"type": "cross-article-repeated-blocks", "items": repeated_blocks}
        )
    if repeated_images:
        review_candidates.append(
            {"type": "cross-article-selected-images", "items": repeated_images}
        )
    issue_counts = Counter(str(item["type"]) for item in hard_anomalies)
    lengths = sorted(body_lengths)
    quantiles = {
        "minimum": lengths[0] if lengths else None,
        "p50": lengths[len(lengths) // 2] if lengths else None,
        "p95": lengths[min(len(lengths) - 1, math.floor(len(lengths) * 0.95))]
        if lengths
        else None,
        "maximum": lengths[-1] if lengths else None,
    }
    passes_content_checks = not hard_anomalies and bool(body_lengths)
    formal_target_reached = len(body_lengths) == target
    return {
        "formatVersion": "jojo-parser-validation-content-audit/1",
        "publisher": publisher,
        "year": year,
        "target": target,
        "audited": len(body_lengths),
        "formalTargetReached": formal_target_reached,
        "configuredParserVersion": parser_version,
        "parserVersion": expected_version,
        "qaRevision": qa_revision,
        "extractionStatuses": dict(sorted(extraction_statuses.items())),
        "bodyCharacters": quantiles,
        "selectedImages": selected_images,
        "hardAnomalyCount": len(hard_anomalies),
        "hardAnomaliesByType": dict(sorted(issue_counts.items())),
        "hardAnomalies": hard_anomalies,
        "reviewCandidateCount": len(review_candidates),
        "reviewCandidates": review_candidates,
        "passesContentChecks": passes_content_checks,
        "passesHardChecks": passes_content_checks and formal_target_reached,
    }


def main() -> int:
    args = parse_args()
    result = audit_content(
        state=args.state,
        archive_root=args.archive_root,
        publisher=args.publisher,
        year=args.year,
        target=args.target,
        expected_parser_version=args.expected_parser_version,
        allow_partial=args.allow_partial,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "publisher",
                        "year",
                        "target",
                        "audited",
                        "formalTargetReached",
                        "parserVersion",
                        "configuredParserVersion",
                        "extractionStatuses",
                        "bodyCharacters",
                        "selectedImages",
                        "hardAnomalyCount",
                        "hardAnomaliesByType",
                        "reviewCandidateCount",
                        "passesHardChecks",
                        "passesContentChecks",
                    )
                }
                | {"output": str(args.output)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(rendered)
    accepted = (
        result["passesContentChecks"]
        if args.allow_partial
        else result["passesHardChecks"]
    )
    return 0 if bool(accepted) else 2


if __name__ == "__main__":
    raise SystemExit(main())
