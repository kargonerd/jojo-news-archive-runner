from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html as html_module
import json
import re
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from dateutil.parser import isoparse

from .news_models import (
    ARCHIVABLE_IMAGE_ROLES,
    ArticleStatus,
    Author,
    BlockType,
    CaptureProvider,
    CaptureReference,
    ContentBlock,
    ContentType,
    Extraction,
    ImageCandidate,
    ImageRole,
    JojoArticle,
    Quality,
    RawCapture,
)
from .publisher_specs import COMMON_REMOVE_SELECTORS, PublisherSpec, publisher_spec


_SPACE_RE = re.compile(r"\s+")
_CREDIT_RE = re.compile(
    r"(?i)(?:^|\s)(photographer|photo|credit|illustration|graphic)s?\s*:"
)
_NOISE_RE = re.compile(
    r"(?i)(advert|sponsor|promo|recommend|related|newsletter|subscribe|"
    r"paywall|cookie|tracking|pixel|logo|icon|avatar)"
)
_TRACKING_RE = re.compile(r"(?i)(pixel|tracking|spacer|transparent)")
_GRAPHIC_RE = re.compile(r"(?i)(chart|graphic|infographic|interactive)")
_MINIMUM_BODY_CHARACTERS = 100
_EXACT_NOISE_TEXT = {
    "advertisement",
    "advertiser content",
    "sponsored content",
}
_NYT_ATTENDEE_RE = re.compile(
    r'name:"((?:\\.|[^"\\])*)",caption:"((?:\\.|[^"\\])*)"'
)


def stable_article_id(publisher: str, canonical_url: str) -> str:
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    return f"{publisher}:{digest}"


