from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html as html_module
import json
import re
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

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
_MINIMUM_SYNDICATED_BODY_CHARACTERS = 400
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
    allow_generic_syndication: bool = False,
) -> JojoArticle:
    spec = publisher_spec(publisher)
    soup = BeautifulSoup(html_bytes, "html.parser")
    news_article = _find_news_article_json(soup)
    nyt_preloaded_metadata = (
        _nyt_preloaded_article_metadata(soup, canonical_url=canonical_url)
        if spec.publisher == "nyt"
        else {}
    )
    body = None
    structured_image_gallery_selected = False
    nyt_interactive_body_selected = False
    if spec.publisher == "ap":
        gallery_body = _ap_carousel_gallery(soup)
        if gallery_body is not None:
            body = gallery_body
            structured_image_gallery_selected = True
        else:
            body = _ap_structured_race_call_body(news_article)
    if spec.publisher == "nyt":
        body = _nyt_story_body_companions(soup)
    if spec.publisher in {"reuters", "bloomberg"} and _is_yahoo_syndication(
        soup,
        raw_capture=raw_capture,
    ):
        body = _yahoo_syndication_body(
            soup,
            stop_at_reporting_by=spec.publisher == "reuters",
        )
    generic_syndication_allowed = (
        allow_generic_syndication
        or (
            raw_capture is not None
            and (
                raw_capture.selected_candidate.provider == CaptureProvider.OTHER
                or (
                    spec.publisher == "ft"
                    and raw_capture.selected_candidate.provider
                    == CaptureProvider.INFINI_NEWS
                )
            )
        )
    )
    if (
        body is None
        and spec.publisher == "bloomberg"
        and generic_syndication_allowed
    ):
        body = _bloomberg_partner_body(soup)
    if body is None and (
        generic_syndication_allowed
    ):
        body = _generic_syndication_body(soup)
    if body is None and spec.publisher == "nyt":
        body = _nyt_legacy_article_body(soup)
    if (
        body is None
        and spec.publisher == "nyt"
        and "/watching/" in canonical_url.casefold()
    ):
        body = _nyt_watching_body(soup)
    if (
        spec.publisher == "nyt"
        and "/interactive/" in canonical_url.casefold()
    ):
        interactive_body = _nyt_interactive_body(soup)
        if interactive_body is not None:
            body = interactive_body
            nyt_interactive_body_selected = True
    if spec.publisher == "bloomberg":
        embedded_bloomberg_body = _bloomberg_embedded_article_body(soup)
        if embedded_bloomberg_body is not None and (
            body is None
            or len(body.get_text(" ", strip=True))
            < len(embedded_bloomberg_body.get_text(" ", strip=True))
        ):
            body = embedded_bloomberg_body
    if spec.publisher == "nyt":
        preloaded_body = _nyt_preloaded_article_body(
            soup,
            canonical_url=canonical_url,
        )
        if preloaded_body is not None and (
            body is None
            or len(body.get_text(" ", strip=True))
            < len(preloaded_body.get_text(" ", strip=True))
        ):
            body = preloaded_body
    if body is None:
        body = _select_body(soup, spec)
    if spec.publisher == "wsj":
        gallery_body = _structured_image_gallery(soup)
        if gallery_body is None:
            gallery_body = _wsj_amp_story_gallery(soup)
        if gallery_body is None:
            gallery_body = _wsj_legacy_slideshow(soup)
        if gallery_body is not None:
            body = gallery_body
            structured_image_gallery_selected = True
    if spec.publisher == "nyt":
        gallery_body = _nyt_preloaded_image_gallery(soup)
        if gallery_body is not None and not nyt_interactive_body_selected:
            body = gallery_body
            structured_image_gallery_selected = True
    if spec.embedded_html_body_keys and (
        body is None
        or body.select_one(
            "p, h2, h3, h4, h5, h6, blockquote, ul, ol, table"
        )
        is None
    ):
        embedded_body = _embedded_html_body(
            soup,
            keys=spec.embedded_html_body_keys,
        )
        if embedded_body is not None:
            body = embedded_body
    if spec.use_structured_article_body:
        structured_body = _structured_article_body(news_article)
        if body is None:
            body = structured_body
        elif structured_body is not None:
            body = _prefer_structured_body_with_media(
                body,
                structured_body=structured_body,
            )
    if spec.publisher == "nyt":
        birdkit_body = _nyt_birdkit_attendee_body(soup)
        if birdkit_body is not None:
            body = birdkit_body
    clean_body = BeautifulSoup(str(body), "html.parser") if body else BeautifulSoup("", "html.parser")
    _remove_noise(clean_body, spec)

    headline = _first_text(
        _string_or_none(nyt_preloaded_metadata.get("headline")),
        _string_or_none(news_article.get("headline")) if news_article else None,
        _ap_data_bulletin_headline(news_article)
        if spec.publisher == "ap"
        else None,
        _meta_content(soup, "property", "og:title"),
        _meta_content(soup, "name", "twitter:title"),
        _tag_text(soup.select_one("article h1, main h1, h1")),
    )
    description = _first_text(
        _string_or_none(nyt_preloaded_metadata.get("description")),
        _string_or_none(news_article.get("description")) if news_article else None,
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "property", "og:description"),
    )
    authors = _extract_authors(news_article, soup)
    metadata_authors = nyt_preloaded_metadata.get("authors")
    if isinstance(metadata_authors, list) and metadata_authors:
        authors = [
            Author(name=value)
            for value in metadata_authors
            if isinstance(value, str) and value.strip()
        ]
    published_at = _parse_datetime(
        _first_text(
            _string_or_none(nyt_preloaded_metadata.get("published_at")),
            _string_or_none(news_article.get("datePublished"))
            if news_article
            else None,
            _meta_content(soup, "property", "article:published_time"),
            _meta_content(soup, "property", "og:article:published_time"),
            _meta_content(soup, "name", "pub_date"),
            _meta_content(soup, "name", "pdate"),
            _meta_content(
                soup,
                "name",
                "analyticsAttributes.articleDate",
            ),
            _meta_content(soup, "name", "sailthru.date"),
            _nyt_visible_published_at(soup),
            _ft_legacy_published_at(soup) if spec.publisher == "ft" else None,
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
            _string_or_none(nyt_preloaded_metadata.get("modified_at")),
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
    if spec.publisher == "nyt":
        content_type = _nyt_media_content_type(
            soup,
            default=content_type,
            structured_image_gallery_selected=structured_image_gallery_selected,
            canonical_url=canonical_url,
        )
    if (
        spec.publisher == "wsj"
        and _wsj_interactive_puzzle(soup, news_article, canonical_url)
    ):
        content_type = ContentType.INTERACTIVE
    if (
        spec.publisher == "ap"
        and _is_ap_data_bulletin(news_article, canonical_url)
    ):
        content_type = ContentType.INTERACTIVE

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
        image_key = _image_identity(image.original_url)
        existing = images_by_url.get(image_key)
        if existing is None:
            images_by_url[image_key] = image
        else:
            _merge_candidate_urls(existing, image)

    if clean_body:
        blocks, body_images = _extract_blocks(
            clean_body,
            base_url=canonical_url,
            spec=spec,
            starting_position=0,
        )
        for image in body_images:
            image_key = _image_identity(image.original_url)
            existing = images_by_url.get(image_key)
            if existing is None:
                images_by_url[image_key] = image
                continue
            # A body occurrence provides position/caption evidence that metadata alone
            # does not. Keep the lead role but merge useful descriptive fields.
            _merge_candidate_urls(existing, image)
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

    if content_type == ContentType.ARTICLE and (
        structured_image_gallery_selected
        or _looks_like_gallery(
            blocks,
            allow_uncaptioned=spec.publisher == "ft",
        )
    ):
        content_type = ContentType.GALLERY
    plain_text = "\n\n".join(
        value
        for block in blocks
        if (value := _block_plain_text(block))
    )
    body_html = _inner_html(clean_body)
    images = list(images_by_url.values())
    if (
        spec.publisher == "ft"
        and content_type == ContentType.ARTICLE
        and _ft_image_led_article(
            news_article,
            body_characters=len(plain_text),
            images=images,
        )
    ):
        content_type = ContentType.GALLERY
    warnings: list[str] = []
    if not headline:
        warnings.append("missing-headline")
    image_block_count = sum(
        block.type == BlockType.IMAGE for block in blocks
    )
    image_led_gallery = (
        content_type == ContentType.GALLERY
        and (
            image_block_count >= 1
            or (spec.publisher in {"nyt", "ft"} and len(images) >= 1)
        )
    )
    publisher_notice = _is_publisher_notice(
        headline=headline,
        description=description,
        plain_text=plain_text,
    )
    structured_short_record = _is_structured_short_record(
        spec=spec,
        news_article=news_article,
        headline=headline,
        plain_text=plain_text,
    )
    if (
        len(plain_text) < _MINIMUM_BODY_CHARACTERS
        and not image_led_gallery
        and not publisher_notice
        and not structured_short_record
    ):
        warnings.append("body-too-short")
    if publisher_notice:
        warnings.append("publisher-notice")
    if structured_short_record:
        warnings.append("structured-short-record")
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


def _is_yahoo_syndication(
    soup: BeautifulSoup,
    *,
    raw_capture: RawCapture | None,
) -> bool:
    if raw_capture is not None:
        host = (urlsplit(raw_capture.final_url).hostname or "").casefold()
        if host == "yahoo.com" or host.endswith(".yahoo.com"):
            return True
    site_name = _meta_content(soup, "property", "og:site_name")
    return bool(site_name and "yahoo" in site_name.casefold())


def _yahoo_syndication_body(
    soup: BeautifulSoup,
    *,
    stop_at_reporting_by: bool,
) -> Tag | None:
    primary_article = soup.select_one("article")
    if primary_article is None:
        return None
    paragraphs = [
        paragraph
        for paragraph in primary_article.select("p")
        if paragraph.find_parent("article") is primary_article
    ]
    if not paragraphs:
        return None
    wrapper_document = BeautifulSoup(
        "<div data-jojo-source='yahoo-syndication'></div>",
        "html.parser",
    )
    wrapper = wrapper_document.select_one("div")
    if wrapper is None:
        return None
    for paragraph in paragraphs:
        ancestor_classes = " ".join(
            " ".join(parent.get("class", []))
            for parent in paragraph.parents
            if isinstance(parent, Tag)
        ).casefold()
        if (
            paragraph.find_parent(("header", "button", "nav", "footer"))
            or paragraph.select_one("button") is not None
            or any(
                marker in ancestor_classes
                for marker in ("key-takeaway", "yahoo-scout")
            )
        ):
            continue
        copy = BeautifulSoup(str(paragraph), "html.parser").select_one("p")
        if copy is not None:
            wrapper.append(copy)
        if stop_at_reporting_by and re.match(
            r"^\s*\((?:additional )?reporting by\b",
            paragraph.get_text(" ", strip=True),
            re.IGNORECASE,
        ):
            break
    return wrapper if wrapper.select_one("p") is not None else None


def _generic_syndication_body(soup: BeautifulSoup) -> Tag | None:
    selectors = (
        "[itemprop='articleBody']",
        ".post-content",
        ".entry-content",
        ".article-content",
        ".article-body",
        ".story-body",
        "[class*='article-body' i]",
        "[class*='story-body' i]",
        "article",
        "main",
    )
    for selector in selectors:
        for node in soup.select(selector):
            document = BeautifulSoup(str(node), "html.parser")
            copy = document.select_one(selector)
            if copy is None:
                copy = document.find(node.name)
            if not isinstance(copy, Tag):
                continue
            for noise in copy.select(
                "aside, header, nav, footer, form, button, "
                "[class*='recommend' i], [class*='related' i], "
                "[class*='newsletter' i], [class*='advert' i]"
            ):
                noise.decompose()
            paragraphs = [
                _clean_text(paragraph.get_text(" ", strip=True))
                for paragraph in copy.select("p")
            ]
            body_characters = sum(
                len(paragraph) for paragraph in paragraphs if paragraph
            )
            if len([value for value in paragraphs if value]) >= 2 and (
                body_characters
                >= _MINIMUM_SYNDICATED_BODY_CHARACTERS
            ):
                return copy
    return None


def _bloomberg_partner_body(soup: BeautifulSoup) -> Tag | None:
    for node in soup.select("[class*='storyContent' i]"):
        paragraphs = [
            _clean_text(paragraph.get_text(" ", strip=True))
            for paragraph in node.select("p")
        ]
        substantial = [value for value in paragraphs if value]
        if (
            len(substantial) >= 2
            and sum(len(value) for value in substantial)
            >= _MINIMUM_SYNDICATED_BODY_CHARACTERS
        ):
            return node
    return None


def _bloomberg_embedded_article_body(soup: BeautifulSoup) -> Tag | None:
    candidates: list[Tag] = []
    for script in soup.select('script[type="application/json"]'):
        value = script.string or script.get_text()
        if not value.strip():
            continue
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_json_objects(payload):
            document = item.get("body")
            if (
                not isinstance(document, dict)
                or document.get("type") != "document"
            ):
                continue
            rendered = _render_bloomberg_document(document)
            if rendered is not None:
                candidates.append(rendered)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda node: len(node.get_text(" ", strip=True)),
    )


def _render_bloomberg_document(document: dict[str, Any]) -> Tag | None:
    parsed = BeautifulSoup(
        "<div data-jojo-source='bloomberg-embedded-body'></div>",
        "html.parser",
    )
    wrapper = parsed.select_one("div")
    if wrapper is None:
        return None

    def text_content(value: object) -> str:
        if isinstance(value, dict):
            if value.get("type") == "text":
                return _string_or_none(value.get("value")) or ""
            children = value.get("content")
            if not isinstance(children, list):
                return ""
            return "".join(
                text_content(child) for child in children
            )
        if isinstance(value, list):
            return "".join(text_content(child) for child in value)
        return ""

    for block in document.get("content", []):
        if not isinstance(block, dict):
            continue
        block_type = _string_or_none(block.get("type")) or ""
        text = _clean_text(text_content(block))
        if block_type in {"paragraph", "blockquote"} and text:
            tag = parsed.new_tag("blockquote" if block_type == "blockquote" else "p")
            tag.string = text
            wrapper.append(tag)
        elif block_type == "heading" and text:
            level = block.get("data", {}).get("level", 2)
            level = level if isinstance(level, int) and 2 <= level <= 6 else 2
            tag = parsed.new_tag(f"h{level}")
            tag.string = text
            wrapper.append(tag)
        elif block_type in {"list", "unordered-list", "ordered-list"}:
            items = [
                _clean_text(text_content(child))
                for child in block.get("content", [])
                if isinstance(child, dict)
            ]
            items = [item for item in items if item]
            if items:
                list_tag = parsed.new_tag(
                    "ol" if block_type == "ordered-list" else "ul"
                )
                for item in items:
                    item_tag = parsed.new_tag("li")
                    item_tag.string = item
                    list_tag.append(item_tag)
                wrapper.append(list_tag)

        for child in _walk_json_objects(block):
            if child.get("type") == "embed":
                embed_url = _first_text(
                    _string_or_none(child.get("href")),
                    _string_or_none(
                        (child.get("iframeData") or {}).get("url")
                    )
                    if isinstance(child.get("iframeData"), dict)
                    else None,
                )
                if embed_url:
                    iframe = parsed.new_tag("iframe", src=embed_url)
                    wrapper.append(iframe)
            if child.get("type") == "media":
                data = child.get("data")
                if not isinstance(data, dict):
                    continue
                video = data.get("video")
                if isinstance(video, dict):
                    source = _string_or_none(video.get("src"))
                    if source:
                        iframe = parsed.new_tag("iframe", src=source)
                        wrapper.append(iframe)
    if wrapper.select_one("p, h2, h3, h4, h5, h6, blockquote, ul, ol, iframe"):
        return wrapper
    return None


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
        nodes = [node for node in soup.select(selector) if isinstance(node, Tag)]
        if nodes:
            return max(nodes, key=lambda node: len(node.get_text(" ", strip=True)))
    return None


def _nyt_story_body_companions(soup: BeautifulSoup) -> Tag | None:
    nodes = [
        node
        for node in soup.select(".StoryBodyCompanionColumn")
        if not any(
            isinstance(parent, Tag)
            and "StoryBodyCompanionColumn" in (parent.get("class") or [])
            for parent in node.parents
        )
    ]
    if len(nodes) < 2:
        return None
    document = BeautifulSoup(
        "<div data-jojo-source='nyt-story-companions'></div>",
        "html.parser",
    )
    wrapper = document.select_one("div")
    if wrapper is None:
        return None
    for node in nodes:
        copy = BeautifulSoup(str(node), "html.parser").select_one(
            ".StoryBodyCompanionColumn"
        )
        if copy is not None:
            wrapper.append(copy)
    return wrapper if wrapper.get_text(" ", strip=True) else None


def _nyt_legacy_article_body(soup: BeautifulSoup) -> Tag | None:
    nodes = [
        node
        for node in soup.select(".articleBody")
        if not any(
            isinstance(parent, Tag)
            and "articleBody" in (parent.get("class") or [])
            for parent in node.parents
        )
    ]
    if not nodes:
        return None
    document = BeautifulSoup(
        "<div data-jojo-source='nyt-legacy-article-body'></div>",
        "html.parser",
    )
    wrapper = document.select_one("div")
    if wrapper is None:
        return None
    for node in nodes:
        copy = BeautifulSoup(str(node), "html.parser").select_one(
            ".articleBody"
        )
        if copy is not None:
            wrapper.append(copy)
    return wrapper if wrapper.select_one('[itemprop="articleBody"], p') else None


def _nyt_watching_body(soup: BeautifulSoup) -> Tag | None:
    main = soup.select_one("main")
    if not isinstance(main, Tag):
        return None
    document = BeautifulSoup(
        "<div data-jojo-source='nyt-watching'></div>",
        "html.parser",
    )
    wrapper = document.select_one("div")
    if wrapper is None:
        return None
    lead = main.select_one(".WatchingHeader__header figure")
    if isinstance(lead, Tag):
        wrapper.append(BeautifulSoup(str(lead), "html.parser"))
    seen: set[str] = set()
    for node in main.select(
        ".Interactive__figure > h2, "
        ".interactive-graphic h1, "
        ".interactive-graphic .summary, "
        ".interactive-graphic .cards a, "
        ".interactive-graphic .footer .title"
    ):
        text = _clean_text(node.get_text(" ", strip=True))
        identity = text.casefold()
        if not text or identity in seen:
            continue
        seen.add(identity)
        name = node.name if node.name in {"h1", "h2", "h3"} else "p"
        rendered = document.new_tag(name)
        rendered.string = text
        wrapper.append(rendered)
    text = _clean_text(wrapper.get_text(" ", strip=True))
    return wrapper if len(text) >= _MINIMUM_BODY_CHARACTERS else None


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


def _nyt_interactive_body(soup: BeautifulSoup) -> Tag | None:
    for selector in (
        ".g-story.g-freebird",
        ".interactive-graphic",
        ".interactive-body",
        "section.interactive-content",
    ):
        for candidate in soup.select(selector):
            if len(_clean_text(candidate.get_text(" ", strip=True))) >= 200:
                quiz_body = _nyt_interactive_quiz_body(candidate)
                if quiz_body is not None:
                    return quiz_body
                return candidate
    return None


def _nyt_interactive_quiz_body(candidate: Tag) -> Tag | None:
    questions = candidate.select(".multiple-choice-question")
    if len(questions) < 2:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for question in questions:
        figure = question.select_one("figure")
        if isinstance(figure, Tag):
            figure_copy = BeautifulSoup(str(figure), "html.parser").find(
                "figure"
            )
            if isinstance(figure_copy, Tag):
                article.append(figure_copy)
        prompt = _tag_text(question.select_one(".question-text"))
        if prompt:
            heading = document.new_tag("h2")
            heading.string = prompt
            article.append(heading)
        answers = [
            text
            for node in question.select(".answer-text")
            if (text := _tag_text(node))
        ]
        if answers:
            answer_list = document.new_tag("ul")
            for answer in answers:
                item = document.new_tag("li")
                item.string = answer
                answer_list.append(item)
            article.append(answer_list)
    return article if len(article.select("h2")) >= 2 else None


def _structured_image_gallery(soup: BeautifulSoup) -> Tag | None:
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
            if not (
                isinstance(types, list)
                and "ImageGallery" in types
            ):
                continue
            media = item.get("associatedMedia")
            if not isinstance(media, list):
                continue
            rows: list[tuple[str, str, str | None]] = []
            for image in media:
                if not isinstance(image, dict):
                    continue
                image_url = _first_text(
                    _string_or_none(image.get("contentUrl")),
                    _string_or_none(image.get("url")),
                )
                caption = _string_or_none(image.get("caption"))
                if not image_url or not caption:
                    continue
                creator = image.get("creator")
                credit = (
                    _string_or_none(creator.get("name"))
                    if isinstance(creator, dict)
                    else _string_or_none(creator)
                )
                rows.append((image_url, caption, credit))
            if len(rows) < 3:
                continue
            document = BeautifulSoup("<article></article>", "html.parser")
            article = document.article
            if not isinstance(article, Tag):
                return None
            for image_url, caption, credit in rows:
                figure = document.new_tag("figure")
                image_node = document.new_tag("img")
                image_node["src"] = image_url
                image_node["alt"] = caption
                figure.append(image_node)
                figcaption = document.new_tag("figcaption")
                figcaption.string = (
                    f"{caption} Photographer: {credit}"
                    if credit
                    else caption
                )
                figure.append(figcaption)
                article.append(figure)
            return article
    return None


def _wsj_amp_story_gallery(soup: BeautifulSoup) -> Tag | None:
    pages = soup.select("amp-story > amp-story-page")
    if len(pages) < 3:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for page in pages:
        image = page.select_one(
            "amp-img[media*='landscape' i], amp-img"
        )
        if not isinstance(image, Tag):
            continue
        source = _string_or_none(image.get("src"))
        if not source:
            continue
        figure = document.new_tag("figure")
        image_node = document.new_tag("img")
        image_node["src"] = source
        width = _string_or_none(image.get("width"))
        height = _string_or_none(image.get("height"))
        if width:
            image_node["width"] = width
        if height:
            image_node["height"] = height
        caption = _tag_text(page.select_one(".wsj--caption"))
        credit = _tag_text(page.select_one(".wsj--credit"))
        if caption:
            image_node["alt"] = caption
        figure.append(image_node)
        if caption or credit:
            figcaption = document.new_tag("figcaption")
            figcaption.string = " ".join(
                value
                for value in (
                    caption,
                    f"Credit: {credit}" if credit else None,
                )
                if value
            )
            figure.append(figcaption)
        article.append(figure)
    return article if len(article.select("figure")) >= 3 else None


def _wsj_legacy_slideshow(soup: BeautifulSoup) -> Tag | None:
    slides = soup.select(".dj-slideshow .slide-wrapper:not(.thumbgrid-wrapper)")
    if len(slides) < 3:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for slide in slides:
        image = slide.select_one("img[src], img[data-src]")
        if not isinstance(image, Tag):
            continue
        source = _first_text(
            _string_or_none(image.get("src")),
            _string_or_none(image.get("data-src")),
        )
        if not source:
            continue
        credit = _first_text(
            _string_or_none(slide.get("data-credit")),
            _tag_text(slide.select_one(".caption-wrapper span")),
        )
        caption_node = slide.select_one(".caption-wrapper p")
        caption = None
        if isinstance(caption_node, Tag):
            caption_copy = BeautifulSoup(
                str(caption_node),
                "html.parser",
            )
            for credit_node in caption_copy.select("span"):
                credit_node.decompose()
            caption = _tag_text(caption_copy)
        figure = document.new_tag("figure")
        image_node = document.new_tag("img")
        image_node["src"] = source
        if caption:
            image_node["alt"] = caption
        figure.append(image_node)
        if caption or credit:
            figcaption = document.new_tag("figcaption")
            figcaption.string = " ".join(
                value
                for value in (
                    caption,
                    f"Credit: {credit}" if credit else None,
                )
                if value
            )
            figure.append(figcaption)
        article.append(figure)
    return article if len(article.select("figure")) >= 3 else None


def _ap_carousel_gallery(soup: BeautifulSoup) -> Tag | None:
    for carousel in soup.select(
        ".Page-main bsp-carousel.Carousel, "
        ".Page-main .Carousel"
    ):
        slides = carousel.select(".Carousel-slide")
        if len(slides) < 3:
            continue
        document = BeautifulSoup("<article></article>", "html.parser")
        article = document.article
        if not isinstance(article, Tag):
            return None
        for slide in slides:
            source_image = slide.select_one("img")
            if not isinstance(source_image, Tag):
                continue
            image = BeautifulSoup(
                str(source_image),
                "html.parser",
            ).find("img")
            if not isinstance(image, Tag):
                continue
            figure = document.new_tag("figure")
            figure.append(image)
            caption = _first_text(
                _tag_text(
                    slide.select_one(
                        ".CarouselSlide-caption, "
                        ".CarouselSlide-description, "
                        "[class*='caption' i]"
                    )
                ),
                _clean_text(source_image.get("alt", "")) or None,
            )
            if caption:
                figcaption = document.new_tag("figcaption")
                figcaption.string = caption
                figure.append(figcaption)
            article.append(figure)
        if len(article.select("figure")) >= 3:
            return article
    return None


def _ap_structured_race_call_body(
    news_article: dict[str, Any],
) -> Tag | None:
    if not news_article:
        return None
    keywords = _string_list(news_article.get("keywords"))
    description = _string_or_none(news_article.get("description"))
    if (
        not description
        or len(description) < _MINIMUM_BODY_CHARACTERS
        or not any("race call" in value.casefold() for value in keywords)
    ):
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    paragraph = document.new_tag("p")
    paragraph.string = description
    article.append(paragraph)
    return article


def _ap_data_bulletin_headline(
    news_article: dict[str, Any],
) -> str | None:
    if not news_article:
        return None
    keywords = _string_list(news_article.get("keywords"))
    for keyword in keywords:
        if re.search(r"(?i)(?:--.*\bbox\b|\bbox score\b)", keyword):
            return keyword
    if any(keyword.casefold() == "lotteries" for keyword in keywords):
        ignored = {"lotteries", "general news", "ap", "ap news"}
        return next(
            (
                keyword
                for keyword in keywords
                if keyword.casefold() not in ignored
            ),
            "Lottery results",
        )
    return None


def _is_ap_data_bulletin(
    news_article: dict[str, Any],
    canonical_url: str,
) -> bool:
    if not news_article:
        return False
    headline = _first_text(
        _string_or_none(news_article.get("headline")),
        _ap_data_bulletin_headline(news_article),
    )
    keywords = _string_list(news_article.get("keywords"))
    has_description = bool(
        _string_or_none(news_article.get("description"))
    )
    combined = " ".join(
        [headline or "", canonical_url, *keywords]
    ).casefold()
    return bool(
        re.search(r"--.*\bbox\b|\bbox score\b", combined)
        or re.search(
            r"(?:^|[-/])(?:[a-z]{2}-)?house-\d+-nominated(?:-|$)",
            combined,
        )
        or (
            not has_description
            and any(
                "race call" in keyword.casefold()
                for keyword in keywords
            )
        )
        or (
            not has_description
            and any(
                keyword.casefold() == "lotteries"
                for keyword in keywords
            )
        )
        or (
            not has_description
            and any(
                re.fullmatch(r"[a-z]{2}-winners", keyword.casefold())
                for keyword in keywords
            )
        )
    )


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _nyt_preloaded_state(soup: BeautifulSoup) -> dict[str, Any]:
    payload = _nyt_preloaded_payload(soup)
    state = payload.get("initialState")
    return state if isinstance(state, dict) else {}


def _nyt_preloaded_payload(soup: BeautifulSoup) -> dict[str, Any]:
    marker = "window.__preloadedData = "
    for script in soup.find_all("script"):
        value = script.string or script.get_text()
        if marker not in value:
            continue
        serialized = value.split(marker, 1)[1].strip().rstrip(";")
        # Some NYT releases serialize JavaScript `undefined` in otherwise valid
        # JSON. Those values are configuration-only and safely map to null.
        serialized = re.sub(
            r":\s*undefined(?=\s*[,}])",
            ": null",
            serialized,
        )
        try:
            payload = json.loads(serialized)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _nyt_state_reference(
    state: dict[str, Any],
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    reference = value.get("id")
    if isinstance(reference, str):
        resolved = state.get(reference)
        if isinstance(resolved, dict):
            return resolved
    return value


def _nyt_image_renditions(
    state: dict[str, Any],
    image: dict[str, Any],
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    pending: list[Any] = [image]
    visited: set[str] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        reference = value.get("id")
        if isinstance(reference, str) and reference in state:
            if reference in visited:
                continue
            visited.add(reference)
            pending.append(state[reference])
            continue
        if value.get("__typename") == "ImageRendition" and isinstance(
            value.get("url"), str
        ):
            found.append(value)
            continue
        pending.extend(value.values())
    return found


def _nyt_preloaded_image_gallery(soup: BeautifulSoup) -> Tag | None:
    state = _nyt_preloaded_state(soup)
    image_blocks = [
        value
        for key, value in state.items()
        if ".sprinkledBody.content" in key
        and isinstance(value, dict)
        and value.get("__typename") == "ImageBlock"
    ]
    rows: list[tuple[str, str | None, str | None]] = []
    seen_media: set[str] = set()
    for block in image_blocks:
        media_reference = block.get("media")
        media_id = (
            media_reference.get("id")
            if isinstance(media_reference, dict)
            else None
        )
        if not isinstance(media_id, str) or media_id in seen_media:
            continue
        seen_media.add(media_id)
        media = _nyt_state_reference(state, media_reference)
        if media is None:
            continue
        renditions = _nyt_image_renditions(state, media)
        if not renditions:
            continue
        rendition = max(
            renditions,
            key=lambda item: (
                int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("width") or 0),
            ),
        )
        caption_value = _nyt_state_reference(state, media.get("caption"))
        caption = None
        if caption_value is not None:
            caption = _first_text(
                _string_or_none(caption_value.get("text")),
                _string_or_none(caption_value.get("html")),
            )
        rows.append(
            (
                str(rendition["url"]),
                caption,
                _string_or_none(media.get("credit")),
            )
        )
    if len(rows) < 3:
        rows = _nyt_preloaded_slideshow_rows(state)
    if len(rows) < 3:
        rows = _nyt_denormalized_gallery_rows(soup)
    if len(rows) < 3:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for image_url, caption, credit in rows:
        figure = document.new_tag("figure")
        image = document.new_tag("img")
        image["src"] = image_url
        if caption:
            image["alt"] = caption
        figure.append(image)
        if caption or credit:
            figcaption = document.new_tag("figcaption")
            figcaption.string = " ".join(
                value for value in (caption, credit) if value
            )
            figure.append(figcaption)
        article.append(figure)
    return article


def _nyt_preloaded_slideshow_rows(
    state: dict[str, Any],
) -> list[tuple[str, str | None, str | None]]:
    slideshow_references = [
        value.get("media")
        for value in state.values()
        if isinstance(value, dict)
        and value.get("__typename") == "SlideshowBlock"
    ]
    rows: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    for slideshow_reference in slideshow_references:
        slideshow = _nyt_state_reference(state, slideshow_reference)
        if slideshow is None:
            continue
        slides = slideshow.get("slides")
        if not isinstance(slides, list):
            continue
        for slide_reference in slides:
            slide = _nyt_state_reference(state, slide_reference)
            if slide is None:
                continue
            image = _nyt_state_reference(state, slide.get("image"))
            if image is None:
                continue
            renditions = _nyt_image_renditions(state, image)
            if not renditions:
                continue
            rendition = max(
                renditions,
                key=lambda item: (
                    int(item.get("width") or 0)
                    * int(item.get("height") or 0),
                    int(item.get("width") or 0),
                ),
            )
            url = str(rendition["url"])
            identity = _image_identity(url)
            if identity in seen:
                continue
            seen.add(identity)
            legacy_caption = _string_or_none(
                slide.get("legacyHtmlCaption")
            )
            caption = (
                _clean_text(
                    BeautifulSoup(
                        legacy_caption,
                        "html.parser",
                    ).get_text(" ")
                )
                if legacy_caption
                else None
            )
            rows.append(
                (
                    url,
                    caption,
                    _string_or_none(image.get("credit")),
                )
            )
    return rows


def _nyt_denormalized_gallery_rows(
    soup: BeautifulSoup,
) -> list[tuple[str, str | None, str | None]]:
    payload = _nyt_preloaded_payload(soup)
    initial_data = payload.get("initialData")
    if not isinstance(initial_data, dict):
        return []
    data = initial_data.get("data")
    article = data.get("article") if isinstance(data, dict) else None
    body = article.get("sprinkledBody") if isinstance(article, dict) else None
    if not isinstance(body, dict):
        return []

    rows: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()

    def add_image(image: Any) -> None:
        if not isinstance(image, dict):
            return
        renditions = [
            value
            for value in _walk_json_objects(image.get("crops", []))
            if value.get("__typename") == "ImageRendition"
            and isinstance(value.get("url"), str)
        ]
        if not renditions:
            return
        rendition = max(
            renditions,
            key=lambda item: (
                int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("width") or 0),
            ),
        )
        url = str(rendition["url"])
        identity = _image_identity(url)
        if identity in seen:
            return
        seen.add(identity)
        caption_value = image.get("caption")
        caption = (
            _first_text(
                _string_or_none(caption_value.get("text")),
                _string_or_none(caption_value.get("html")),
            )
            if isinstance(caption_value, dict)
            else None
        )
        caption = _first_text(
            caption,
            _string_or_none(image.get("legacyHtmlCaption")),
        )
        rows.append(
            (
                url,
                _clean_text(
                    BeautifulSoup(caption, "html.parser").get_text(" ")
                )
                if caption
                else None,
                _string_or_none(image.get("credit")),
            )
        )

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        typename = value.get("__typename")
        if typename == "ImageBlock":
            add_image(value.get("media"))
            return
        if typename == "DiptychBlock":
            add_image(value.get("imageOne"))
            add_image(value.get("imageTwo"))
            return
        for child in value.values():
            visit(child)

    visit(body.get("content", []))
    return rows


def _nyt_preloaded_article_body(
    soup: BeautifulSoup,
    *,
    canonical_url: str,
) -> Tag | None:
    state = _nyt_preloaded_state(soup)
    target = next(
        (
            value
            for value in state.values()
            if isinstance(value, dict)
            and value.get("__typename") == "Article"
            and value.get("url") == canonical_url
        ),
        None,
    )
    if not isinstance(target, dict):
        return None
    body = _nyt_state_reference(state, target.get("body"))
    if body is None:
        return None
    references = next(
        (
            value
            for key, value in body.items()
            if key.startswith("content") and isinstance(value, list)
        ),
        [],
    )
    paragraphs: list[str] = []

    def inline_text(value: Any, visited: set[str] | None = None) -> list[str]:
        visited = visited or set()
        if isinstance(value, list):
            return [
                text
                for child in value
                for text in inline_text(child, visited)
            ]
        if not isinstance(value, dict):
            return []
        reference = value.get("id")
        if (
            isinstance(reference, str)
            and reference in state
            and reference not in visited
        ):
            visited.add(reference)
            return inline_text(state[reference], visited)
        if (
            value.get("__typename") == "TextInline"
            and isinstance(value.get("text"), str)
        ):
            return [str(value["text"])]
        return [
            text
            for child in value.values()
            for text in inline_text(child, visited)
        ]

    for reference in references:
        block = _nyt_state_reference(state, reference)
        if block is None or block.get("__typename") not in {
            "ParagraphBlock",
            "Heading1Block",
            "Heading2Block",
            "Heading3Block",
            "SummaryBlock",
        }:
            continue
        text = " ".join(inline_text(block))
        text = _clean_text(text)
        if text:
            paragraphs.append(text)
    if len(paragraphs) < 2 or sum(map(len, paragraphs)) < 100:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for text in paragraphs:
        paragraph = document.new_tag("p")
        paragraph.string = text
        article.append(paragraph)
    return article


def _nyt_preloaded_article_metadata(
    soup: BeautifulSoup,
    *,
    canonical_url: str,
) -> dict[str, Any]:
    state = _nyt_preloaded_state(soup)
    target = next(
        (
            value
            for value in state.values()
            if isinstance(value, dict)
            and value.get("__typename") == "Article"
            and value.get("url") == canonical_url
        ),
        None,
    )
    if not isinstance(target, dict):
        return {}
    headline = _nyt_state_reference(state, target.get("headline"))
    authors: list[str] = []
    bylines = target.get("bylines")
    if isinstance(bylines, list):
        for byline_reference in bylines:
            byline = _nyt_state_reference(state, byline_reference)
            if byline is None:
                continue
            rendered = _string_or_none(byline.get("renderedRepresentation"))
            if rendered:
                rendered = re.sub(
                    r"^(?:By|Photographs? by|Reporting by)\s+",
                    "",
                    rendered,
                    flags=re.IGNORECASE,
                )
                authors.append(rendered)
    return {
        "headline": _first_text(
            _string_or_none(headline.get("default"))
            if headline is not None
            else None,
            _string_or_none(headline.get("default@stripHtml"))
            if headline is not None
            else None,
        ),
        "description": _string_or_none(target.get("summary")),
        "authors": authors,
        "published_at": _string_or_none(target.get("firstPublished")),
        "modified_at": _first_text(
            _string_or_none(target.get("lastModified")),
            _string_or_none(target.get("lastMajorModification")),
        ),
    }


def _nyt_media_content_type(
    soup: BeautifulSoup,
    *,
    default: ContentType,
    structured_image_gallery_selected: bool,
    canonical_url: str,
) -> ContentType:
    if structured_image_gallery_selected:
        return ContentType.GALLERY
    state = _nyt_preloaded_state(soup)
    body_types = {
        value.get("__typename")
        for key, value in state.items()
        if ".sprinkledBody.content" in key and isinstance(value, dict)
    }
    if "VideoBlock" in body_types:
        return ContentType.VIDEO
    if "InteractiveBlock" in body_types:
        return ContentType.INTERACTIVE
    tagline = _first_text(
        _meta_content(soup, "name", "nyt-collection:tagline"),
        _meta_content(soup, "property", "nyt-collection:tagline"),
    )
    if tagline and "cartoon" in tagline.casefold():
        return ContentType.GALLERY
    description = _first_text(
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "property", "og:description"),
    )
    url = canonical_url.casefold()
    if (
        description
        and "comic strip" in description.casefold()
        and (
            "/comics" in url
            or "-comics." in url
            or "/the-strip-" in url
        )
        and soup.select_one("article img, .story-body img, #story-body img")
    ):
        return ContentType.GALLERY
    page_text = soup.get_text(" ", strip=True).casefold()
    if (
        "editorial cartoonist" in page_text
        and soup.select_one("article img, main img, .story-body img")
    ):
        return ContentType.GALLERY
    return default


