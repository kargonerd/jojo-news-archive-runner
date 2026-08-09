from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from dateutil.parser import isoparse

from .archive_sources import (
    ap_hosted_publication_datetime,
    archive_source_spec,
    normalize_article_url,
)
from .news_models import CaptureCandidate, CaptureProvider
from .wayback_manifest import MANIFEST_FORMAT_VERSION


ARQUIVO_PT_REPLAY_ENDPOINT = "https://arquivo.pt/noFrame/replay"
_TIMESTAMP_RE = re.compile(r"\d{14}")


def build_ap_hosted_manifest_rows(
    rows: Iterable[dict[str, object]],
    *,
    from_year: int,
    to_year: int,
    maximum_candidates: int = 3,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    if from_year < 1900 or to_year > 2100 or from_year > to_year:
        raise ValueError("invalid publication year range")
    if maximum_candidates < 1:
        raise ValueError("maximum_candidates must be positive")

    spec = archive_source_spec("ap")
    grouped: dict[
        str,
        tuple[datetime, list[tuple[tuple[object, ...], CaptureCandidate]]],
    ] = {}
    seen_rows = 0
    rejected_rows = 0
    for row in rows:
        seen_rows += 1
        original_url = str(row.get("url") or row.get("original") or "").strip()
        canonical_source = str(
            row.get("canonicalUrl")
            or row.get("canonical_url")
            or original_url
        ).strip()
        timestamp = str(row.get("timestamp") or "").strip()
        mime_type = str(row.get("mime") or row.get("mimetype") or "").strip()
        status_code = str(row.get("status") or row.get("statuscode") or "").strip()
        canonical_url = normalize_article_url(spec, canonical_source)
        published_at = ap_hosted_publication_datetime(canonical_url or "")
        if (
            canonical_url is None
            or published_at is None
            or not from_year <= published_at.year <= to_year
            or _TIMESTAMP_RE.fullmatch(timestamp) is None
            or mime_type.casefold() != "text/html"
            or status_code != "200"
        ):
            rejected_rows += 1
            continue
        captured_at = _timestamp_datetime(timestamp)
        candidate = CaptureCandidate(
            provider=CaptureProvider.ARQUIVO_PT,
            snapshot_url=(
                f"{ARQUIVO_PT_REPLAY_ENDPOINT}/{timestamp}/{original_url}"
            ),
            source_url=original_url,
            expected_headline=_optional_string(row.get("expectedHeadline")),
            captured_at=captured_at,
            digest=_optional_string(row.get("digest")),
            mime_type=mime_type,
            status_code=200,
            byte_count=_optional_nonnegative_int(row.get("length")),
        )
        site = parse_qs(urlsplit(original_url).query).get("SITE", [""])[0]
        rank = (
            0 if site.casefold() == "ap" else 1,
            abs(int((captured_at - published_at).total_seconds())),
            timestamp,
            candidate.snapshot_url,
        )
        group = grouped.setdefault(canonical_url, (published_at, []))
        group[1].append((rank, candidate))

    manifest_rows: list[dict[str, object]] = []
    candidate_count = 0
    duplicate_candidates = 0
    for canonical_url in sorted(grouped):
        published_at, ranked_candidates = grouped[canonical_url]
        candidates: list[CaptureCandidate] = []
        identities: set[tuple[str, str]] = set()
        for _, candidate in sorted(ranked_candidates, key=lambda item: item[0]):
            identity = (candidate.snapshot_url, candidate.digest or "")
            if identity in identities:
                duplicate_candidates += 1
                continue
            identities.add(identity)
            candidates.append(candidate)
            if len(candidates) >= maximum_candidates:
                break
        if not candidates:
            continue
        candidate_count += len(candidates)
        manifest_rows.append(
            {
                "formatVersion": MANIFEST_FORMAT_VERSION,
                "publisher": "ap",
                "canonicalUrl": canonical_url,
                "publishedAt": published_at.isoformat(),
                "candidates": [
                    candidate.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=True,
                    )
                    for candidate in candidates
                ],
            }
        )
    return manifest_rows, {
        "rowsSeen": seen_rows,
        "rowsRejected": rejected_rows,
        "articles": len(manifest_rows),
        "candidates": candidate_count,
        "duplicateCandidates": duplicate_candidates,
    }


def write_ap_hosted_manifest(
    rows: Iterable[dict[str, object]],
    destination: Path,
    *,
    from_year: int,
    to_year: int,
    maximum_candidates: int = 3,
) -> dict[str, int]:
    manifest_rows, metrics = build_ap_hosted_manifest_rows(
        rows,
        from_year=from_year,
        to_year=to_year,
        maximum_candidates=maximum_candidates,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    opener = gzip.open if destination.suffix == ".gz" else open
    with opener(temporary, "wt", encoding="utf-8") as handle:
        for row in manifest_rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    temporary.replace(destination)
    return metrics


def ap_hosted_page_metadata(
    html_bytes: bytes,
) -> tuple[datetime, str] | None:
    """Read stable identity metadata from a legacy Hosted AP story page."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    timestamp_node = soup.select_one(
        ".ap-story-table .timestamp.updated[title], "
        ".ap-story-table time.updated[datetime]"
    )
    timestamp = ""
    if timestamp_node is not None:
        timestamp = str(
            timestamp_node.get("title")
            or timestamp_node.get("datetime")
            or ""
        ).strip()
    headline_node = soup.select_one(
        ".ap-story-table .headline.entry-title, "
        ".ap-story-table .entry-title"
    )
    body = soup.select_one(".ap-story-table .entry-content")
    headline = (
        " ".join(headline_node.get_text(" ", strip=True).split())
        if headline_node is not None
        else ""
    )
    body_characters = len(
        " ".join(body.get_text(" ", strip=True).split())
        if body is not None
        else ""
    )
    if not timestamp or not headline or body_characters < 100:
        return None
    try:
        published_at = isoparse(timestamp)
    except (TypeError, ValueError, OverflowError):
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return published_at.astimezone(timezone.utc), headline


def _timestamp_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def _optional_string(value: object) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