def parse_article(
    html_bytes: bytes,
    *,
    publisher: str,
    canonical_url: str,
    raw_capture: RawCapture | None = None,
    parsed_at: datetime | None = None,
) -> JojoArticle:
    spec = publisher_spec(publisher)
    soup = BeautifulSoup(html_bytes, "html.parser")
    news_article = _find_news_article_json(soup)
    body = _select_body(soup, spec)
    if body is None and spec.embedded_html_body_keys:
        body = _embedded_html_body(
            soup,
            keys=spec.embedded_html_body_keys,
        )
    if body is None and spec.use_structured_article_body:
        body = _structured_article_body(news_article)
    if spec.publisher == "nyt":
        birdkit_body = _nyt_birdkit_attendee_body(soup)
        if birdkit_body is not None:
            body = birdkit_body
    clean_body = BeautifulSoup(str(body), "html.parser") if body else BeautifulSoup("", "html.parser")
    _remove_noise(clean_body, spec)

    headline = _first_text(
        _string_or_none(news_article.get("headline")) if news_article else None,
        _meta_content(soup, "property", "og:title"),
        _meta_content(soup, "name", "twitter:title"),
        _tag_text(soup.select_one("article h1, main h1, h1")),
    )
    description = _first_text(
        _string_or_none(news_article.get("description")) if news_article else None,
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "property", "og:description"),
    )
    authors = _extract_authors(news_article, soup)
    published_at = _parse_datetime(
        _first_text(
            _string_or_none(news_article.get("datePublished"))
            if news_article
            else None,
            _meta_content(soup, "property", "article:published_time"),
            _meta_content(soup, "name", "pub_date"),
            _meta_content(soup, "name", "pdate"),
            _tag_attribute(
                soup.select_one(
                    '[itemprop="datePublished"][datetime], '
                    'time[datetime][data-testid*="timestamp" i]'
                ),
                "datetime",
            ),
        )
    )
    if published_at is None and raw_capture is not None:
        published_at = raw_capture.published_at
    modified_at = _parse_datetime(
        _first_text(
            _string_or_none(news_article.get("dateModified"))
            if news_article
            else None,
            _meta_content(soup, "property", "article:modified_time"),
            _meta_content(soup, "name", "lastmod"),
            _tag_attribute(
                soup.select_one('[itemprop="dateModified"][datetime]'),
                "datetime",
            ),
        )
    )
    section = _first_text(
        _string_or_none(news_article.get("articleSection"))
        if news_article
        else None,
        raw_capture.section if raw_capture else None,
        _meta_content(soup, "name", "section"),
        _meta_content(soup, "property", "article:section"),
    )
    language = _document_language(soup, default=spec.default_language)
    content_type = _content_type(news_article, canonical_url)

    images_by_url: dict[str, ImageCandidate] = {}
    blocks: list[ContentBlock] = []
    for url in _lead_image_urls(soup, news_article, canonical_url):
        image = _image_candidate(
            url=url,
            candidate_urls=[url],
            role=ImageRole.LEAD,
            spec=spec,
            reasons=["structured-lead-image"],
        )
        images_by_url.setdefault(image.original_url, image)

    if clean_body:
        blocks, body_images = _extract_blocks(
            clean_body,
            base_url=canonical_url,
            spec=spec,
            starting_position=0,
        )
        for image in body_images:
            existing = images_by_url.get(image.original_url)
            if existing is None:
                images_by_url[image.original_url] = image
                continue
            # A body occurrence provides position/caption evidence that metadata alone
            # does not. Keep the lead role but merge useful descriptive fields.
            if not existing.caption and image.caption:
                existing.caption = image.caption
            if not existing.credit and image.credit:
                existing.credit = image.credit
            if not existing.alt and image.alt:
                existing.alt = image.alt
            existing.selection_reasons = sorted(
                set(existing.selection_reasons + image.selection_reasons)
            )
            for block in blocks:
                if block.asset_id == image.asset_id:
                    block.asset_id = existing.asset_id
        blocks = _deduplicate_blocks(blocks)

    if content_type == ContentType.ARTICLE and _looks_like_gallery(blocks):
        content_type = ContentType.GALLERY
    plain_text = "\n\n".join(
        value
        for block in blocks
        if (value := _block_plain_text(block))
    )
    body_html = _inner_html(clean_body)
    images = list(images_by_url.values())
    warnings: list[str] = []
    if not headline:
        warnings.append("missing-headline")
    if len(plain_text) < _MINIMUM_BODY_CHARACTERS:
        warnings.append("body-too-short")
    if not published_at:
        warnings.append("missing-published-at")
    if body is None:
        warnings.append("article-body-not-found")

    status = ArticleStatus.COMPLETE
    if "article-body-not-found" in warnings:
        status = ArticleStatus.UNSUPPORTED
    elif "body-too-short" in warnings or "missing-headline" in warnings:
        status = ArticleStatus.PARTIAL

    capture_reference = _capture_reference(
        raw_capture=raw_capture,
        publisher=publisher,
        canonical_url=canonical_url,
    )
    parsed_at = parsed_at or datetime.now(timezone.utc)
    return JojoArticle(
        article_id=(
            raw_capture.article_id
            if raw_capture
            else stable_article_id(publisher, canonical_url)
        ),
        publisher=publisher,
        edition=spec.edition,
        canonical_url=canonical_url,
        language=language,
        content_type=content_type,
        section=section,
        headline=headline,
        description=description,
        authors=authors,
        published_at=published_at,
        modified_at=modified_at,
        plain_text=plain_text,
        body_html=body_html,
        blocks=blocks,
        images=images,
        source_capture=capture_reference,
        extraction=Extraction(
            parser=publisher,
            parser_version=spec.parser_version,
            parsed_at=parsed_at,
            source_capture_id=capture_reference.capture_id,
        ),
        quality=Quality(
            status=status,
            body_characters=len(plain_text),
            block_count=len(blocks),
            images_referenced=len(images),
            images_selected=sum(image.should_archive for image in images),
            warnings=warnings,
        ),
    )


def _capture_reference(
    *,
    raw_capture: RawCapture | None,
    publisher: str,
    canonical_url: str,
) -> CaptureReference:
    if raw_capture:
        candidate = raw_capture.selected_candidate
        timestamp = (
            candidate.captured_at.isoformat()
            if candidate.captured_at
            else "unknown"
        )
        return CaptureReference(
            capture_id=(
                f"{candidate.provider.value}:{timestamp}:"
                f"{raw_capture.raw_html.sha256[:16]}"
            ),
            provider=candidate.provider,
            snapshot_url=candidate.snapshot_url,
            captured_at=candidate.captured_at,
            raw_html=raw_capture.raw_html,
        )
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]
    return CaptureReference(
        capture_id=f"other:unknown:{digest}",
        provider=CaptureProvider.OTHER,
        snapshot_url=canonical_url,
    )