def _is_publisher_notice(
    *,
    headline: str | None,
    description: str | None,
    plain_text: str,
) -> bool:
    combined = " ".join(
        value for value in (headline, description, plain_text) if value
    ).casefold()
    return bool(
        re.search(
            r"\barticle was published in error\b|"
            r"\binadvertently published on this page\b",
            combined,
        )
    )


def _is_structured_short_record(
    *,
    spec: PublisherSpec,
    news_article: dict[str, Any],
    headline: str | None,
    plain_text: str,
) -> bool:
    if not headline:
        return False
    if spec.publisher == "reuters":
        combined = f"{headline}\n{plain_text}".casefold()
        return bool(
            len(plain_text) >= 40
            and (
                headline.casefold().startswith("brief-")
                or re.match(r"(?i)^标题新闻[：:]", headline)
                or "路透中文快讯将暂不做进一步报导" in combined
            )
        )
    if spec.publisher != "ap":
        return False
    keywords = news_article.get("keywords")
    if isinstance(keywords, str):
        keyword_values = [keywords]
    elif isinstance(keywords, list):
        keyword_values = [value for value in keywords if isinstance(value, str)]
    else:
        keyword_values = []
    metric_labels = re.findall(
        r"(?i)(?:calories|fat|sodium|sugar|protein|"
        r"carbohydrates?|price|rank(?:ing)?)"
        r"(?:\s*\([^)]{1,12}\))?\s*:",
        plain_text,
    )
    keyword_keys = {
        re.sub(r"[^a-z0-9]+", "", value.casefold())
        for value in keyword_values
    }
    ap_news_alert = bool(
        len(plain_text) >= 40
        and (
            "apalertanoticioso" in keyword_keys
            or "apnewsalert" in keyword_keys
        )
    )
    return bool(
        (
            re.match(r"^\s*#\d+\b", headline)
            and any(
                value.casefold() == "archive"
                for value in keyword_values
            )
            and len(metric_labels) >= 3
        )
        or ap_news_alert
    )


