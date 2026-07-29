from __future__ import annotations

import ast
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
    dependent_resources: dict[str, bytes] | None = None,
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
    ft_crossword_selected = False
    if spec.publisher == "ap":
        gallery_body = _ap_carousel_gallery(soup)
        if gallery_body is not None:
            body = gallery_body
            structured_image_gallery_selected = True
        else:
            body = _ap_structured_race_call_body(news_article)
            if body is None:
                body = _ap_structured_description_body(news_article)
            if body is None:
                body = _ap_structured_data_bulletin_body(
                    news_article,
                    canonical_url,
                )
        ap_dom_body = _select_body(soup, spec)
        if ap_dom_body is not None and (
            body is None
            or len(_clean_text(ap_dom_body.get_text(" ", strip=True)))
            > len(_clean_text(body.get_text(" ", strip=True)))
        ):
            body = ap_dom_body
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
    if spec.publisher == "wsj":
        partner_body = _wsj_tovima_body(soup)
        if partner_body is not None:
            body = partner_body
        puzzle_body = _wsj_puzzle_body(soup, canonical_url=canonical_url)
        if puzzle_body is not None:
            body = puzzle_body
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
        bloomberg_feature_body = _bloomberg_feature_landing_body(soup)
        if bloomberg_feature_body is not None and (
            body is None
            or len(body.get_text(" ", strip=True))
            < len(bloomberg_feature_body.get_text(" ", strip=True))
        ):
            body = bloomberg_feature_body
        bloomberg_quiz_body = _bloomberg_embedded_quiz_body(soup)
        if bloomberg_quiz_body is not None:
            body = bloomberg_quiz_body
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
        adventure_body = _nyt_adventure_resource_body(
            soup,
            dependent_resources=dependent_resources or {},
        )
        if adventure_body is not None:
            body = adventure_body
    if body is None:
        body = _select_body(soup, spec)
    if spec.publisher == "nyt":
        legacy_interactive = _nyt_legacy_interactive_graphic(soup)
        if legacy_interactive is not None:
            body = legacy_interactive
        embedded_interactive = _nyt_embedded_interactive_lede(soup)
        if embedded_interactive is not None and (
            body is None
            or _nyt_noninteractive_body_length(body)
            < len(embedded_interactive.get_text(" ", strip=True))
        ):
            body = embedded_interactive
        legacy_newsgraphic = _nyt_legacy_newsgraphic_body(soup)
        if legacy_newsgraphic is not None:
            body = legacy_newsgraphic
        flex_interactive = _nyt_legacy_flex_body(soup)
        if flex_interactive is not None:
            body = flex_interactive
        interactive_documents = _nyt_interactive_document_body(
            soup,
            canonical_url=canonical_url,
        )
        if interactive_documents is not None:
            body = interactive_documents
        inline_interactive = _nyt_inline_interactive_media(
            soup,
            canonical_url=canonical_url,
        )
        if inline_interactive is not None:
            body_text = (
                _clean_text(body.get_text(" ", strip=True))
                if body is not None
                else ""
            )
            if body is None or len(body_text) < _MINIMUM_BODY_CHARACTERS:
                body = inline_interactive
            else:
                # Media-only wrappers supplement a prose interactive; they
                # must not replace an anthology's complete article text.
                for child in list(inline_interactive.children):
                    body.append(child)
        ballot_interactive = _nyt_balloteer_body(
            soup,
            canonical_url=canonical_url,
        )
        if ballot_interactive is not None:
            if body is None:
                body = ballot_interactive
            else:
                for child in list(ballot_interactive.children):
                    body.append(child)
        if "/interactive/" in canonical_url.casefold():
            redirect_interactive = _nyt_interactive_redirect_body(soup)
            if redirect_interactive is not None:
                body = redirect_interactive
            metadata_interactive = _nyt_interactive_metadata_body(soup)
            body_text = (
                _clean_text(body.get_text(" ", strip=True))
                if body is not None
                else ""
            )
            metadata_text = (
                _clean_text(
                    metadata_interactive.get_text(" ", strip=True)
                )
                if metadata_interactive is not None
                else ""
            )
            if metadata_interactive is not None and (
                body is None
                or body.select_one(
                    "p, h1, h2, h3, h4, li, table, figure, iframe"
                )
                is None
                or (
                    len(body_text) < 2 * _MINIMUM_BODY_CHARACTERS
                    and body.select_one("img[src], figure, iframe") is None
                    and len(metadata_text) > len(body_text)
                    and metadata_text.casefold()
                    not in body_text.casefold()
                )
            ):
                body = metadata_interactive
    if spec.publisher == "reuters":
        reuters_live_blog = _reuters_live_blog_body(soup)
        if reuters_live_blog is not None:
            body = reuters_live_blog
        else:
            modern_legacy_body = soup.select_one(
                "#rcs-articleContent #article-text"
            )
            if isinstance(modern_legacy_body, Tag):
                body = modern_legacy_body
            legacy_reuters_body = _reuters_legacy_article_body(soup)
            if legacy_reuters_body is not None:
                body = legacy_reuters_body
    if spec.publisher == "wsj":
        gallery_body = _structured_image_gallery(soup)
        if gallery_body is None:
            gallery_body = _wsj_amp_story_gallery(soup)
        if gallery_body is None:
            gallery_body = _wsj_webui_slideshow(soup)
        if gallery_body is None:
            gallery_body = _wsj_legacy_slideshow(soup)
        if gallery_body is None:
            gallery_body = _wsj_unsupported_media_gallery(soup)
        if gallery_body is not None:
            body = gallery_body
            structured_image_gallery_selected = True
    if spec.publisher == "nyt":
        gallery_body = _nyt_preloaded_image_gallery(soup)
        if gallery_body is None:
            gallery_body = _nyt_legacy_op_art_gallery(soup)
        if (
            gallery_body is not None
            and not nyt_interactive_body_selected
            and _nyt_should_select_gallery_body(soup, body=body)
        ):
            body = gallery_body
            structured_image_gallery_selected = True
        legacy_video_body = _nyt_legacy_lede_video_body(soup, body=body)
        if (
            legacy_video_body is not None
            and not structured_image_gallery_selected
            and not nyt_interactive_body_selected
        ):
            body = legacy_video_body
    if spec.publisher == "ft":
        crossword_body = _ft_crossword_body(soup, body=body)
        if crossword_body is not None:
            body = crossword_body
            ft_crossword_selected = True
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
    if spec.publisher == "ap":
        _remove_ap_body_promos(clean_body)
    if spec.publisher == "reuters":
        _trim_reuters_recirculation_tail(clean_body)
    if spec.publisher == "nyt":
        _trim_nyt_access_shell_tail(clean_body)
    _remove_noise(clean_body, spec)

    headline = _first_text(
        (
            "Bloomberg Tax Quiz"
            if (
                spec.publisher == "bloomberg"
                and "/features/2017-tax-quiz" in canonical_url.casefold()
                and soup.select_one("#quiz-container section.question")
            )
            else None
        ),
        _string_or_none(nyt_preloaded_metadata.get("headline")),
        _ap_structured_headline(news_article)
        if spec.publisher == "ap"
        else (
            _string_or_none(news_article.get("headline"))
            if news_article
            else None
        ),
        _ap_data_bulletin_headline(news_article)
        if spec.publisher == "ap"
        else None,
        _ap_wire_keyword_headline(news_article)
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
    if spec.publisher == "wsj":
        wsj_page_content_type = _clean_text(
            _meta_content(soup, "name", "page.content.type") or ""
        ).casefold()
        if wsj_page_content_type in {
            "gallery",
            "photo gallery",
            "photo-gallery",
            "slideshow",
        }:
            content_type = ContentType.GALLERY
    if (
        spec.publisher == "ap"
        and _is_ap_data_bulletin(news_article, canonical_url)
    ):
        content_type = ContentType.INTERACTIVE
    if spec.publisher == "ft" and ft_crossword_selected:
        content_type = ContentType.INTERACTIVE
    if (
        content_type == ContentType.ARTICLE
        and soup.select_one(
            "audio[data-audio-subtype='podcast'], "
            "audio source[type^='audio/']"
        )
        and not (
            spec.publisher == "bloomberg"
            and _bloomberg_article_narration(soup)
        )
    ):
        content_type = ContentType.AUDIO

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
            if (
                existing is None
                and spec.publisher == "bloomberg"
                and _bloomberg_low_resolution_image(image)
            ):
                caption_key = _clean_text(
                    image.caption or ""
                ).casefold()
                existing = next(
                    (
                        candidate
                        for candidate in images_by_url.values()
                        if candidate.role == ImageRole.LEAD
                        and caption_key
                        and _clean_text(
                            candidate.caption or ""
                        ).casefold()
                        == caption_key
                    ),
                    None,
                )
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
    embedded_nontext_content = bool(
        content_type
        in {
            ContentType.INTERACTIVE,
            ContentType.VIDEO,
            ContentType.AUDIO,
            ContentType.TRANSCRIPT,
        }
        and any(
            block.type in {BlockType.EMBED, BlockType.IMAGE}
            for block in blocks
        )
    )
    publisher_notice = _is_publisher_notice(
        headline=headline,
        description=description,
        plain_text=plain_text,
    )
    structured_short_record = _is_structured_short_record(
        spec=spec,
        soup=soup,
        news_article=news_article,
        headline=headline,
        plain_text=plain_text,
    )
    if (
        len(plain_text) < _MINIMUM_BODY_CHARACTERS
        and not image_led_gallery
        and not embedded_nontext_content
        and not publisher_notice
        and not structured_short_record
    ):
        warnings.append("body-too-short")
    if publisher_notice:
        warnings.append("publisher-notice")
    if structured_short_record:
        warnings.append("structured-short-record")
    if spec.publisher == "ft" and _ft_explicit_truncation_notice(soup):
        warnings.append("truncated-body")
    if spec.publisher == "bloomberg" and _bloomberg_teaser_shell(soup):
        warnings.append("truncated-body")
    if (
        spec.publisher == "wsj"
        and content_type == ContentType.ARTICLE
        and _wsj_legacy_ellipsis_truncation(plain_text)
    ):
        warnings.append("truncated-body")
    if not published_at:
        warnings.append("missing-published-at")
    if body is None:
        warnings.append("article-body-not-found")

    status = ArticleStatus.COMPLETE
    if "article-body-not-found" in warnings:
        status = ArticleStatus.UNSUPPORTED
    elif (
        "body-too-short" in warnings
        or "missing-headline" in warnings
        or "truncated-body" in warnings
    ):
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


def _wsj_tovima_body(soup: BeautifulSoup) -> Tag | None:
    """Select only the licensed WSJ copy from To Vima partner pages."""
    partner_url = _first_text(
        _meta_content(soup, "property", "og:url"),
        _tag_attribute(soup.select_one("link[rel='canonical']"), "href"),
    )
    if not partner_url or "tovima.com/" not in partner_url.casefold():
        return None
    body = soup.select_one(".post-body.main-content, .post-body.article-wrapper")
    return body if isinstance(body, Tag) else None


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


def _bloomberg_feature_landing_body(soup: BeautifulSoup) -> Tag | None:
    """Recover editorial indexes from Bloomberg's legacy feature template."""
    container = soup.select_one(".dvz-content2")
    if not isinstance(container, Tag):
        return None
    intro = container.select_one(".intro, .introWrap")
    index = container.select_one(".index, .grid")
    if (
        not isinstance(intro, Tag)
        or len(_clean_text(intro.get_text(" ", strip=True))) < 300
        or not isinstance(index, Tag)
        or len(index.select("a[href]")) < 3
    ):
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    paragraph = document.new_tag("p")
    paragraph.string = _clean_text(intro.get_text(" ", strip=True))
    article.append(paragraph)
    seen_headings: set[str] = set()
    for anchor in index.select("a[href]"):
        text = _tag_text(anchor)
        if not text or text.casefold() in seen_headings:
            continue
        seen_headings.add(text.casefold())
        heading = document.new_tag("h2")
        heading.string = text
        article.append(heading)
    seen_images: set[str] = set()
    for source_image in container.select("img[src]"):
        source = _tag_attribute(source_image, "src")
        if not source:
            continue
        identity = _image_identity(source)
        if identity in seen_images:
            continue
        seen_images.add(identity)
        figure = document.new_tag("figure")
        image = document.new_tag("img")
        image["src"] = source
        alt = _tag_attribute(source_image, "alt")
        if alt:
            image["alt"] = alt
        figure.append(image)
        article.append(figure)
    return article


def _bloomberg_embedded_quiz_body(soup: BeautifulSoup) -> Tag | None:
    container = soup.select_one("#quiz-container")
    if not isinstance(container, Tag):
        return None
    questions = container.select("section.question[id^='Q']")
    if len(questions) < 3:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    seen_images: set[str] = set()
    for question in questions:
        identifier = _string_or_none(question.get("id"))
        prompt = _tag_text(question.select_one(":scope > h2"))
        options = [
            text
            for option in question.select(
                ":scope > ol.quiz-answers > li"
            )
            if (text := _tag_text(option))
        ]
        if not prompt or len(options) < 2:
            continue
        heading = document.new_tag("h2")
        heading.string = prompt
        article.append(heading)
        image = question.select_one(".quiz-question img[src]")
        if isinstance(image, Tag):
            source = _string_or_none(image.get("src"))
            if source and _image_identity(source) not in seen_images:
                seen_images.add(_image_identity(source))
                figure = document.new_tag("figure")
                image_copy = document.new_tag("img")
                image_copy["src"] = source
                caption = _tag_text(
                    question.select_one(".quiz-question .captionline")
                )
                if caption:
                    image_copy["alt"] = caption
                figure.append(image_copy)
                credit = _tag_text(
                    question.select_one(".quiz-question .creditline")
                )
                if caption or credit:
                    figcaption = document.new_tag("figcaption")
                    figcaption.string = " ".join(
                        value for value in (caption, credit) if value
                    )
                    figure.append(figcaption)
                article.append(figure)
        option_list = document.new_tag("ul")
        for option in options:
            item = document.new_tag("li")
            item.string = option
            option_list.append(item)
        article.append(option_list)
        answer = (
            container.select_one(f"section.answer#A{identifier[1:]}")
            if identifier and identifier[1:].isdigit()
            else None
        )
        if isinstance(answer, Tag):
            explanations = [
                (len(text), text)
                for node in answer.find_all("div", recursive=False)
                if (
                    "navbuttons" not in (node.get("class") or [])
                    and "thisresult" not in (node.get("class") or [])
                    and (text := _tag_text(node))
                )
            ]
            if explanations:
                explanation = document.new_tag("p")
                explanation.string = max(explanations)[1]
                article.append(explanation)
    return (
        article
        if len(article.select("h2")) >= 3
        and len(_clean_text(article.get_text(" ", strip=True))) >= 500
        else None
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


def _json_ld_objects(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
    for script in soup.select('script[type="application/ld+json"]'):
        value = script.string or script.get_text()
        if not value.strip():
            continue
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        yield from _walk_json_objects(payload)


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
        for node in soup.select(".articleBody, #articleBody")
        if not any(
            isinstance(parent, Tag)
            and (
                "articleBody" in (parent.get("class") or [])
                or parent.get("id") == "articleBody"
            )
            for parent in node.parents
        )
    ]
    if not nodes:
        primary_nodes = [
            node
            for node in soup.select(
            "article.story.theme-main .story-body, "
            "article#story .story-body"
            )
            if not any(
                isinstance(parent, Tag)
                and "story-body" in (parent.get("class") or [])
                for parent in node.parents
            )
        ]
        if not primary_nodes:
            return None
        nodes = [
            node
            for primary in primary_nodes
            for node in primary.select(
                ".story-content, [itemprop='articleBody']"
            )
            if not any(
                isinstance(parent, Tag)
                and parent is not primary
                and (
                    "story-content" in (parent.get("class") or [])
                    or parent.get("itemprop") == "articleBody"
                )
                for parent in node.parents
            )
        ]
        if not nodes:
            nodes = [
                primary
                for primary in primary_nodes
                if primary.select_one("p, figure, table")
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
        copy = BeautifulSoup(str(node), "html.parser").find()
        if copy is not None:
            wrapper.append(copy)
    return wrapper if wrapper.select_one(
        '[itemprop="articleBody"], .story-content, p'
    ) else None


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
    raw_paragraphs = [
        paragraph
        for paragraph in re.split(r"\n\s*\n", value)
        if _clean_text(paragraph)
    ]
    if not raw_paragraphs:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for raw_paragraph in raw_paragraphs:
        image_match = re.match(
            r"^\s*\[(https?://[^\]\s]+)\]\s*(.*)$",
            raw_paragraph,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if image_match is not None:
            figure = document.new_tag("figure")
            image = document.new_tag("img")
            image["src"] = image_match.group(1)
            figure.append(image)
            article.append(figure)
            paragraph = _clean_text(image_match.group(2))
            if not paragraph:
                continue
        else:
            paragraph = _clean_text(raw_paragraph)
        node = document.new_tag("p")
        node.string = paragraph
        article.append(node)
    return article


def _nyt_interactive_body(soup: BeautifulSoup) -> Tag | None:
    # Some legacy packages are anthologies made from several independently
    # authored interactive articles.  Selecting the first graphic silently
    # drops all sibling stories, as on the 2019 Gamergate opinion package.
    story = soup.select_one("article.story.theme-interactive")
    if isinstance(story, Tag):
        story_sections = story.select(".rad-article")
        story_text = _clean_text(story.get_text(" ", strip=True))
        if len(story_sections) >= 2 and len(story_text) >= 400:
            return story
    for selector in (
        ".g-story.g-freebird",
        ".interactive-graphic",
        ".interactive-body",
        "section.interactive-content",
    ):
        for candidate in soup.select(selector):
            candidate_text = _clean_text(
                candidate.get_text(" ", strip=True)
            )
            if (
                len(candidate_text) >= 200
                or (
                    candidate.select_one("img[src], figure, iframe")
                    and (
                        selector == ".interactive-graphic"
                        or len(candidate_text) >= 30
                    )
                )
            ):
                quiz_body = _nyt_interactive_quiz_body(candidate)
                if quiz_body is not None:
                    return quiz_body
                div_body = _nyt_div_only_interactive_body(candidate)
                if div_body is not None:
                    return div_body
                return candidate
    return None


def _nyt_div_only_interactive_body(candidate: Tag) -> Tag | None:
    """Turn old graphics made entirely from semantic divs into text blocks."""
    plain_text_fallback = candidate.select_one("#timeline_plain_text")
    if isinstance(plain_text_fallback, Tag):
        text = _clean_text(
            plain_text_fallback.get_text(" ", strip=True)
        )
        if len(text) >= _MINIMUM_BODY_CHARACTERS:
            document = BeautifulSoup("<article></article>", "html.parser")
            article = document.article
            if isinstance(article, Tag):
                paragraph = document.new_tag("p")
                paragraph.string = text
                article.append(paragraph)
                return article
    if candidate.select_one("p, h1, h2, h3, h4, li, table"):
        return None
    sections = candidate.select(".g-section")
    if len(sections) < 2:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    intro = _tag_text(candidate.select_one(".g-intro"))
    if intro:
        paragraph = document.new_tag("p")
        paragraph.string = intro
        article.append(paragraph)
    for section in sections:
        for selector, name in (
            (".g-source", "p"),
            (".g-translation", "blockquote"),
        ):
            text = _tag_text(section.select_one(selector))
            if not text:
                continue
            node = document.new_tag(name)
            node.string = text
            article.append(node)
    return article if len(_clean_text(article.get_text(" ", strip=True))) >= 200 else None


def _nyt_interactive_metadata_body(soup: BeautifulSoup) -> Tag | None:
    """Keep useful metadata when a legacy interactive is only a JS shell."""
    description = _first_text(
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "property", "og:description"),
        _meta_content(soup, "name", "twitter:description"),
    )
    if not description:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    paragraph = document.new_tag("p")
    paragraph.string = description
    article.append(paragraph)
    return article


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


def _nyt_balloteer_body(
    soup: BeautifulSoup,
    *,
    canonical_url: str,
) -> Tag | None:
    """Preserve the data endpoint for NYT quizzes rendered by Balloteer."""
    if "/interactive/" not in canonical_url.casefold():
        return None
    ballot_slug: str | None = None
    for script in soup.select("script"):
        value = script.string or script.get_text()
        if "ballot_slug" not in value or "embed_init" not in value:
            continue
        match = re.search(
            r"""["']ballot_slug["']\s*:\s*["']([^"'\\]+)["']""",
            value,
        )
        if match:
            ballot_slug = match.group(1).strip()
            break
    if not ballot_slug or not re.fullmatch(r"[A-Za-z0-9._-]+", ballot_slug):
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    iframe = document.new_tag("iframe")
    iframe["src"] = (
        "https://www.nytimes.com/svc/int/balloteer/ballot/"
        f"{ballot_slug}"
    )
    iframe["title"] = f"Interactive quiz data: {ballot_slug}"
    iframe["data-interactive-provider"] = "nyt-balloteer"
    article.append(iframe)
    return article


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
    slides = soup.select(
        ".dj-slideshow .slide-wrapper:not(.thumbgrid-wrapper), "
        ".wsj-slideshow-slide:not(.explore-more-slide)"
    )
    if len(slides) < 2:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for slide in slides:
        image = slide.select_one("img[src], img[data-src]")
        content_url = slide.select_one(
            "meta[itemprop='contentUrl'][content], "
            "meta[property='contentUrl'][content]"
        )
        source = _first_text(
            _string_or_none(content_url.get("content"))
            if isinstance(content_url, Tag)
            else None,
            _string_or_none(image.get("src"))
            if isinstance(image, Tag)
            else None,
            _string_or_none(image.get("data-src"))
            if isinstance(image, Tag)
            else None,
        )
        if not source:
            continue
        credit = _first_text(
            _string_or_none(slide.get("data-credit")),
            _tag_text(
                slide.select_one(
                    ".caption-wrapper span, "
                    "[itemprop='copyrightHolder'], "
                    ".credit"
                )
            ),
        )
        caption_node = slide.select_one(
            ".caption-wrapper p, [itemprop='caption'], "
            ".wsj-slideshow-caption, figcaption"
        )
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
    return article if len(article.select("figure")) >= 2 else None


def _wsj_webui_slideshow(soup: BeautifulSoup) -> Tag | None:
    rows: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    decoder = json.JSONDecoder()
    for script in soup.find_all("script"):
        value = script.string or script.get_text()
        if "WEBUI_SLIDESHOWS" not in value:
            continue
        for match in re.finditer(r"\bstate\s*:\s*(?=\{)", value):
            try:
                state, _ = decoder.raw_decode(value, match.end())
            except json.JSONDecodeError:
                continue
            if not isinstance(state, dict):
                continue
            context = state.get("context")
            slides = (
                context.get("slides")
                if isinstance(context, dict)
                else None
            )
            if not isinstance(slides, list):
                continue
            for slide in slides:
                if not isinstance(slide, dict):
                    continue
                source = _string_or_none(slide.get("imageSrc"))
                if not source:
                    continue
                identity = _image_identity(source)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(
                    (
                        source,
                        _string_or_none(slide.get("caption")),
                        _string_or_none(slide.get("credit")),
                    )
                )
    if len(rows) < 3:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for source, caption, credit in rows:
        figure = document.new_tag("figure")
        image = document.new_tag("img")
        image["src"] = source
        if caption:
            image["alt"] = caption
        figure.append(image)
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
    return article


def _wsj_legacy_ellipsis_truncation(plain_text: str) -> bool:
    """Recognize short legacy archive captures cut off with a literal ellipsis."""
    text = plain_text.rstrip()
    return len(text) < 1_000 and bool(
        re.search(r"[A-Za-z][.]{3}$", text)
    )


def _wsj_unsupported_media_gallery(soup: BeautifulSoup) -> Tag | None:
    """Recover the synopsis when an old slideshow app cannot be replayed."""
    shell = soup.select_one(".wsj-snippet-body, .wsj-snippet-login")
    if not isinstance(shell, Tag):
        return None
    shell_text = shell.get_text(" ", strip=True).casefold()
    if (
        "media that is not currently supported" not in shell_text
        or soup.select_one(".slideshow-article") is None
    ):
        return None
    description = _first_text(
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "property", "og:description"),
    )
    if not description:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    paragraph = document.new_tag("p")
    paragraph.string = description
    article.append(paragraph)
    return article


def _bloomberg_article_narration(soup: BeautifulSoup) -> bool:
    """Distinguish Bloomberg's text-to-speech player from an audio story."""
    for heading in soup.select("h1, h2, h3, h4, [role='heading']"):
        if _clean_text(heading.get_text(" ", strip=True)).casefold() == (
            "listen to article"
        ):
            return True
    return bool(
        soup.select_one(
            "audio source[src*='assets.bwbx.io/s3/readings/'], "
            "audio[src*='assets.bwbx.io/s3/readings/']"
        )
    )


def _bloomberg_low_resolution_image(image: ImageCandidate) -> bool:
    return any(
        bool(
            re.search(
                r"/(?:60x-1|60x60)\.(?:avif|gif|jpe?g|png|webp)(?:[?#]|$)",
                url,
                flags=re.IGNORECASE,
            )
        )
        for url in image.candidate_urls
    )


def _promote_bloomberg_image_candidates(candidates: list[str]) -> list[str]:
    """Prefer Bloomberg's lossless-aspect 1200px rendition over a 60px lazy image."""
    promoted: list[str] = []
    for url in candidates:
        parsed = urlsplit(url)
        if parsed.hostname in {"assets.bwbx.io", "assets.bwbx.com"}:
            high_resolution = re.sub(
                r"/60x-1(?=\.(?:avif|gif|jpe?g|png|webp)(?:[?#]|$))",
                "/1200x-1",
                url,
                flags=re.IGNORECASE,
            )
            if high_resolution != url and high_resolution not in promoted:
                promoted.append(high_resolution)
        if url not in promoted:
            promoted.append(url)
    return promoted


def _promote_ft_image_candidates(candidates: list[str]) -> list[str]:
    """Prefer a 1200px FT Origami rendition while retaining source variants."""
    promoted: list[str] = []
    for url in candidates:
        parts = urlsplit(url)
        if (
            parts.hostname in {"ft.com", "www.ft.com"}
            and "/__origami/service/image/" in parts.path
        ):
            high_resolution = re.sub(
                r"([?&]width=)(?:[1-9]\d{0,2}|1[01]\d{2})(?=&|$)",
                r"\g<1>1200",
                url,
                flags=re.IGNORECASE,
            )
            if high_resolution != url and high_resolution not in promoted:
                promoted.append(high_resolution)
        if url not in promoted:
            promoted.append(url)
    return promoted


def _promote_reuters_image_candidates(candidates: list[str]) -> list[str]:
    """Prefer a full-size rendition for Reuters' legacy lazy image endpoint."""
    promoted: list[str] = []
    for url in candidates:
        parts = urlsplit(url)
        if (
            parts.hostname
            and parts.hostname.casefold().endswith("reutersmedia.net")
            and parts.path == "/resources/r/"
        ):
            high_resolution = re.sub(
                r"([?&]w=)(?:[1-9]\d{0,2}|1[01]\d{2})(?=&|$)",
                r"\g<1>1200",
                url,
                flags=re.IGNORECASE,
            )
            if high_resolution != url and high_resolution not in promoted:
                promoted.append(high_resolution)
        if url not in promoted:
            promoted.append(url)
    return promoted


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


def _reuters_legacy_article_body(soup: BeautifulSoup) -> Tag | None:
    """Convert Reuters' pre-2011 BR-delimited articleText into paragraphs."""
    source = soup.select_one("#articleText")
    if not isinstance(source, Tag):
        return None
    text = _clean_text(source.get_text(" ", strip=True))
    if len(text) < _MINIMUM_BODY_CHARACTERS:
        return None
    fragments = re.split(
        r"(?:<br\s*/?>\s*){2,}",
        source.decode_contents(),
        flags=re.IGNORECASE,
    )
    paragraphs = [
        _clean_text(
            BeautifulSoup(fragment, "html.parser").get_text(" ")
            if "<" in fragment
            else html_module.unescape(fragment)
        )
        for fragment in fragments
    ]
    paragraphs = [
        paragraph
        for paragraph in paragraphs
        if paragraph
        and not re.fullmatch(
            r"(?i)(?:editing by|reporting by)\b.*",
            paragraph,
        )
    ]
    if not paragraphs:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for value in paragraphs:
        paragraph = document.new_tag("p")
        paragraph.string = value
        article.append(paragraph)
    return article


def _reuters_live_blog_body(soup: BeautifulSoup) -> Tag | None:
    posting = next(
        (
            value
            for value in _json_ld_objects(soup)
            if value.get("@type") == "LiveBlogPosting"
        ),
        None,
    )
    if posting is None:
        return None
    updates = posting.get("liveBlogUpdate")
    if not isinstance(updates, list):
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    seen: set[tuple[str, str]] = set()
    for update in updates:
        if not isinstance(update, dict):
            continue
        headline = _string_or_none(update.get("headline"))
        raw_body = _string_or_none(update.get("articleBody"))
        body_text = (
            _clean_text(
                BeautifulSoup(raw_body, "html.parser").get_text(" ")
            )
            if raw_body
            else None
        )
        if body_text:
            body_text = re.sub(
                r"(?<=[a-z0-9)])(?=[A-Z](?:[a-z]{2,}|['’][a-z]))",
                ". ",
                body_text,
            )
            body_text = re.sub(
                r"(?i)\s*Trouble viewing video posts\?.*cookie settings\s*$",
                "",
                body_text,
            ).strip()
        if not headline and not body_text:
            continue
        identity = (headline or "", body_text or "")
        if identity in seen:
            continue
        seen.add(identity)
        if headline:
            heading = document.new_tag("h2")
            heading.string = headline
            article.append(heading)
        if body_text:
            paragraph = document.new_tag("p")
            paragraph.string = body_text
            article.append(paragraph)
    return article if len(article.get_text(" ", strip=True)) >= 80 else None


def _ap_structured_description_body(
    news_article: dict[str, Any],
) -> Tag | None:
    """Recover self-contained AP briefs stored only in JSON-LD descriptions."""
    if not news_article:
        return None
    description = _string_or_none(news_article.get("description"))
    keywords = _string_list(news_article.get("keywords"))
    is_score_bulletin = any(
        re.search(r"(?i)\b(?:prep\s+)?scores?\b", keyword)
        for keyword in keywords
    )
    is_archive_brief = any(
        keyword.casefold() == "archive"
        for keyword in keywords
    )
    if not description or (
        len(description) < _MINIMUM_BODY_CHARACTERS
        and not is_score_bulletin
        and not is_archive_brief
    ):
        return None
    if re.search(
        r"(?i)^(?:visit|view|click|subscribe)\b.*(?:\||edition|website)",
        description,
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


def _ap_structured_data_bulletin_body(
    news_article: dict[str, Any],
    canonical_url: str,
) -> Tag | None:
    """Represent AP metadata-only election/result wires without inventing prose."""
    if not _is_ap_data_bulletin(news_article, canonical_url):
        return None
    headline = _first_text(
        _string_or_none(news_article.get("headline")),
        _ap_data_bulletin_headline(news_article),
    )
    if not headline:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    paragraph = document.new_tag("p")
    paragraph.string = headline
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


def _ap_structured_headline(
    news_article: dict[str, Any],
) -> str | None:
    headline = (
        _string_or_none(news_article.get("headline"))
        if news_article
        else None
    )
    if headline and headline.casefold() in {"ap", "ap news"}:
        return None
    return headline


def _ap_wire_keyword_headline(
    news_article: dict[str, Any],
) -> str | None:
    """Use AP's descriptive wire slug when generic page metadata says AP News."""
    if not news_article:
        return None
    keywords = _string_list(news_article.get("keywords"))
    strict_wire_slug = next(
        (
            keyword
            for keyword in keywords
            if re.match(r"^[A-Z]{2,5}--\S", keyword)
        ),
        None,
    )
    if strict_wire_slug:
        return strict_wire_slug
    ignored = {
        "general news",
        "international news",
        "ap",
        "ap news",
        "archive",
    }
    return next(
        (
            keyword
            for keyword in keywords
            if keyword.casefold() not in ignored
            and "-" in keyword
            and len(keyword) >= 12
        ),
        None,
    )


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
    metadata_only_election_slug = bool(
        not has_description
        and headline
        and any(headline.casefold() == keyword.casefold() for keyword in keywords)
        and re.fullmatch(
            r"[a-z]{2}-(?=[a-z0-9-]*(?:"
            r"uncontested|nominated|winners?|topraces|"
            r"camend|house|sthou|delg-dist|cnty"
            r"))[a-z0-9-]+",
            headline.casefold(),
        )
    )
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
        or metadata_only_election_slug
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
            payload = {}
            for key in ("initialData", "initialState"):
                recovered = _json_object_after_key(
                    serialized,
                    key=key,
                )
                if recovered is not None:
                    payload[key] = recovered
        if isinstance(payload, dict):
            return payload
    return {}


def _json_object_after_key(
    serialized: str,
    *,
    key: str,
) -> dict[str, Any] | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*', serialized)
    if match is None:
        return None
    start = serialized.find("{", match.end())
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(serialized)):
        character = serialized[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(serialized[start:index + 1])
                except (json.JSONDecodeError, TypeError):
                    return None
                return value if isinstance(value, dict) else None
    return None


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
        rows = _nyt_preloaded_visual_story_rows(state)
    if len(rows) < 3:
        rows = _nyt_denormalized_gallery_rows(soup)
    if len(rows) < 3:
        rows = _nyt_legacy_slideshow_json_rows(soup)
    if len(rows) < 3:
        rows = _nyt_itemprop_gallery_rows(soup)
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
                value
                for value in (
                    caption,
                    f"Credit: {credit}" if credit else None,
                )
                if value
            )
            figure.append(figcaption)
        article.append(figure)
    return article


def _nyt_should_select_gallery_body(
    soup: BeautifulSoup,
    *,
    body: Tag | None,
) -> bool:
    """Do not replace substantive NYT prose merely because it has 3+ images."""
    state = _nyt_preloaded_state(soup)
    if any(
        isinstance(value, dict)
        and value.get("__typename") == "SlideshowBlock"
        for value in state.values()
    ):
        return True
    if any(
        '"imageslideshow"' in (script.string or script.get_text())
        for script in soup.select('script[type="application/json"]')
    ):
        return True
    page_type = _first_text(
        _meta_content(soup, "name", "PT"),
        _meta_content(soup, "name", "page.content.type"),
        _meta_content(soup, "name", "article.type"),
    )
    if page_type and page_type.casefold() in {
        "gallery",
        "photo gallery",
        "slideshow",
    }:
        return True
    if body is None:
        return True
    paragraphs = [
        _clean_text(paragraph.get_text(" ", strip=True))
        for paragraph in body.select("p")
        if paragraph.find_parent("figcaption") is None
    ]
    substantive = [text for text in paragraphs if text]
    return (
        len(substantive) < 2
        or sum(len(text) for text in substantive) < 300
    )


def _nyt_legacy_slideshow_json_rows(
    soup: BeautifulSoup,
) -> list[tuple[str, str | None, str | None]]:
    """Recover ordered images from NYT's pre-React slideshow JSON."""
    for script in soup.select('script[type="application/json"]'):
        raw = script.string or script.get_text()
        if not raw or '"imageslideshow"' not in raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        slideshow = payload.get("imageslideshow")
        slides = (
            slideshow.get("slides")
            if isinstance(slideshow, dict)
            else None
        )
        if not isinstance(slides, list):
            continue
        rows: list[tuple[str, str | None, str | None]] = []
        seen: set[str] = set()
        for slide in slides:
            if not isinstance(slide, dict):
                continue
            crops = slide.get("image_crops")
            if not isinstance(crops, dict):
                continue
            renditions = [
                value
                for value in crops.values()
                if isinstance(value, dict)
                and isinstance(value.get("url"), str)
            ]
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
            caption_value = slide.get("caption")
            caption_html = (
                _first_text(
                    _string_or_none(caption_value.get("full")),
                    _string_or_none(caption_value.get("short")),
                )
                if isinstance(caption_value, dict)
                else None
            )
            rows.append(
                (
                    url,
                    _clean_text(
                        BeautifulSoup(
                            caption_html,
                            "html.parser",
                        ).get_text(" ")
                    )
                    if caption_html
                    else None,
                    _string_or_none(slide.get("credit")),
                )
            )
        if len(rows) >= 3:
            return rows
    return []


def _nyt_legacy_lede_video_body(
    soup: BeautifulSoup,
    *,
    body: Tag | None,
) -> Tag | None:
    """Preserve the destination of old NYT video-led short articles."""
    lead = soup.select_one(
        "figure.video.lede[data-videoid], "
        "figure.media.video.lede"
    )
    if not isinstance(lead, Tag):
        return None
    link = lead.select_one(
        "a.video-link[href], a[href*='/video/'][href]"
    )
    if not isinstance(link, Tag):
        return None
    destination = _string_or_none(link.get("href"))
    if not destination:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    if body is not None:
        body_copy = BeautifulSoup(str(body), "html.parser")
        copied_root = body_copy.find(body.name)
        if isinstance(copied_root, Tag):
            article.append(copied_root)
    iframe = document.new_tag("iframe")
    iframe["src"] = destination
    iframe["title"] = (
        _tag_text(lead.select_one(".headline"))
        or "Related New York Times video"
    )
    article.append(iframe)
    return article


def _ft_crossword_body(
    soup: BeautifulSoup,
    *,
    body: Tag | None,
) -> Tag | None:
    """Preserve the downloadable puzzle asset on FT crossword pages."""
    headline = _first_text(
        _meta_content(soup, "property", "og:title"),
        _tag_text(soup.select_one("h1")),
    )
    if not headline or "crossword" not in headline.casefold():
        return None
    link = next(
        (
            candidate
            for candidate in soup.select("a[href]")
            if "crossword pdf"
            in _clean_text(candidate.get_text(" ", strip=True)).casefold()
        ),
        None,
    )
    if not isinstance(link, Tag):
        return None
    destination = _string_or_none(link.get("href"))
    if not destination:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    if body is not None:
        body_copy = BeautifulSoup(str(body), "html.parser")
        copied_root = body_copy.find(body.name)
        if isinstance(copied_root, Tag):
            article.append(copied_root)
    iframe = document.new_tag("iframe")
    iframe["src"] = destination
    iframe["title"] = "Download crossword PDF"
    article.append(iframe)
    return article


def _nyt_preloaded_visual_story_rows(
    state: dict[str, Any],
) -> list[tuple[str, str | None, str | None]]:
    """Recover ordered NYT visual stories composed from image/diptych blocks."""
    body = next(
        (
            value
            for key, value in state.items()
            if key.endswith(".sprinkledBody")
            and isinstance(value, dict)
            and value.get("__typename") == "DocumentBlock"
        ),
        None,
    )
    if body is None:
        return []
    content = body.get("content@filterEmpty")
    if not isinstance(content, list):
        return []

    image_references: list[Any] = []
    for block_reference in content:
        block = _nyt_state_reference(state, block_reference)
        if block is None:
            continue
        block_type = block.get("__typename")
        if block_type == "ImageBlock":
            image_references.append(block.get("media"))
        elif block_type in {"DiptychBlock", "TriptychBlock"}:
            image_references.extend(
                block.get(key)
                for key in ("imageOne", "imageTwo", "imageThree")
            )
        elif isinstance(block_type, str) and block_type.startswith("Header"):
            lede_block = _nyt_state_reference(state, block.get("ledeMedia"))
            if lede_block is not None:
                image_references.append(lede_block.get("media"))

    rows: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    for image_reference in image_references:
        image = _nyt_state_reference(state, image_reference)
        if image is None:
            continue
        renditions = _nyt_image_renditions(state, image)
        if not renditions:
            continue
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
            continue
        seen.add(identity)
        legacy_caption = _string_or_none(image.get("legacyHtmlCaption"))
        caption_value = _nyt_state_reference(state, image.get("caption"))
        caption = (
            _clean_text(
                BeautifulSoup(legacy_caption, "html.parser").get_text(" ")
            )
            if legacy_caption
            else None
        )
        if caption is None and caption_value is not None:
            caption = _first_text(
                _string_or_none(caption_value.get("text")),
                _string_or_none(caption_value.get("html")),
            )
        rows.append(
            (
                url,
                caption,
                _string_or_none(image.get("credit")),
            )
        )
    return rows if len(rows) >= 3 else []


def _nyt_itemprop_gallery_rows(
    soup: BeautifulSoup,
) -> list[tuple[str, str | None, str | None]]:
    article = soup.select_one("article")
    if not isinstance(article, Tag):
        return []
    paragraph_characters = sum(
        len(text)
        for paragraph in article.select("p")
        if (
            (text := _clean_text(paragraph.get_text(" ", strip=True)))
            and paragraph.find_parent("header") is None
            and text.casefold() not in {"advertisement", "supported by"}
            and not text.casefold().startswith("by ")
        )
    )
    if paragraph_characters >= _MINIMUM_BODY_CHARACTERS:
        return []
    rows: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    for figure in article.select(
        "figure[itemid][itemtype*='ImageObject' i]"
    ):
        url = _string_or_none(figure.get("itemid"))
        if not url:
            continue
        identity = _image_identity(url)
        if identity in seen:
            continue
        seen.add(identity)
        caption = _tag_text(
            figure.select_one(
                "figcaption [itemprop='caption description'], "
                "figcaption"
            )
        )
        credit = _tag_text(
            figure.select_one(
                "[itemprop='copyrightHolder'], "
                "[itemprop='creditText']"
            )
        )
        rows.append((url, caption, credit))
    return rows if len(rows) >= 3 else []


def _nyt_legacy_op_art_gallery(soup: BeautifulSoup) -> Tag | None:
    lead_story = soup.select_one(".ledeStory")
    if not isinstance(lead_story, Tag):
        return None
    kicker = _tag_text(
        lead_story.select_one(".kicker, .storyHeader")
    )
    if not kicker or "op-art" not in kicker.casefold():
        return None
    source = _tag_attribute(lead_story.select_one("img[src]"), "src")
    if not source:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    figure = document.new_tag("figure")
    image = document.new_tag("img")
    image["src"] = source
    description = _tag_text(lead_story.select_one(".storySummary"))
    if description:
        image["alt"] = description
    figure.append(image)
    credit = _tag_text(
        soup.select_one(".interactiveFooter .module, .interactiveFooter")
    )
    if description or credit:
        figcaption = document.new_tag("figcaption")
        figcaption.string = " ".join(
            value for value in (description, credit) if value
        )
        figure.append(figcaption)
    article.append(figure)
    return article


def _nyt_adventure_resource_body(
    soup: BeautifulSoup,
    *,
    dependent_resources: dict[str, bytes],
) -> Tag | None:
    """Recover quiz prose serialized inside an archived Adventure JS bundle."""
    script_urls = {
        _normalized_url(
            str(script.get("src") or ""),
            base_url="https://www.nytimes.com/",
        )
        for script in soup.select(
            "#adventure-project-container script[src], "
            "section.interactive-content script[src]"
        )
    }
    matching_resources = [
        content
        for url, content in dependent_resources.items()
        if _normalized_url(url, base_url="https://www.nytimes.com/")
        in script_urls
    ]
    for content in matching_resources:
        javascript = content.decode("utf-8", errors="replace")
        for match in re.finditer(
            r"""JSON\.parse\('((?:\\.|[^'\\])*)'\)""",
            javascript,
        ):
            serialized = match.group(1)
            if '"entitiesById"' not in serialized:
                continue
            try:
                decoded = ast.literal_eval(f"'{serialized}'")
                payload = json.loads(decoded)
            except (SyntaxError, ValueError, json.JSONDecodeError):
                continue
            body = _nyt_adventure_entity_body(payload)
            if body is not None:
                return body
    return None


def _nyt_adventure_entity_body(payload: object) -> Tag | None:
    if not isinstance(payload, dict):
        return None
    entities = payload.get("entitiesById")
    root_id = payload.get("root")
    if not isinstance(entities, dict) or not isinstance(root_id, str):
        return None
    root = entities.get(root_id)
    if not isinstance(root, dict) or root.get("type") != "quiz":
        return None
    question_ids = [
        value
        for value in root.get("entities", [])
        if (
            isinstance(value, str)
            and isinstance(entities.get(value), dict)
            and entities[value].get("type") == "multiple_choice_question"
        )
    ]
    if len(question_ids) < 3:
        return None

    def entity_text(entity_id: object) -> str | None:
        entity = entities.get(entity_id)
        if not isinstance(entity, dict):
            return None
        data = entity.get("data")
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, str):
            return None
        # Adventure text supports Markdown plus small HTML fragments.
        rendered = BeautifulSoup(content, "html.parser").get_text(
            " ",
            strip=True,
        )
        rendered = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", rendered)
        return _clean_text(rendered) or None

    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for number, question_id in enumerate(question_ids, start=1):
        question = entities[question_id]
        children = question.get("entities", [])
        if not isinstance(children, list):
            continue
        prompt = next(
            (
                entity_text(child_id)
                for child_id in children
                if (
                    isinstance(entities.get(child_id), dict)
                    and entities[child_id].get("type") == "text"
                )
            ),
            None,
        )
        answers: list[tuple[str, bool]] = []
        explanation: str | None = None
        for child_id in children:
            child = entities.get(child_id)
            if not isinstance(child, dict):
                continue
            child_type = child.get("type")
            descendants = child.get("entities", [])
            if not isinstance(descendants, list):
                continue
            if child_type == "answer":
                answer_text = next(
                    (entity_text(value) for value in descendants if entity_text(value)),
                    None,
                )
                data = child.get("data")
                correct = bool(
                    isinstance(data, dict) and data.get("correct") is True
                )
                if answer_text:
                    answers.append((answer_text, correct))
            elif child_type == "response":
                explanation = next(
                    (
                        entity_text(value)
                        for value in descendants
                        if (
                            isinstance(entities.get(value), dict)
                            and entities[value].get("type") == "text"
                            and entity_text(value)
                        )
                    ),
                    None,
                )
        if not prompt or len(answers) < 2:
            continue
        heading = document.new_tag("h2")
        heading.string = f"{number}. {prompt}"
        article.append(heading)
        choices = document.new_tag("ul")
        for answer_text, _ in answers:
            item = document.new_tag("li")
            item.string = answer_text
            choices.append(item)
        article.append(choices)
        correct_answers = [text for text, correct in answers if correct]
        if correct_answers:
            answer_paragraph = document.new_tag("p")
            answer_paragraph.string = (
                "Correct answer: " + "; ".join(correct_answers)
            )
            article.append(answer_paragraph)
        if explanation:
            explanation_paragraph = document.new_tag("p")
            explanation_paragraph.string = explanation
            article.append(explanation_paragraph)
    text = _clean_text(article.get_text(" ", strip=True))
    return article if len(text) >= 500 else None


def _nyt_legacy_interactive_graphic(soup: BeautifulSoup) -> Tag | None:
    shell = soup.select_one("#interactiveShell, #main")
    freeform = soup.select_one("#interactiveFreeFormMain")
    if not isinstance(shell, Tag) or not isinstance(freeform, Tag):
        return None
    freeform_text = _clean_text(freeform.get_text(" ", strip=True))
    if (
        len(freeform_text) >= _MINIMUM_BODY_CHARACTERS
        and freeform.select_one("p, table, ul, ol, h2, h3") is not None
    ):
        # Some pre-React interactives put the complete, already-rendered
        # article (including comparison tables) in this container.  Rebuilding
        # it from only the deck and media silently discarded that prose.
        recovered_document = BeautifulSoup(str(freeform), "html.parser")
        recovered = recovered_document.select_one("#interactiveFreeFormMain")
        if isinstance(recovered, Tag):
            return recovered
    summary = _tag_text(shell.select_one(".storySummary .summary, .storySummary"))
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    if summary and not summary.casefold().startswith("related article"):
        paragraph = document.new_tag("p")
        paragraph.string = summary
        article.append(paragraph)
    seen_images: set[str] = set()
    for source_image in freeform.select("img[src]"):
        source = _tag_attribute(source_image, "src")
        if (
            not source
            or any(
                marker in source.casefold()
                for marker in (
                    "nytlogo",
                    "masthead-logo",
                    "/adx/",
                    "up.nytimes.com",
                    "wt.o.nytimes.com",
                    "unavailable-photo",
                )
            )
        ):
            continue
        identity = _image_identity(source)
        if identity in seen_images:
            continue
        seen_images.add(identity)
        figure = document.new_tag("figure")
        image = document.new_tag("img")
        image["src"] = source
        alt = _first_text(
            _string_or_none(source_image.get("alt")),
            summary if len(seen_images) == 1 else None,
        )
        if alt:
            image["alt"] = alt
        figure.append(image)
        article.append(figure)
    embed_rows: list[tuple[str, str | None]] = []
    for anchor in freeform.select("a[href]"):
        href = _string_or_none(anchor.get("href"))
        if href and re.search(r"(?i)\.(?:pdf|txt|csv)(?:$|[?#])", href):
            embed_rows.append((href, _tag_text(anchor)))
    for script in freeform.select("script"):
        value = script.string or script.get_text()
        match = re.search(
            r"""DV\.load\(\s*["'](?P<url>(?:https?:)?//"""
            r"""(?:www\.)?documentcloud\.org/documents/[^"']+?)"""
            r"""(?:\.js)?["']""",
            value,
            flags=re.IGNORECASE,
        )
        if match:
            url = match.group("url")
            if url.startswith("//"):
                url = f"https:{url}"
            url = re.sub(r"\.js$", "", url, flags=re.IGNORECASE)
            embed_rows.append((url, "DocumentCloud document"))
    seen_embeds: set[str] = set()
    for href, label in embed_rows:
        normalized = _normalized_url(href, base_url="https://www.nytimes.com/")
        if not normalized or normalized in seen_embeds:
            continue
        seen_embeds.add(normalized)
        iframe = document.new_tag("iframe")
        iframe["src"] = normalized
        if label:
            iframe["title"] = label
        article.append(iframe)
    sources = _tag_text(
        shell.select_one(
            "#interactiveFooter .sources, "
            "#interactiveFooter .credit"
        )
    )
    if sources:
        figcaption = document.new_tag("figcaption")
        figcaption.string = sources
        last_figure = article.find_all("figure")[-1] if article.find("figure") else None
        if isinstance(last_figure, Tag):
            last_figure.append(figcaption)
        else:
            article.append(figcaption)
    return article if article.select_one("p, figure, iframe") else None


def _nyt_embedded_interactive_lede(soup: BeautifulSoup) -> Tag | None:
    """Recover legacy NYT interactive ledes whose full story lives in a figure."""
    graphic = soup.select_one(
        "figure.interactive-embedded .interactive-graphic"
    )
    if not isinstance(graphic, Tag):
        return None
    if graphic.select_one("p, table, img[src]") is None:
        return None
    document = BeautifulSoup(str(graphic), "html.parser")
    recovered = document.select_one(".interactive-graphic")
    if not isinstance(recovered, Tag):
        return None
    # Table extraction preserves the comparison text as one structured block,
    # but images nested inside legacy table cells need explicit media blocks.
    for table in recovered.select("table"):
        insertion_point: Tag = table
        for source_image in table.select("img[src]"):
            figure = document.new_tag("figure")
            image = document.new_tag("img")
            for attribute in ("src", "data-src", "alt", "width", "height"):
                value = source_image.get(attribute)
                if value is not None:
                    image[attribute] = value
            figure.append(image)
            cell = source_image.find_parent(["td", "th"])
            caption = (
                _tag_text(cell.select_one(".caption"))
                if isinstance(cell, Tag)
                else None
            )
            if caption:
                figcaption = document.new_tag("figcaption")
                figcaption.string = caption
                figure.append(figcaption)
            insertion_point.insert_after(figure)
            insertion_point = figure
    return recovered


def _nyt_noninteractive_body_length(body: Tag) -> int:
    """Measure surrounding prose without counting an embedded graphic twice."""
    document = BeautifulSoup(str(body), "html.parser")
    copy = document.find()
    if not isinstance(copy, Tag):
        return 0
    for graphic in copy.select(
        "figure.interactive-embedded, .interactive-graphic"
    ):
        graphic.decompose()
    return len(_clean_text(copy.get_text(" ", strip=True)))


def _nyt_inline_interactive_media(
    soup: BeautifulSoup,
    *,
    canonical_url: str,
) -> Tag | None:
    """Recover image sequences embedded in JavaScript-only legacy graphics."""
    if (
        "/interactive/" not in canonical_url.casefold()
        and not soup.select_one("#interactiveShell")
    ):
        return None
    graphic = soup.select_one(".interactive-graphic")
    if not isinstance(graphic, Tag):
        return None
    scope = graphic.find_parent("article") or graphic
    urls: list[str] = []
    alt_by_identity: dict[str, str] = {}
    seen: set[str] = set()
    for source in scope.select("script, style"):
        value = (source.string or source.get_text()).replace("\\/", "/")
        for match in re.finditer(
            r"""(?i)(?:https?:)?//(?:graphics\d*|static\d*)"""
            r"""\.(?:nytimes|nyt)\.com/[^"'()<>\s]+?"""
            r"""\.(?:jpe?g|png|gif)(?:\?[^"'()<>\s]*)?""",
            value,
        ):
            url = match.group(0)
            if url.startswith("//"):
                url = f"https:{url}"
            identity = _image_identity(url)
            if identity in seen:
                continue
            seen.add(identity)
            urls.append(url)
    styled_nodes = list(scope.select("[style*='background-image']"))
    for node in soup.select(".g-victim-photo[style*='background-image']"):
        if node not in styled_nodes:
            styled_nodes.append(node)
    for node in styled_nodes:
        value = _string_or_none(node.get("style"))
        if not value:
            continue
        for match in re.finditer(
            r"""(?i)(?:https?:)?//(?:graphics\d*|static\d*)"""
            r"""\.(?:nytimes|nyt)\.com/[^"'()<>\s]+?"""
            r"""\.(?:jpe?g|png|gif)(?:\?[^"'()<>\s]*)?""",
            value.replace("\\/", "/"),
        ):
            url = match.group(0)
            if url.startswith("//"):
                url = f"https:{url}"
            identity = _image_identity(url)
            label = _first_text(
                _string_or_none(node.get("data-name")),
                _string_or_none(node.get("aria-label")),
            )
            if label:
                alt_by_identity[identity] = _clean_text(
                    label.replace("_", " ").replace("-", " ")
                )
            if identity in seen:
                continue
            seen.add(identity)
            urls.append(url)
    if len(urls) < 2:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    for url in urls:
        figure = document.new_tag("figure")
        image = document.new_tag("img")
        image["src"] = url
        alt = alt_by_identity.get(_image_identity(url))
        if alt:
            image["alt"] = alt
        figure.append(image)
        article.append(figure)
    return article


def _nyt_legacy_newsgraphic_body(soup: BeautifulSoup) -> Tag | None:
    """Recover malformed legacy graphics whose generated nodes escaped article."""
    if not soup.select_one(".interactive-graphic"):
        return None
    if not soup.select_one(".g-victim-photo, .g-item-image"):
        return None
    paragraphs = [
        node
        for node in soup.select(".g-body")
        if _tag_text(node)
    ]
    if len(paragraphs) < 5:
        return None
    if sum(len(_tag_text(node) or "") for node in paragraphs) < 500:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    seen_text: set[str] = set()
    seen_images: set[str] = set()
    for node in soup.select(".g-body, .g-item-image img[src]"):
        if node.name == "img":
            source = _tag_attribute(node, "src")
            if not source:
                continue
            identity = _image_identity(source)
            if identity in seen_images:
                continue
            seen_images.add(identity)
            figure = document.new_tag("figure")
            image = document.new_tag("img")
            image["src"] = source
            alt = _tag_attribute(node, "alt")
            if alt:
                image["alt"] = alt
            figure.append(image)
            article.append(figure)
            continue
        text = _tag_text(node)
        identity = text.casefold() if text else ""
        if not text or identity in seen_text:
            continue
        seen_text.add(identity)
        paragraph = document.new_tag("p")
        paragraph.string = text
        article.append(paragraph)
    return article if article.select_one("p") else None


def _nyt_legacy_flex_body(soup: BeautifulSoup) -> Tag | None:
    """Recover text, statistics and media from NYT's legacy LOOK template."""
    payload: dict[str, Any] | None = None
    for script in soup.select(
        "#interactiveFreeFormMain script, .interactive-graphic script"
    ):
        value = script.string or script.get_text()
        match = re.search(
            r"""(?s)function\s+getFlexData\s*\(\s*\)\s*\{\s*"""
            r"""return\s*(?P<payload>\{.*?\})\s*;\s*\}""",
            value,
        )
        if not match:
            continue
        try:
            candidate = json.loads(match.group("payload"))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    lede = data.get("lede")
    if isinstance(lede, dict):
        description = _string_or_none(lede.get("description"))
        if description:
            paragraph = document.new_tag("p")
            paragraph.string = description
            article.append(paragraph)
    tracks = data.get("tracks")
    if isinstance(tracks, dict):
        track_rows = tracks.get("track")
        if isinstance(track_rows, dict):
            track_rows = [track_rows]
        if isinstance(track_rows, list):
            for track in track_rows:
                if not isinstance(track, dict):
                    continue
                source = _string_or_none(track.get("source"))
                if not source:
                    continue
                audio = document.new_tag("audio")
                audio["src"] = source
                title = _string_or_none(track.get("title"))
                if title:
                    audio["title"] = title
                article.append(audio)
    item_columns = data.get("items")
    if isinstance(item_columns, list):
        for column in item_columns:
            stories = (
                column.get("story")
                if isinstance(column, dict)
                else None
            )
            if not isinstance(stories, list):
                continue
            for story in stories:
                if not isinstance(story, dict):
                    continue
                headline = _string_or_none(story.get("headline"))
                if headline:
                    heading = document.new_tag("h2")
                    heading.string = headline
                    article.append(heading)
                byline = _string_or_none(story.get("byline"))
                if byline:
                    paragraph = document.new_tag("p")
                    paragraph.string = _clean_text(
                        BeautifulSoup(
                            byline,
                            "html.parser",
                        ).get_text(" ")
                    )
                    article.append(paragraph)
                story_html = _string_or_none(story.get("text"))
                if story_html:
                    fragment = BeautifulSoup(story_html, "html.parser")
                    for child in list(fragment.contents):
                        article.append(child)
                for field in ("photo", "thumb", "bottom"):
                    source = _string_or_none(story.get(field))
                    if not source:
                        continue
                    figure = document.new_tag("figure")
                    image = document.new_tag("img")
                    image["src"] = source
                    if headline:
                        image["alt"] = headline
                    figure.append(image)
                    credit = _string_or_none(story.get("pcred"))
                    if credit:
                        figcaption = document.new_tag("figcaption")
                        figcaption.string = credit
                        figure.append(figcaption)
                    article.append(figure)
    column_two = data.get("col2")
    if isinstance(column_two, dict):
        text = _string_or_none(column_two.get("text"))
        if text:
            paragraph = document.new_tag("p")
            paragraph.string = text
            article.append(paragraph)
    slideshow = _string_or_none(data.get("gobig"))
    if slideshow:
        iframe = document.new_tag("iframe")
        iframe["src"] = slideshow
        iframe["title"] = "Slideshow"
        article.append(iframe)
    column_three = data.get("col3")
    if isinstance(column_three, dict):
        video = column_three.get("video")
        if isinstance(video, dict):
            promo = _string_or_none(video.get("promo"))
            if promo:
                figure = document.new_tag("figure")
                image = document.new_tag("img")
                image["src"] = promo
                image["alt"] = _first_text(
                    _string_or_none(video.get("title")),
                    "Video",
                )
                figure.append(image)
                caption = " ".join(
                    value
                    for value in (
                        _string_or_none(video.get("caption")),
                        _string_or_none(video.get("credit")),
                    )
                    if value
                )
                if caption:
                    figcaption = document.new_tag("figcaption")
                    figcaption.string = caption
                    figure.append(figcaption)
                article.append(figure)
        stats = column_three.get("stats")
        if isinstance(stats, list):
            rendered_stats = [
                (
                    _string_or_none(item.get("key")),
                    (
                        str(item.get("value"))
                        if isinstance(item.get("value"), (int, float))
                        else _string_or_none(item.get("value"))
                    ),
                )
                for item in stats
                if isinstance(item, dict)
            ]
            rendered_stats = [
                (key, value)
                for key, value in rendered_stats
                if key and value
            ]
            if rendered_stats:
                stats_list = document.new_tag("ul")
                for key, value in rendered_stats:
                    item = document.new_tag("li")
                    item.string = f"{key}: {value}"
                    stats_list.append(item)
                article.append(stats_list)
    return article if article.select_one("p, iframe, figure, li") else None


def _nyt_interactive_document_body(
    soup: BeautifulSoup,
    *,
    canonical_url: str,
) -> Tag | None:
    """Preserve linked source documents in later legacy interactive shells."""
    if (
        "/interactive/" not in canonical_url.casefold()
        and not soup.select_one("#interactiveShell")
    ):
        return None
    story = soup.select_one("article.story, .interactive-graphic")
    if not isinstance(story, Tag):
        return None
    documents: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for anchor in story.select("a[href]"):
        href = _string_or_none(anchor.get("href"))
        if not href or not re.search(
            r"(?i)\.(?:pdf|txt|csv)(?:$|[?#])",
            href,
        ):
            continue
        normalized = _normalized_url(href, base_url="https://www.nytimes.com/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        documents.append((normalized, _tag_text(anchor)))
    for script in story.select("script"):
        value = script.string or script.get_text()
        for match in re.finditer(
            r"""DV\.(?:flexLoad|load)\(\s*["']"""
            r"""(?P<url>(?:https?:)?//"""
            r"""(?:www\.)?documentcloud\.org/documents/[^"']+?)"""
            r"""(?:\.js)?["']""",
            value,
            flags=re.IGNORECASE,
        ):
            url = match.group("url")
            if url.startswith("//"):
                url = f"https:{url}"
            url = re.sub(r"\.js$", "", url, flags=re.IGNORECASE)
            normalized = _normalized_url(url, base_url="https://www.nytimes.com/")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            documents.append((normalized, "DocumentCloud document"))
    if not documents:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    description = _first_text(
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "name", "lp"),
        _meta_content(soup, "property", "og:description"),
    )
    if description:
        paragraph = document.new_tag("p")
        paragraph.string = description
        article.append(paragraph)
    for href, label in documents:
        iframe = document.new_tag("iframe")
        iframe["src"] = href
        if label:
            iframe["title"] = label
        article.append(iframe)
    return article


def _nyt_interactive_redirect_body(soup: BeautifulSoup) -> Tag | None:
    """Preserve metadata and destination for NYT's intentionally blank promos."""
    description = _first_text(
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "name", "lp"),
        _meta_content(soup, "property", "og:description"),
    )
    destination: str | None = None
    for script in soup.select("script"):
        value = script.string or script.get_text()
        match = re.search(
            r"""(?i)\bdestUrl\s*=\s*["']\s*(?P<url>https?://[^"']+)""",
            value,
        )
        if match:
            destination = match.group("url").strip()
            break
    if not destination:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    if description:
        paragraph = document.new_tag("p")
        paragraph.string = description
        article.append(paragraph)
    if destination:
        iframe = document.new_tag("iframe")
        iframe["src"] = destination
        iframe["title"] = "Interactive destination"
        article.append(iframe)
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
    if any(
        re.search(
            r"""(?i)["']source["']\s*:\s*["'][^"']+\.mp3(?:[?"']|$)""",
            (script.string or script.get_text()).replace("\\/", "/"),
        )
        for script in soup.select(
            "#interactiveFreeFormMain script, .interactive-graphic script"
        )
    ):
        return ContentType.AUDIO
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
    if soup.select_one(
        "figure.video.lede[data-videoid], "
        "figure.media.video.lede .video-link[href*='/video/']"
    ):
        return ContentType.VIDEO
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
    if (
        "/opinion/cartoon-" in url
        and soup.select_one("article img, main img, .story-body img")
    ):
        return ContentType.GALLERY
    if (
        soup.select_one(".interactive-headline")
        and soup.select_one(
            "img[src*='int.nyt.com/newsgraphics/'], "
            "img[data-src*='int.nyt.com/newsgraphics/']"
        )
    ):
        return ContentType.INTERACTIVE
    if soup.select_one(
        "figure.interactive-embedded .interactive-graphic"
    ):
        return ContentType.INTERACTIVE
    if (
        description
        and description.casefold().startswith("as interpreted by ")
        and soup.select_one(
            "#story-body img[src], .story-body img[src], #article img[src]"
        )
    ):
        return ContentType.GALLERY
    if (
        "/opinion/" in url
        and re.search(r"(?:^|[-_/])heng(?:[-_.]|$)", url)
        and soup.select_one("img[src*='hengart' i]")
    ):
        return ContentType.GALLERY
    legacy_story_body = soup.select_one(
        "article.story.theme-main .story-body"
    )
    legacy_story_image = (
        legacy_story_body.select_one(
            "figure[itemprop='associatedMedia'] img[src]"
        )
        if isinstance(legacy_story_body, Tag)
        else None
    )
    legacy_story_prose = (
        " ".join(
            node.get_text(" ", strip=True)
            for node in legacy_story_body.select(
                ".story-content[itemprop='articleBody'], "
                "p.story-body-text.story-content"
            )
        )
        if isinstance(legacy_story_body, Tag)
        else ""
    )
    if (
        legacy_story_image is not None
        and len(_clean_text(legacy_story_prose)) < _MINIMUM_BODY_CHARACTERS
    ):
        return ContentType.GALLERY
    if "/interactive/" in url and default == ContentType.LIVEBLOG:
        return ContentType.INTERACTIVE
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
            r"\binadvertently published on this page\b|"
            r"\b(?:article|feature) (?:has been|was) removed "
            r"because of a copyright dispute\b",
            combined,
        )
    )