def _find_news_article_json(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select('script[type="application/ld+json"]'):
        value = script.string or script.get_text()
        if not value.strip():
            continue
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_json_objects(payload):
            types = item.get("@type")
            if isinstance(types, str):
                types = [types]
            if isinstance(types, list) and any(
                value in {"NewsArticle", "Article", "ReportageNewsArticle"}
                for value in types
            ):
                return item
    return {}


def _walk_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_objects(child)


def _select_body(soup: BeautifulSoup, spec: PublisherSpec) -> Tag | None:
    for selector in spec.body_selectors:
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            return node
    return None


def _structured_article_body(
    news_article: dict[str, Any],
) -> Tag | None:
    value = news_article.get("articleBody")
    if not isinstance(value, str):
        return None
    paragraphs = [
        _clean_text(paragraph)
        for paragraph in re.split(r"\n\s*\n", value)
        if _clean_text(paragraph)
    ]
    if not paragraphs:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for paragraph in paragraphs:
        node = document.new_tag("p")
        node.string = paragraph
        article.append(node)
    return article


def _nyt_birdkit_attendee_body(
    soup: BeautifulSoup,
) -> Tag | None:
    rows: list[tuple[str, str]] = []
    for script in soup.find_all("script"):
        value = script.string or script.get_text()
        if not value or "sheets:{attendees:[" not in value:
            continue
        for match in _NYT_ATTENDEE_RE.finditer(value):
            try:
                name = json.loads(f'"{match.group(1)}"')
                caption = json.loads(f'"{match.group(2)}"')
            except (json.JSONDecodeError, TypeError):
                continue
            name = _clean_text(str(name))
            caption = _clean_text(str(caption))
            if name:
                rows.append((name, caption))
    if len(rows) < 3:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for name, caption in rows:
        paragraph = document.new_tag("p")
        paragraph.string = f"{name} — {caption}" if caption else name
        article.append(paragraph)
    return article


def _embedded_html_body(
    soup: BeautifulSoup,
    *,
    keys: tuple[str, ...],
) -> Tag | None:
    decoder = json.JSONDecoder()
    quoted_keys = tuple(f'"{key}"' for key in keys)
    for script in soup.find_all("script"):
        value = script.string or script.get_text()
        if not value or not any(key in value for key in quoted_keys):
            continue
        starts = [
            match.end()
            for match in re.finditer(r"=\s*(?=\{)", value)
        ]
        if not starts:
            first_object = value.find("{")
            if first_object >= 0:
                starts.append(first_object)
        for start in starts:
            try:
                payload, _ = decoder.raw_decode(value[start:])
            except (json.JSONDecodeError, TypeError):
                continue
            for item in _walk_json_objects(payload):
                for key in keys:
                    html_value = item.get(key)
                    if (
                        not isinstance(html_value, str)
                        or not html_value.strip()
                    ):
                        continue
                    document = BeautifulSoup(
                        f"<article>{html_value}</article>",
                        "html.parser",
                    )
                    article = document.article
                    if isinstance(article, Tag):
                        return article
    return None


def _remove_noise(soup: BeautifulSoup, spec: PublisherSpec) -> None:
    for selector in (*COMMON_REMOVE_SELECTORS, *spec.remove_selectors):
        for node in soup.select(selector):
            node.decompose()
    for node in soup.select("p, div, span"):
        if _clean_text(node.get_text(" ", strip=True)).casefold() in _EXACT_NOISE_TEXT:
            node.decompose()


def _extract_blocks(
    body: BeautifulSoup,
    *,
    base_url: str,
    spec: PublisherSpec,
    starting_position: int,
) -> tuple[list[ContentBlock], list[ImageCandidate]]:
    blocks: list[ContentBlock] = []
    images: list[ImageCandidate] = []
    selectors = [
        "p",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "ul",
        "ol",
        "figure",
        "img",
        "table",
        "hr",
        "iframe",
        *spec.text_block_selectors,
    ]
    selected = body.select(", ".join(selectors))
    publisher_text_node_ids = {
        id(node)
        for selector in spec.text_block_selectors
        for node in body.select(selector)
    }
    for node in selected:
        if _has_selected_ancestor(node, body):
            continue
        position = starting_position + len(blocks)
        name = node.name.lower()
        if name == "p":
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                blocks.append(
                    ContentBlock(
                        type=BlockType.PARAGRAPH,
                        position=position,
                        text=text,
                        html=str(node),
                    )
                )
        elif name in {"div", "span"} and id(node) in publisher_text_node_ids:
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                event = node.find_parent("article")
                is_heading = bool(
                    isinstance(event, Tag)
                    and "title" in (event.get("class") or [])
                )
                blocks.append(
                    ContentBlock(
                        type=(
                            BlockType.HEADING
                            if is_heading
                            else BlockType.PARAGRAPH
                        ),
                        position=position,
                        level=2 if is_heading else None,
                        text=text,
                        html=str(node),
                    )
                )
        elif name in {"h2", "h3", "h4", "h5", "h6"}:
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                blocks.append(
                    ContentBlock(
                        type=BlockType.HEADING,
                        position=position,
                        level=int(name[1]),
                        text=text,
                        html=str(node),
                    )
                )
        elif name == "blockquote":
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                blocks.append(
                    ContentBlock(
                        type=BlockType.QUOTE,
                        position=position,
                        text=text,
                        html=str(node),
                    )
                )
        elif name in {"ul", "ol"}:
            items = [
                _clean_text(item.get_text(" ", strip=True))
                for item in node.find_all("li", recursive=False)
            ]
            items = [item for item in items if item]
            if items:
                blocks.append(
                    ContentBlock(
                        type=BlockType.LIST,
                        position=position,
                        text="\n".join(items),
                        items=items,
                        html=str(node),
                    )
                )
        elif name in {"figure", "img"}:
            image_node = node.find("img") if name == "figure" else node
            if not isinstance(image_node, Tag):
                continue
            image = _image_from_tag(
                image_node,
                container=node,
                base_url=base_url,
                spec=spec,
            )
            if not image:
                continue
            images.append(image)
            blocks.append(
                ContentBlock(
                    type=BlockType.IMAGE,
                    position=position,
                    asset_id=image.asset_id,
                    caption=image.caption,
                    credit=image.credit,
                    html=str(node),
                )
            )
        elif name == "table":
            text = _clean_text(node.get_text(" ", strip=True))
            blocks.append(
                ContentBlock(
                    type=BlockType.TABLE,
                    position=position,
                    text=text or None,
                    html=str(node),
                )
            )
        elif name == "hr":
            blocks.append(ContentBlock(type=BlockType.DIVIDER, position=position))
        elif name == "iframe":
            source = _normalized_url(node.get("src"), base_url=base_url)
            if source:
                blocks.append(
                    ContentBlock(
                        type=BlockType.EMBED,
                        position=position,
                        embed_url=source,
                        html=str(node),
                    )
                )
    return blocks, images


def _has_selected_ancestor(node: Tag, body: BeautifulSoup) -> bool:
    selected_names = {
        "p",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "ul",
        "ol",
        "figure",
        "table",
        "iframe",
    }
    parent = node.parent
    while isinstance(parent, Tag) and parent is not body:
        if parent.name and parent.name.lower() in selected_names:
            return True
        parent = parent.parent
    return False


def _deduplicate_blocks(blocks: list[ContentBlock]) -> list[ContentBlock]:
    seen_text: set[str] = set()
    seen_assets: set[str] = set()
    unique: list[ContentBlock] = []
    for block in blocks:
        if block.text:
            normalized = _normalize_block_text(block.text)
            if normalized and normalized in seen_text:
                continue
            if normalized:
                seen_text.add(normalized)
        if block.type == BlockType.IMAGE and block.asset_id:
            if block.asset_id in seen_assets:
                continue
            seen_assets.add(block.asset_id)
        block.position = len(unique)
        unique.append(block)
    return unique


def _normalize_block_text(value: str) -> str:
    return _clean_text(value).casefold()


def _image_from_tag(
    image_node: Tag,
    *,
    container: Tag,
    base_url: str,
    spec: PublisherSpec,
) -> ImageCandidate | None:
    candidates = _image_urls(image_node, base_url=base_url)
    if not candidates:
        return None
    original_url = candidates[0]
    width = _integer_attribute(image_node, "width")
    height = _integer_attribute(image_node, "height")
    alt = _clean_text(image_node.get("alt", "")) or None
    caption, credit = _caption_credit(container)
    context = " ".join(
        filter(
            None,
            [
                container.get("class") and " ".join(container.get("class", [])),
                container.get("id"),
                image_node.get("class") and " ".join(image_node.get("class", [])),
                image_node.get("id"),
                original_url,
            ],
        )
    )
    reasons = ["inside-article-body"]
    role = ImageRole.BODY
    if _TRACKING_RE.search(context) or (
        width is not None
        and height is not None
        and width <= 2
        and height <= 2
    ):
        role = ImageRole.TRACKING
        reasons.append("tracking-signal")
    elif _NOISE_RE.search(context):
        if re.search(r"(?i)(advert|sponsor|promo)", context):
            role = ImageRole.ADVERTISEMENT
        elif re.search(r"(?i)(recommend|related)", context):
            role = ImageRole.RECOMMENDATION
        elif re.search(r"(?i)avatar", context):
            role = ImageRole.AUTHOR_AVATAR
        elif re.search(r"(?i)logo", context):
            role = ImageRole.LOGO
        else:
            role = ImageRole.ICON
        reasons.append("non-editorial-context")
    elif _GRAPHIC_RE.search(context):
        role = (
            ImageRole.INFOGRAPHIC
            if re.search(r"(?i)infographic", context)
            else ImageRole.CHART
        )
        reasons.append("graphic-context")
    elif width is not None and height is not None and max(width, height) <= 64:
        role = ImageRole.ICON
        reasons.append("small-dimensions")
    if caption:
        reasons.append("has-caption")
    if urlsplit(original_url).hostname in spec.preferred_image_hosts:
        reasons.append("publisher-image-host")
    return _image_candidate(
        url=original_url,
        candidate_urls=candidates,
        role=role,
        spec=spec,
        reasons=reasons,
        caption=caption,
        credit=credit,
        alt=alt,
        width=width,
        height=height,
    )


def _image_candidate(
    *,
    url: str,
    candidate_urls: list[str],
    role: ImageRole,
    spec: PublisherSpec,
    reasons: list[str],
    caption: str | None = None,
    credit: str | None = None,
    alt: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> ImageCandidate:
    del spec  # retained in the signature for publisher-specific policy hooks
    asset_id = f"urlsha256:{hashlib.sha256(url.encode('utf-8')).hexdigest()}"
    return ImageCandidate(
        asset_id=asset_id,
        role=role,
        original_url=url,
        candidate_urls=candidate_urls,
        caption=caption,
        credit=credit,
        alt=alt,
        width=width,
        height=height,
        should_archive=role in ARCHIVABLE_IMAGE_ROLES,
        selection_reasons=sorted(set(reasons)),
    )


def _image_urls(image: Tag, *, base_url: str) -> list[str]:
    values: list[tuple[int, str]] = []
    for attribute in ("src", "data-src", "data-original", "data-image"):
        normalized = _normalized_url(image.get(attribute), base_url=base_url)
        if normalized:
            values.append((0, normalized))
    for attribute in ("srcset", "data-srcset"):
        raw = image.get(attribute)
        if not isinstance(raw, str):
            continue
        for entry in raw.split(","):
            parts = entry.strip().split()
            if not parts:
                continue
            normalized = _normalized_url(parts[0], base_url=base_url)
            if not normalized:
                continue
            score = 0
            if len(parts) > 1 and parts[1].endswith("w"):
                try:
                    score = int(parts[1][:-1])
                except ValueError:
                    score = 0
            values.append((score, normalized))
    values.sort(key=lambda item: item[0], reverse=True)
    result: list[str] = []
    for _, value in values:
        if value not in result:
            result.append(value)
    return result


def _lead_image_urls(
    soup: BeautifulSoup,
    article: dict[str, Any],
    base_url: str,
) -> list[str]:
    values: list[str] = []
    if article:
        values.extend(_flatten_image_values(article.get("image")))
    values.extend(
        filter(
            None,
            [
                _meta_content(soup, "property", "og:image"),
                _meta_content(soup, "name", "twitter:image"),
                _meta_content(soup, "name", "parsely-image-url"),
            ],
        )
    )
    result: list[str] = []
    for value in values:
        normalized = _normalized_url(value, base_url=base_url)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _flatten_image_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return _flatten_image_values(value.get("url") or value.get("contentUrl"))
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_image_values(item))
        return result
    return []


def _caption_credit(container: Tag) -> tuple[str | None, str | None]:
    caption_node = container.select_one("figcaption, [class*='caption' i]")
    if not caption_node:
        return None, None
    raw = _dedupe_lines(caption_node.get_text("\n", strip=True))
    if not raw:
        return None, None
    match = _CREDIT_RE.search(raw)
    if not match:
        return raw, None
    caption = _clean_text(raw[: match.start()]) or None
    credit = _clean_text(raw[match.start() :]) or None
    if caption and credit and caption.casefold() == credit.casefold():
        caption = None
    return caption, credit


def _dedupe_lines(value: str) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for line in value.splitlines():
        clean = _clean_text(line)
        key = clean.casefold()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return "\n".join(result)


def _extract_authors(
    article: dict[str, Any],
    soup: BeautifulSoup,
) -> list[Author]:
    values: list[str] = []
    source = article.get("author") if article else None
    if isinstance(source, str):
        values.append(source)
    elif isinstance(source, dict):
        name = _string_or_none(source.get("name"))
        if name:
            values.append(name)
    elif isinstance(source, list):
        for item in source:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                name = _string_or_none(item.get("name"))
                if name:
                    values.append(name)
    if not values:
        meta = _meta_content(soup, "name", "author")
        if meta:
            values.extend(part.strip() for part in meta.split(","))
    result: list[Author] = []
    seen: set[str] = set()
    for value in values:
        clean = _clean_text(value)
        if clean and clean.casefold() not in seen:
            result.append(Author(name=clean))
            seen.add(clean.casefold())
    return result


def _content_type(article: dict[str, Any], canonical_url: str) -> ContentType:
    article_type = article.get("@type") if article else None
    url = canonical_url.casefold()
    if "live" in url:
        return ContentType.LIVEBLOG
    if "newsletter" in url:
        return ContentType.NEWSLETTER
    if "transcript" in url:
        return ContentType.TRANSCRIPT
    if "opinion" in url:
        return ContentType.OPINION
    if "video" in url:
        return ContentType.VIDEO
    if "interactive" in url or "/features/" in url:
        return ContentType.INTERACTIVE
    if isinstance(article_type, str) and article_type == "ReportageNewsArticle":
        return ContentType.ARTICLE
    return ContentType.ARTICLE


def _looks_like_gallery(blocks: list[ContentBlock]) -> bool:
    image_blocks = [
        block for block in blocks if block.type == BlockType.IMAGE
    ]
    text_blocks = [
        block
        for block in blocks
        if block.type
        in {
            BlockType.PARAGRAPH,
            BlockType.HEADING,
            BlockType.QUOTE,
            BlockType.LIST,
            BlockType.TABLE,
        }
    ]
    caption_characters = sum(
        len(_clean_text(block.caption or ""))
        for block in image_blocks
    )
    text_characters = sum(
        len(_clean_text(block.text or ""))
        for block in text_blocks
    )
    return bool(
        image_blocks
        and len(text_blocks) <= 2
        and caption_characters >= 100
        and caption_characters >= text_characters
    )


def _block_plain_text(block: ContentBlock) -> str | None:
    if block.text and block.type in {
        BlockType.PARAGRAPH,
        BlockType.HEADING,
        BlockType.QUOTE,
        BlockType.LIST,
        BlockType.TABLE,
    }:
        return _clean_text(block.text)
    if block.type == BlockType.IMAGE:
        parts: list[str] = []
        for value in (block.caption, block.credit):
            clean = _clean_text(value or "")
            if clean and clean not in parts:
                parts.append(clean)
        return "\n".join(parts) or None
    return None


def _document_language(soup: BeautifulSoup, *, default: str) -> str:
    node = soup.find("html")
    if isinstance(node, Tag):
        value = node.get("lang")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _meta_content(
    soup: BeautifulSoup,
    attribute: str,
    value: str,
) -> str | None:
    node = soup.select_one(f'meta[{attribute}="{value}"]')
    if not isinstance(node, Tag):
        return None
    return _string_or_none(node.get("content"))


def _tag_text(node: Tag | None) -> str | None:
    return _clean_text(node.get_text(" ", strip=True)) if node else None


def _tag_attribute(node: Tag | None, attribute: str) -> str | None:
    if not isinstance(node, Tag):
        return None
    return _string_or_none(node.get(attribute))


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = isoparse(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _first_text(*values: str | None) -> str | None:
    for value in values:
        clean = _clean_text(value or "")
        if clean:
            return clean
    return None


def _clean_text(value: str) -> str:
    return _SPACE_RE.sub(" ", html_module.unescape(value)).strip()


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _normalized_url(value: Any, *, base_url: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = html_module.unescape(value.strip())
    if not value or value.startswith(("data:", "blob:", "javascript:")):
        return None
    if value.startswith("//"):
        value = "https:" + value
    value = urljoin(base_url, value)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _integer_attribute(node: Tag, name: str) -> int | None:
    value = node.get(name)
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        match = re.match(r"^\d+", value)
        if match:
            return int(match.group(0))
    return None


def _inner_html(soup: BeautifulSoup) -> str:
    root = soup.body or soup
    return "".join(str(child) for child in root.contents).strip()