def _wsj_interactive_puzzle(
    soup: BeautifulSoup,
    article: dict[str, Any],
    canonical_url: str,
) -> bool:
    section = _string_or_none(article.get("articleSection")) if article else None
    has_puzzle_embed = soup.select_one(
        ".interactive-puzzle-template iframe, "
        ".puzzle-template-article-sector iframe, "
        "iframe[class*='puzzle' i]"
    )
    if not has_puzzle_embed:
        return False
    url = canonical_url.casefold()
    return bool(
        (section and "puzzle" in section.casefold())
        or any(token in url for token in ("acrostic", "crossword", "/puzzles/"))
    )


def _prefer_structured_body_with_media(
    body: Tag,
    *,
    structured_body: Tag,
) -> Tag:
    body_text = _clean_text(body.get_text(" ", strip=True))
    structured_text = _clean_text(
        structured_body.get_text(" ", strip=True)
    )
    if (
        len(body_text) >= _MINIMUM_BODY_CHARACTERS
        or len(structured_text) <= len(body_text)
    ):
        return body

    media_nodes = list(body.select("figure"))
    media_nodes.extend(
        node
        for node in body.select("iframe")
        if node.find_parent("figure") is None
    )
    media_nodes.extend(
        node
        for node in body.select("img")
        if node.find_parent("figure") is None
    )
    for media in media_nodes:
        clone_document = BeautifulSoup(str(media), "html.parser")
        clone = clone_document.find(media.name)
        if not isinstance(clone, Tag):
            continue
        if media.name == "img":
            wrapper = clone_document.new_tag("figure")
            clone.extract()
            wrapper.append(clone)
            structured_body.append(wrapper)
        else:
            structured_body.append(clone)
    return structured_body


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
        "pre",
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
        if name in {"p", "pre"}:
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
    caption_container = container
    if spec.publisher == "ap":
        carousel_slide = image_node.find_parent(
            class_=lambda value: value and "Carousel-slide" in value
        )
        if isinstance(carousel_slide, Tag):
            caption_container = carousel_slide
    caption, credit = _caption_credit(caption_container)
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