def _is_structured_short_record(
    *,
    spec: PublisherSpec,
    soup: BeautifulSoup,
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
    if spec.publisher == "nyt":
        page_text = _clean_text(soup.get_text(" ", strip=True)).casefold()
        return bool(
            len(plain_text) >= 50
            and "sports briefing" in page_text
            and (
                "by the associated press" in page_text
                or "by associated press" in page_text
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
    ap_data_bulletin = (
        _is_ap_data_bulletin(news_article, "")
        and plain_text.casefold() == headline.casefold()
    )
    ap_score_bulletin = bool(
        len(plain_text) >= 40
        and any(
            re.search(r"(?i)\b(?:prep\s+)?scores?\b", value)
            for value in keyword_values
        )
    )
    description = _string_or_none(news_article.get("description"))
    ap_archive_brief = bool(
        len(plain_text) >= 40
        and description
        and plain_text == description
        and any(
            value.casefold() == "archive"
            for value in keyword_values
        )
        and not re.search(
            r"(?i)^(?:visit|view|click|subscribe)\b",
            plain_text,
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
        or ap_data_bulletin
        or ap_score_bulletin
        or ap_archive_brief
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
        "iframe[class*='puzzle' i], "
        "a[href*='/documents/'][href$='.pdf' i]"
    )
    if not has_puzzle_embed:
        return False
    url = canonical_url.casefold()
    return bool(
        (section and "puzzle" in section.casefold())
        or any(
            token in url
            for token in (
                "acrostic",
                "crossword",
                "cryptic-puzzle",
                "variety-puzzle",
                "number-puzzles",
                "/puzzles/",
            )
        )
    )


def _wsj_puzzle_body(
    soup: BeautifulSoup,
    *,
    canonical_url: str,
) -> Tag | None:
    links = [
        link
        for link in soup.select("a[href]")
        if (
            (href := _string_or_none(link.get("href")))
            and "/documents/" in href.casefold()
            and href.casefold().split("?", 1)[0].endswith(".pdf")
        )
    ]
    if not links:
        return None
    page_text = _clean_text(soup.get_text(" ", strip=True)).casefold()
    url = canonical_url.casefold()
    if "puzzle" not in page_text and "puzzle" not in url:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    seen: set[str] = set()
    for link in links:
        href = str(link.get("href"))
        if href in seen:
            continue
        seen.add(href)
        label = _tag_text(link) or "Download puzzle PDF"
        paragraph = document.new_tag("p")
        paragraph.string = label
        article.append(paragraph)
        iframe = document.new_tag("iframe")
        iframe["src"] = href
        iframe["title"] = label
        article.append(iframe)
    return article if seen else None


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
        text = _clean_text(node.get_text(" ", strip=True)).casefold()
        if text in _EXACT_NOISE_TEXT:
            node.decompose()
        elif (
            spec.publisher == "bloomberg"
            and text == "share this article"
        ):
            node.decompose()
        elif (
            spec.publisher == "bloomberg"
            and node.name in {"p", "li", "span"}
            and text.startswith(
                (
                    "want to receive this post in your inbox",
                    "sign up for next china",
                    "sign up here to receive the davos diary",
                    "sign up for the new economy daily newsletter",
                    "sign up for our middle east newsletter",
                    "sign up for our coming middle east newsletter",
                    "for the best in travel, food, drinks, fashion, cars, "
                    "and life, sign up for the pursuits newsletter",
                )
            )
        ):
            node.decompose()
        elif (
            spec.publisher == "ft"
            and node.name == "p"
            and text.startswith(
                "copyright the financial times limited"
            )
            and "please don't " in text
            and "articles from ft.com" in text
        ):
            node.decompose()
    if spec.publisher == "ft":
        _remove_ft_newsletter_promos(soup)
        _strip_ft_copyright_suffixes(soup)


def _remove_ft_newsletter_promos(soup: BeautifulSoup) -> None:
    """Remove newsletter cards flattened into FT syndication body paragraphs."""
    for heading in list(soup.select("h2, h3, h4")):
        heading_text = _clean_text(
            heading.get_text(" ", strip=True)
        ).casefold()
        card = heading.find_parent(
            class_=lambda value: value and "n-content-layout" in value
        )
        if not isinstance(card, Tag):
            continue
        card_text = _clean_text(card.get_text(" ", strip=True)).casefold()
        if (
            "newsletter" in heading_text
            or (
                heading_text == "house & home unlocked"
                and "newsletter" in card_text
            )
        ):
            card.decompose()
    cta_patterns = (
        re.compile(r"(?i)^sign up here with one click\b"),
        re.compile(r"(?i)^sign up here[.!]?$"),
        re.compile(
            r"(?i)^sign up for the newsletter by clicking here\b"
        ),
    )
    promo_patterns = (
        re.compile(r"(?i)\bnewsletter\b"),
        re.compile(r"(?i)\bin your inbox\b"),
        re.compile(r"(?i)^track trends in tech, media and telecoms\b"),
        re.compile(r"(?i)^house\s*&\s*home unlocked$"),
        re.compile(r"(?i)^follow @ft"),
    )
    direct_promo_patterns = (
        re.compile(
            r"(?i)^lex recommends the ft(?:'s|’s) .*newsletter\b"
        ),
        re.compile(
            r"(?i)^do you want to receive lex in your inbox\?\s*"
            r"sign up for the weekly best of lex email\b"
        ),
        re.compile(r"(?i)^our popular newsletter .*sign up here\b"),
        re.compile(r"(?i)^subscribers can use myft to follow\b"),
        re.compile(r"(?i)^follow ft(?:'s|’s) live coverage\b"),
        re.compile(r"(?i)^follow @ft"),
        re.compile(r"(?i)^join our online book group\b"),
        re.compile(
            r"(?i)^the ft is free to read today\.\s*"
            r"you can share this article\b"
        ),
    )
    for node in list(soup.select("p")):
        text = _clean_text(node.get_text(" ", strip=True))
        if any(pattern.search(text) for pattern in direct_promo_patterns):
            node.decompose()
            continue
        if not any(pattern.search(text) for pattern in cta_patterns):
            continue
        previous = node.find_previous_sibling()
        for _ in range(4):
            if not isinstance(previous, Tag):
                break
            earlier = previous.find_previous_sibling()
            previous_text = _clean_text(
                previous.get_text(" ", strip=True)
            )
            if not any(
                pattern.search(previous_text)
                for pattern in promo_patterns
            ):
                break
            previous.decompose()
            previous = earlier
        node.decompose()


def _strip_ft_copyright_suffixes(soup: BeautifulSoup) -> None:
    """Remove syndication copyright footers without dropping article prose."""
    pattern = re.compile(
        r"""(?i)\s*[–—-]\s*copyright\s+(?:the\s+)?"""
        r"""financial\s+times\s+limited(?:\s+\d{4})?\s*$"""
    )
    for text_node in list(soup.find_all(string=pattern)):
        cleaned = pattern.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()


def _remove_ap_body_promos(soup: BeautifulSoup) -> None:
    """Remove AP calls-to-action embedded as legacy body paragraphs."""
    patterns = (
        re.compile(
            r"(?i)\bsign up for (?:the )?ap(?:'s|’s) .*newsletter\b"
        ),
        re.compile(
            r"(?i)^for more lottery results,\s*go to jackpot\.com\b"
        ),
    )
    for node in list(soup.select("p")):
        text = _clean_text(node.get_text(" ", strip=True))
        if not any(pattern.search(text) for pattern in patterns):
            continue
        previous = node.find_previous_sibling()
        for _ in range(4):
            if not isinstance(previous, Tag):
                break
            earlier = previous.find_previous_sibling()
            if _clean_text(previous.get_text(" ", strip=True)) == "___":
                previous.decompose()
            previous = earlier
        node.decompose()


def _trim_reuters_recirculation_tail(soup: BeautifulSoup) -> None:
    """Drop modern Reuters recommendation modules appended inside body."""
    markers = list(
        soup.select(
            "[data-testid='Latest Updates'], "
            "[data-variant-id='article-latest-updates'], "
            "[class*='read-next-mobile__container']"
        )
    )
    for node in soup.select("p, div"):
        text = _clean_text(node.get_text(" ", strip=True)).casefold()
        if text.startswith(
            "our standards: the thomson reuters trust principles"
        ):
            markers.append(node)
    if not markers:
        return
    top = soup.find()
    if not isinstance(top, Tag):
        return
    marker_ids = {id(marker) for marker in markers}
    marker = next(
        (
            node
            for node in top.descendants
            if isinstance(node, Tag) and id(node) in marker_ids
        ),
        None,
    )
    if not isinstance(marker, Tag):
        return
    tail = marker
    while isinstance(tail.parent, Tag):
        for sibling in list(tail.next_siblings):
            if isinstance(sibling, Tag):
                sibling.decompose()
            else:
                sibling.extract()
        if tail.parent is top:
            break
        tail = tail.parent
    marker.decompose()


def _trim_nyt_access_shell_tail(soup: BeautifulSoup) -> None:
    """Remove verification/paywall UI appended after a recovered NYT body."""
    marker = next(
        (
            node
            for node in soup.select("p, div")
            if _clean_text(node.get_text(" ", strip=True))
            .casefold()
            .startswith(
                "thank you for your patience while we verify access"
            )
        ),
        None,
    )
    if not isinstance(marker, Tag):
        return
    top = soup.find()
    if not isinstance(top, Tag):
        return
    tail = marker
    while isinstance(tail.parent, Tag):
        for sibling in list(tail.next_siblings):
            if isinstance(sibling, Tag):
                sibling.decompose()
            else:
                sibling.extract()
        if tail.parent is top:
            break
        tail = tail.parent
    marker.decompose()


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
        "audio",
        "amp-brightcove",
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
        elif name == "audio":
            source_node = node.select_one("source[src]")
            source_value = (
                source_node.get("src")
                if isinstance(source_node, Tag)
                else node.get("src")
            )
            source = _normalized_url(source_value, base_url=base_url)
            if source:
                blocks.append(
                    ContentBlock(
                        type=BlockType.EMBED,
                        position=position,
                        embed_url=source,
                        html=str(node),
                    )
                )
        elif name == "amp-brightcove":
            account = _string_or_none(node.get("data-account"))
            player = _string_or_none(node.get("data-player")) or "default"
            embed = _string_or_none(node.get("data-embed")) or "default"
            video_id = _string_or_none(node.get("data-video-id"))
            if account and video_id:
                source = (
                    f"https://players.brightcove.net/{account}/"
                    f"{player}_{embed}/index.html?videoId={video_id}"
                )
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
        if (
            node.name == "img"
            and parent.name == "p"
            and not _clean_text(parent.get_text(" ", strip=True))
        ):
            parent = parent.parent
            continue
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
    if spec.publisher == "bloomberg":
        candidates = _promote_bloomberg_image_candidates(candidates)
    if spec.publisher == "ft":
        candidates = _promote_ft_image_candidates(candidates)
    if spec.publisher == "reuters":
        candidates = _promote_reuters_image_candidates(candidates)
    original_url = candidates[0]
    width = _integer_attribute(image_node, "width")
    height = _integer_attribute(image_node, "height")
    alt = _first_text(
        _clean_text(image_node.get("alt", "")),
        _clean_text(image_node.get("aria-label", "")),
    )
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
    if _is_placeholder_image_url(url):
        role = ImageRole.LOGO
        reasons = [*reasons, "generic-publisher-branding"]
    if spec.publisher == "nyt" and _nyt_generic_branding_image(url):
        role = ImageRole.LOGO
        reasons = [*reasons, "generic-publisher-branding"]
    identity = _image_identity(url)
    asset_id = (
        f"urlsha256:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
    )
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
            return _image_identity(
                urlunsplit(
                    (
                        nested_parts.scheme.casefold(),
                        nested_parts.netloc.casefold(),
                        nested_parts.path,
                        "",
                        "",
                    )
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
    if host.endswith("reutersmedia.net") and parts.path == "/resources/r/":
        legacy_id = re.search(
            r"(?:^|&)i=(\d+)(?:&|$)",
            parts.query,
            flags=re.IGNORECASE,
        )
        if legacy_id is not None:
            return f"reuters-image:{legacy_id.group(1)}"
    if host in {
        "prod-upp-image-read.ft.com",
        "com.ft.imagepublish.upp-prod-eu.s3.amazonaws.com",
    }:
        ft_asset = re.search(
            r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})(?:$|[./])",
            parts.path,
            re.IGNORECASE,
        )
        if ft_asset is not None:
            return f"ft-image:{ft_asset.group(1).casefold()}"
    if host == "assets.bwbx.io":
        bloomberg_asset = re.fullmatch(
            r"(.+/v\d+)/[^/]+",
            parts.path,
            re.IGNORECASE,
        )
        if bloomberg_asset is not None:
            return f"bloomberg-image:{bloomberg_asset.group(1).casefold()}"
    if host == "int.nyt.com" and "/newsgraphics/" in parts.path:
        responsive_path = re.sub(
            r"_(?:300|480|720|800|945)_v(?=\d+\.[a-z0-9]+$)",
            "_responsive_v",
            parts.path,
            flags=re.IGNORECASE,
        )
        return urlunsplit(
            (
                parts.scheme.casefold(),
                parts.netloc.casefold(),
                responsive_path,
                "",
                "",
            )
        )
    if host == "static01.nyt.com" and "/images/" in parts.path:
        directory, separator, filename = parts.path.rpartition("/")
        asset_name = directory.rsplit("/", 1)[-1]
        if (
            separator
            and asset_name
            and filename.casefold().startswith(asset_name.casefold())
        ):
            return f"nyt-image:{directory.casefold()}"
    wsj_image = (
        re.fullmatch(
            r"(/im-\d+)(?:/(?:social|portrait))?/?",
            parts.path,
            re.IGNORECASE,
        )
        if host in {"images.wsj.net", "opinion-images.wsj.net"}
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
    if host == "si.wsj.net":
        legacy_wsj_image = re.fullmatch(
            r"(.+?)_(?:G|D|M|SOC|TOP|IM)_(\d+)\.([a-z0-9]+)",
            parts.path,
            re.IGNORECASE,
        )
        if legacy_wsj_image is not None:
            return (
                "wsj-legacy-image:"
                f"{legacy_wsj_image.group(1).casefold()}_"
                f"{legacy_wsj_image.group(2)}."
                f"{legacy_wsj_image.group(3).casefold()}"
            )
    return url


def _nyt_generic_branding_image(url: str) -> bool:
    parts = urlsplit(url)
    if (parts.hostname or "").casefold() != "static01.nyt.com":
        return False
    return bool(
        re.search(
            r"/vi-assets/images/share/\d+x\d+_(?:nameplate|t)\.png$|"
            r"/images/icons/t_logo_\d+_black\.png$",
            parts.path,
            flags=re.IGNORECASE,
        )
    )


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


def _ft_explicit_truncation_notice(soup: BeautifulSoup) -> bool:
    text = _clean_text(soup.get_text(" ", strip=True))
    return (
        "您已阅读" in text
        and "剩余" in text
        and "订阅以继续探索完整内容" in text
    )


def _bloomberg_teaser_shell(soup: BeautifulSoup) -> bool:
    return bool(
        soup.select_one(
            "[class*='teaser-body'], "
            ".body-content[class*='teaser-content']"
        )
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
    if "ft.com" in (urlsplit(base_url).hostname or "").casefold():
        return _promote_ft_image_candidates(result)
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
            "/rcom-default.png",
            "/r-generic-hdr.png",
            "/javelin/images/social-",
            "/javelin/public/images/social-",
            "/lightsaber/_next/static/media/social-",
            "yahoo_default_logo",
            "yahoo-finance-default-logo",
            "/m/img/social/og-ft-logo",
            "/__assets/creatives/open-graph/ft-v1.jpg",
            "/img/meta/wsj-social-share.",
            "/img/wsj_logo_black_social.",
            "/img/wsj_profile_lg.",
            "/common/imgs/wsjsection.",
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