def _image_identity(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if host in {"ft.com", "www.ft.com"} and "/images/raw/" in parts.path:
        nested = unquote(parts.path.split("/images/raw/", 1)[1])
        for _ in range(4):
            if "/images/raw/" not in nested:
                break
            nested_parts = urlsplit(nested)
            nested = unquote(
                nested_parts.path.split("/images/raw/", 1)[1]
            )
        nested_parts = urlsplit(nested)
        if nested_parts.scheme in {"http", "https"} and nested_parts.netloc:
            return urlunsplit(
                (
                    nested_parts.scheme.casefold(),
                    nested_parts.netloc.casefold(),
                    nested_parts.path,
                    "",
                    "",
                )
            )
    if host == "d1e00ek4ebabms.cloudfront.net":
        return urlunsplit(
            (
                parts.scheme.casefold(),
                parts.netloc.casefold(),
                parts.path,
                "",
                "",
            )
        )
    wsj_image = (
        re.fullmatch(
            r"(/im-\d+)(?:/(?:social|portrait))?/?",
            parts.path,
            re.IGNORECASE,
        )
        if host == "images.wsj.net"
        else None
    )
    if wsj_image is not None:
        return urlunsplit(
            (
                parts.scheme.casefold(),
                parts.netloc.casefold(),
                wsj_image.group(1),
                "",
                "",
            )
        )
    return url


def _ft_image_led_article(
    article: dict[str, Any],
    *,
    body_characters: int,
    images: list[ImageCandidate],
) -> bool:
    if not article or body_characters >= _MINIMUM_BODY_CHARACTERS or not images:
        return False
    word_count = article.get("wordCount")
    article_body = _string_or_none(article.get("articleBody"))
    structured_image = article.get("image")
    if not isinstance(word_count, int) or word_count > 30:
        return False
    if not article_body or len(_clean_text(article_body)) >= 120:
        return False
    if not isinstance(structured_image, dict):
        return False
    width = structured_image.get("width")
    height = structured_image.get("height")
    return (
        isinstance(width, int)
        and isinstance(height, int)
        and width >= 800
        and height >= 600
    )


def _merge_candidate_urls(
    existing: ImageCandidate,
    incoming: ImageCandidate,
) -> None:
    for url in (
        incoming.original_url,
        *incoming.candidate_urls,
    ):
        if url not in existing.candidate_urls:
            existing.candidate_urls.append(url)


def _image_urls(image: Tag, *, base_url: str) -> list[str]:
    values: list[tuple[int, str]] = []
    for attribute in (
        "src",
        "data-src",
        "data-original",
        "data-image",
        "data-flickity-lazyload",
    ):
        normalized = _normalized_url(image.get(attribute), base_url=base_url)
        if normalized and urlsplit(normalized).scheme != "data":
            values.append((0, normalized))
    for attribute in (
        "srcset",
        "data-srcset",
        "data-flickity-lazyload-srcset",
    ):
        raw = image.get(attribute)
        if not isinstance(raw, str):
            continue
        for entry in raw.split(","):
            parts = entry.strip().split()
            if not parts:
                continue
            normalized = _normalized_url(parts[0], base_url=base_url)
            if not normalized or urlsplit(normalized).scheme == "data":
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
        if (
            normalized
            and not _is_placeholder_image_url(normalized)
            and normalized not in result
        ):
            result.append(normalized)
    return result


def _is_placeholder_image_url(url: str) -> bool:
    decoded = unquote(url).casefold()
    return any(
        marker in decoded
        for marker in (
            "/defaultshareimage",
            "/default-share-image",
            "/default_social",
            "/default-social",
            "/defaultpromocrop.",
        )
    )


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
    if "podcast" in url:
        return ContentType.AUDIO
    if "opinion" in url:
        return ContentType.OPINION
    if "video" in url:
        return ContentType.VIDEO
    if "/watching/" in url:
        return ContentType.INTERACTIVE
    if "interactive" in url or "/features/" in url:
        return ContentType.INTERACTIVE
    if isinstance(article_type, str) and article_type == "ReportageNewsArticle":
        return ContentType.ARTICLE
    return ContentType.ARTICLE


def _looks_like_gallery(
    blocks: list[ContentBlock],
    *,
    allow_uncaptioned: bool = False,
) -> bool:
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
    if not image_blocks or len(text_blocks) > 2:
        return False
    if (
        allow_uncaptioned
        and len(image_blocks) >= 3
        and text_characters < _MINIMUM_BODY_CHARACTERS
    ):
        return True
    return bool(
        caption_characters >= 100
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


def _nyt_visible_published_at(soup: BeautifulSoup) -> str | None:
    value = _tag_text(soup.select_one(".PostV2__datePublished"))
    if not value:
        return None
    for format_string in ("%B %d, %Y", "%b. %d, %Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(value, format_string)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).isoformat()
    return None


def _ft_legacy_published_at(soup: BeautifulSoup) -> str | None:
    value = _tag_text(
        soup.select_one(".fullstoryBody .time, .fullstory .time")
    )
    if not value:
        return None
    for format_string in ("%B %d, %Y %I:%M %p", "%b %d, %Y %I:%M %p"):
        try:
            parsed = datetime.strptime(value, format_string)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).isoformat()
    return None


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
