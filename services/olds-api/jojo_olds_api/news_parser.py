from __future__ import annotations

import ast
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import html as html_module
import json
import re
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, Tag
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
    r"(?i)(?:^|\s)(photographer|photo|credit|illustration|graphic|source)s?\s*:"
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
    ".",
    "##",
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
        body = _bloomberg_partner_body(
            soup,
            canonical_url=canonical_url,
        )
    if body is None and generic_syndication_allowed:
        body = _postmedia_syndication_body(soup)
    if body is None and (
        generic_syndication_allowed
    ):
        body = _newsbreak_syndication_body(soup)
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
        legacy_video_body = _wsj_legacy_video_body(
            soup,
            canonical_url=canonical_url,
        )
        if legacy_video_body is not None:
            body = legacy_video_body
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
        document_card_body = _nyt_document_card_body(soup)
        if document_card_body is not None:
            body = document_card_body
        comics_body = _nyt_single_image_comics_body(soup)
        if comics_body is not None:
            body = comics_body
            structured_image_gallery_selected = True
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
        escaped_interactive = _nyt_escaped_legacy_interactive_body(soup)
        if escaped_interactive is not None:
            body = escaped_interactive
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
                    "p, h1, h2, h3, h4, li, table, figure, iframe, img[src]"
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
        structured_body = _structured_article_body(
            news_article,
            extract_ft_embedded_media=spec.publisher == "ft",
        )
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
    wsj_selected_sign_in = bool(
        spec.publisher == "wsj"
        and any(
            _clean_text(node.get_text(" ", strip=True))
            .casefold()
            .startswith("already a member? sign in")
            for node in clean_body.select("p")
        )
    )
    if spec.publisher == "ap":
        _remove_ap_body_promos(clean_body)
    if spec.publisher == "reuters":
        _trim_reuters_recirculation_tail(clean_body)
    if spec.publisher == "bloomberg":
        _trim_bloomberg_subscription_tail(clean_body)
        _remove_bloomberg_damaged_attribution(clean_body)
    if spec.publisher == "wsj":
        _trim_wsj_roadblock_tail(clean_body)
    if spec.publisher == "nyt":
        _trim_nyt_access_shell_tail(clean_body)
    _remove_noise(clean_body, spec)
    if spec.publisher == "wsj":
        inset_tables = _wsj_inset_table_body(soup)
        if inset_tables is not None:
            existing_text = _clean_text(
                clean_body.get_text(" ", strip=True)
            ).casefold()
            for child in list(inset_tables.children):
                if (
                    isinstance(child, Tag)
                    and child.name in {"h2", "h3"}
                    and _clean_text(child.get_text(" ", strip=True))
                    .casefold()
                    in existing_text
                ):
                    continue
                clean_body.append(child)

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
        (
            _tag_text(
                soup.select_one(
                    "#quiz-container section.question h1, "
                    "#quiz-container section.question h2"
                )
            )
            if spec.publisher == "bloomberg"
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
        _wsj_legacy_headline(soup)
        if spec.publisher == "wsj"
        else None,
        _meta_content(soup, "property", "og:title"),
        _meta_content(soup, "name", "twitter:title"),
        _tag_text(soup.select_one("article h1, main h1, h1")),
        (
            _tag_text(soup.select_one("#article-summary"))
            if spec.publisher == "nyt"
            else None
        ),
    )
    if spec.publisher == "ft" and headline:
        headline = re.sub(
            r"(?i)\s*[-–—]\s*FT\.com\s*$",
            "",
            headline,
        ).strip()
    description = _first_text(
        _string_or_none(nyt_preloaded_metadata.get("description")),
        _string_or_none(news_article.get("description")) if news_article else None,
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "property", "og:description"),
    )
    if (
        spec.publisher == "bloomberg"
        and description
        and (
            re.match(
                r"(?i)^sign up to receive (?:the )?.+ newsletter\b",
                description,
            )
            or re.match(
                r"(?i)^want to receive this post in your inbox\b.*"
                r"\bsign up for\b.*\bnewsletter\b",
                description,
            )
            or re.match(
                r"(?i)^for even more:\s*subscribe to bloomberg all access\b",
                description,
            )
        )
    ):
        description = None
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
            (
                _bloomberg_legacy_published_at(soup)
                if spec.publisher == "bloomberg"
                else None
            ),
            _nyt_visible_published_at(soup),
            _ft_legacy_published_at(soup) if spec.publisher == "ft" else None,
            (
                _wsj_legacy_published_at(soup)
                if spec.publisher == "wsj"
                else None
            ),
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
    if any(
        value.get("@type") == "LiveBlogPosting"
        for value in _json_ld_objects(soup)
    ):
        content_type = ContentType.LIVEBLOG
    ft_missing_legacy_visual = bool(
        spec.publisher == "ft"
        and _ft_missing_legacy_visual(soup)
    )
    if (
        spec.publisher == "bloomberg"
        and _bloomberg_article_narration(soup)
    ):
        content_type = ContentType.ARTICLE
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
    if spec.publisher == "wsj" and _wsj_is_legacy_video(soup):
        content_type = ContentType.VIDEO
    if spec.publisher == "wsj":
        wsj_page_content_type = _clean_text(
            _meta_content(soup, "name", "page.content.type") or ""
        ).casefold()
        if wsj_page_content_type in {
            "gallery",
            "photo gallery",
            "photo-gallery",
            "slideshow",
        } or soup.select_one(".slideshow-article"):
            content_type = ContentType.GALLERY
    if (
        spec.publisher == "ap"
        and _is_ap_data_bulletin(news_article, canonical_url)
    ):
        content_type = ContentType.INTERACTIVE
    if spec.publisher == "ft" and ft_crossword_selected:
        content_type = ContentType.INTERACTIVE
    if ft_missing_legacy_visual:
        content_type = ContentType.GALLERY
    if (
        spec.publisher == "ft"
        and soup.select_one(
            ".flashcomponent a.flashlink[href*='.swf' i]"
        )
    ):
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
    wsj_standalone_truncation_marker = False
    bloomberg_lightbox_thumbnails = (
        _bloomberg_legacy_lightbox_thumbnail_identities(
            soup,
            base_url=canonical_url,
        )
        if spec.publisher == "bloomberg"
        else set()
    )
    for url in _lead_image_urls(soup, news_article, canonical_url):
        if (
            spec.publisher == "bloomberg"
            and (
                _bloomberg_author_avatar_url(url)
                or _image_identity(url) in bloomberg_lightbox_thumbnails
            )
        ):
            continue
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
            if (
                spec.publisher == "bloomberg"
                and _bloomberg_author_avatar_url(image.original_url)
            ):
                continue
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
        blocks = _deduplicate_blocks(
            blocks,
            deduplicate_contained_pull_quotes=spec.publisher == "ft",
        )
        if spec.publisher == "wsj":
            trailing_text = (
                _clean_text(blocks[-1].text or "") if blocks else ""
            )
            wsj_standalone_truncation_marker = bool(
                blocks
                and blocks[-1].type == BlockType.PARAGRAPH
                and len(trailing_text) <= 80
                and (
                    trailing_text == "…"
                    or re.search(r"\.{3,}$", trailing_text)
                )
            )
            if wsj_standalone_truncation_marker:
                blocks.pop()

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
    minimum_body_characters = (
        _MINIMUM_BODY_CHARACTERS
        if (
            spec.publisher == "wsj"
            and _wsj_is_editorial_letter(soup)
        )
        else 500
        if (
            spec.publisher == "wsj"
            and content_type == ContentType.ARTICLE
        )
        else _MINIMUM_BODY_CHARACTERS
    )
    if (
        len(plain_text) < minimum_body_characters
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
        spec.publisher == "bloomberg"
        and _bloomberg_parcel_industry_teaser(soup)
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and _bloomberg_pv_magazine_teaser(soup)
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and _bloomberg_partner_full_story_teaser(soup)
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and _bloomberg_john_lothian_summary(soup)
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and _bloomberg_short_source_link_excerpt(
            soup,
            plain_text=plain_text,
        )
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and len(plain_text) < 500
        and soup.select_one("article.artData.paywall") is not None
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and soup.select_one(".ai-block") is not None
        and "signalpro" in _clean_text(
            soup.get_text(" ", strip=True)
        ).casefold()
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and "linkedin.com/" in _clean_text(
            _first_text(
                _meta_content(soup, "property", "og:url"),
                _tag_attribute(
                    soup.select_one("link[rel='canonical']"),
                    "href",
                ),
            )
            or ""
        ).casefold()
        and any(
            marker in _clean_text(soup.get_text(" ", strip=True)).casefold()
            for marker in (
                "cut through the ai noise",
                "full article below with no paywall",
                "read my latest, for free",
                "humbled to see our journey featured in bloomberg",
                "excited to be quoted in bloomberg news",
                "had the pleasure of joining bloomberg podcasts",
                "always-superb editing by",
            )
        )
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and "the practical value is the source trail" in _clean_text(
            soup.get_text(" ", strip=True)
        ).casefold()
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and "as international investment experts report" in _clean_text(
            soup.get_text(" ", strip=True)
        ).casefold()
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and "abitech analysis" in _clean_text(
            soup.get_text(" ", strip=True)
        ).casefold()
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "bloomberg"
        and "biggo finance appears first in google search" in _clean_text(
            soup.get_text(" ", strip=True)
        ).casefold()
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "wsj"
        and content_type == ContentType.ARTICLE
        and (
            wsj_standalone_truncation_marker
            or _wsj_legacy_ellipsis_truncation(plain_text)
        )
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "wsj"
        and _wsj_subscription_truncation(
            soup,
            content_type=content_type,
            plain_text=plain_text,
            selected_sign_in=wsj_selected_sign_in,
        )
    ):
        warnings.append("truncated-body")
    if (
        spec.publisher == "wsj"
        and content_type == ContentType.GALLERY
        and soup.select_one(".slideshow-article")
        and sum(image.should_archive for image in images) < 3
    ):
        warnings.append("incomplete-gallery")
    if (
        ft_missing_legacy_visual
        and not any(image.should_archive for image in images)
        and not any(
            block.type in {BlockType.IMAGE, BlockType.EMBED}
            for block in blocks
        )
    ):
        warnings.append("incomplete-gallery")
    if (
        spec.publisher == "nyt"
        and _nyt_unhydrated_interactive_shell(
            soup,
            content_type=content_type,
            plain_text=plain_text,
            blocks=blocks,
            images=images,
        )
    ):
        warnings.append("incomplete-interactive")
    if not published_at:
        warnings.append("missing-published-at")
    if body is None:
        warnings.append("article-body-not-found")

    warnings = list(dict.fromkeys(warnings))
    status = ArticleStatus.COMPLETE
    if "article-body-not-found" in warnings:
        status = ArticleStatus.UNSUPPORTED
    elif (
        "body-too-short" in warnings
        or "missing-headline" in warnings
        or "truncated-body" in warnings
        or "incomplete-gallery" in warnings
        or "incomplete-interactive" in warnings
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
    skipping_bloomberg_most_read = False
    for paragraph in paragraphs:
        paragraph_text = _clean_text(paragraph.get_text(" ", strip=True))
        if paragraph_text.casefold() in {
            "most read from bloomberg",
            "most read from bloomberg businessweek",
        }:
            skipping_bloomberg_most_read = True
            continue
        if skipping_bloomberg_most_read:
            if paragraph.find_parent(("ul", "ol")) is not None:
                continue
            skipping_bloomberg_most_read = False
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
            paragraph_text,
            re.IGNORECASE,
        ):
            break
    return wrapper if wrapper.select_one("p") is not None else None


def _generic_syndication_body(soup: BeautifulSoup) -> Tag | None:
    partner_url = _first_text(
        _meta_content(soup, "property", "og:url"),
        _tag_attribute(soup.select_one("link[rel='canonical']"), "href"),
    )
    partner_hostname = (
        (urlsplit(partner_url).hostname or "").casefold()
        if partner_url
        else ""
    )
    if (
        partner_hostname == "mql5.com"
        or partner_hostname.endswith(".mql5.com")
    ):
        # MQL5 wraps navigation, recommendations, and both sidebars in the
        # outer article element. Only this nested content node is the
        # syndicated report.
        node = soup.select_one(
            ".postContent.view > .container > .content"
        )
        if isinstance(node, Tag):
            document = BeautifulSoup(str(node), "html.parser")
            copy = document.select_one(".content")
            if isinstance(copy, Tag) and len(
                _clean_text(copy.get_text(" ", strip=True))
            ) >= _MINIMUM_SYNDICATED_BODY_CHARACTERS:
                for link in list(copy.select("a[href*='/signals/']")):
                    link.decompose()
                # MQL5 stores each prose paragraph as a direct child ``div``.
                # Normalize those nodes so the common block extractor keeps
                # their text and any inline article image.
                for child in copy.find_all("div", recursive=False):
                    child.name = "p"
                return copy
    selectors = (
        "[itemprop='articleBody']",
        ".news__body__center__article",
        ".article-text",
        ".post-content",
        ".article-content",
        ".entry-content",
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
                "[class*='newsletter' i], [class*='advert' i], "
                "[class*='subscription' i], [class*='get-app' i], "
                "[class*='whatsapp-group' i], "
                "[class*='content-loader' i], .lazy-widgets, "
                ".watchOrListen-bottom-section-v3, .liveEventMain_widget, "
                ".primeSWrapper, .ts-dots, .bottomTopics, "
                ".topicListContainer, .topicListTitle, .tags, "
                "[id^='views-bootstrap-article-node-view-block-'], "
                "[data-animation-role='button'], "
                "[data-content-field='tags']"
            ):
                noise.decompose()
            for control in list(copy.select("[role='button']")):
                if control.name == "a" and control.select_one("img") is not None:
                    control.unwrap()
                else:
                    control.decompose()
            _remove_generic_syndication_partner_noise(
                copy,
                source_document=soup,
            )
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


def _remove_generic_syndication_partner_noise(
    body: Tag,
    *,
    source_document: BeautifulSoup,
) -> None:
    """Remove partner-site recirculation without trimming licensed copy."""
    partner_url = _first_text(
        _meta_content(source_document, "property", "og:url"),
        _tag_attribute(
            source_document.select_one("link[rel='canonical']"),
            "href",
        ),
    )
    hostname = (
        (urlsplit(partner_url).hostname or "").casefold()
        if partner_url
        else ""
    )
    if hostname == "mediapart.fr" or hostname.endswith(".mediapart.fr"):
        for node in list(body.select("p")):
            text = _clean_text(node.get_text(" ", strip=True))
            if re.match(
                r"(?i)^read more of this bloomberg report "
                r"published by the\b",
                text,
            ):
                node.decompose()
    if (
        hostname == "eco-business.com"
        or hostname.endswith(".eco-business.com")
    ):
        # Eco-Business places its membership CTA inside the article section,
        # immediately after licensed Bloomberg copy. Its class names only
        # refer to the site's "circle" program, not an advertisement.
        for node in list(body.select(".eb-article__eb-circle-banner")):
            node.decompose()
    if (
        hostname == "insurancejournal.com"
        or hostname.endswith(".insurancejournal.com")
    ):
        # Insurance Journal nests article tags and an in-content subscription
        # card inside the same entry-content element as licensed copy.
        for node in list(
            body.select(
                "p.tagtag, .subscribe-banner, "
                "[class*='subscribe-banner' i]"
            )
        ):
            node.decompose()
    if hostname == "linkedin.com" or hostname.endswith(".linkedin.com"):
        for node in list(body.select("section.comment, .comment__body")):
            node.decompose()
        for node in list(body.select("p, li")):
            text = _clean_text(node.get_text(" ", strip=True))
            if (
                "full article below" in text.casefold()
                and "read more from bloomberg news" in text.casefold()
            ):
                node.decompose()
                continue
            if re.fullmatch(r"[\d,.]+\s+followers?", text, re.IGNORECASE):
                node.decompose()
                continue
            if text.casefold() == "report this post":
                node.decompose()
                continue
            hashtag = re.search(r"\s+#[\w-]+", text)
            if hashtag is None or len(re.findall(r"#[\w-]+", text)) < 5:
                continue
            cleaned = text[:hashtag.start()].rstrip()
            if cleaned.startswith('"') and '" "' in cleaned:
                cleaned = cleaned.split('" "', 1)[0]
            cleaned = cleaned.strip().strip('"').strip()
            if cleaned:
                node.clear()
                node.string = cleaned
            else:
                node.decompose()
    if hostname == "benzinga.com" or hostname.endswith(".benzinga.com"):
        marker = next(
            (
                node
                for node in body.select("p, h2, h3, h4")
                if re.match(
                    r"(?i)^(?:see also|read next)\s*:",
                    _clean_text(node.get_text(" ", strip=True)),
                )
            ),
            None,
        )
        if isinstance(marker, Tag):
            tail = marker
            while isinstance(tail.parent, Tag):
                for sibling in list(tail.next_siblings):
                    if isinstance(sibling, Tag):
                        sibling.decompose()
                    else:
                        sibling.extract()
                if tail.parent is body:
                    break
                tail = tail.parent
            marker.decompose()
    if hostname == "newsbreak.com" or hostname.endswith(".newsbreak.com"):
        for card in list(body.select("section")):
            link = card.select_one("a[href][target='_blank']")
            if (
                isinstance(link, Tag)
                and card.select_one("p.textoverflow-3") is not None
            ):
                card.decompose()
    if hostname == "ctrmcenter.com" or hostname.endswith(".ctrmcenter.com"):
        # CTRM Center appends its own republication disclaimer inside the
        # article element, after the licensed Bloomberg copy. It is partner
        # chrome rather than reporting and therefore survives generic footer
        # selectors unless removed explicitly.
        for node in list(body.select(".cat_postinfo, .postinfo, span.bio")):
            text = _clean_text(node.get_text(" ", strip=True)).casefold()
            if (
                "republished on the ctrm center" in text
                and "if you have any issue with this post" in text
            ):
                node.decompose()
    if hostname == "biasly.com" or hostname.endswith(".biasly.com"):
        body.clear()
        return
    if (
        hostname == "bnnbloomberg.ca"
        or hostname.endswith(".bnnbloomberg.ca")
    ):
        for node in list(body.select("p, li, ul, ol")):
            text = _clean_text(node.get_text(" ", strip=True)).casefold()
            if text == "latest updates on company news here":
                node.decompose()
    if (
        hostname == "marketscreener.com"
        or hostname.endswith(".marketscreener.com")
        or hostname == "zonebourse.com"
        or hostname.endswith(".zonebourse.com")
    ):
        for node in list(body.select("p")):
            text = _clean_text(node.get_text(" ", strip=True))
            if not re.fullmatch(r"[.,;:!?]+", text):
                continue
            previous = node.find_previous_sibling("p")
            if not isinstance(previous, Tag):
                node.decompose()
                continue
            previous_text = _clean_text(
                previous.get_text(" ", strip=True)
            ).rstrip()
            previous.clear()
            previous.append(f"{previous_text}{text}")
            node.decompose()


def _postmedia_syndication_body(soup: BeautifulSoup) -> Tag | None:
    """Join Postmedia's paragraph-per-section body without page widgets."""
    paragraphs = soup.select(
        "article.story-v2-article-content-story "
        ".story-v2-content-element-inline > p"
    )
    substantive = [
        paragraph
        for paragraph in paragraphs
        if _clean_text(paragraph.get_text(" ", strip=True))
    ]
    if len(substantive) < 2 or sum(
        len(_clean_text(paragraph.get_text(" ", strip=True)))
        for paragraph in substantive
    ) < _MINIMUM_SYNDICATED_BODY_CHARACTERS:
        return None
    document = BeautifulSoup(
        "<div data-jojo-source='postmedia-syndication'></div>",
        "html.parser",
    )
    wrapper = document.select_one("div")
    if not isinstance(wrapper, Tag):
        return None
    for paragraph in substantive:
        copy = BeautifulSoup(str(paragraph), "html.parser").select_one("p")
        if isinstance(copy, Tag):
            wrapper.append(copy)
    return wrapper


def _newsbreak_syndication_body(soup: BeautifulSoup) -> Tag | None:
    """Recover the licensed article payload without NewsBreak feed cards."""
    partner_url = _first_text(
        _meta_content(soup, "property", "og:url"),
        _tag_attribute(soup.select_one("link[rel='canonical']"), "href"),
    )
    if not partner_url or "newsbreak.com/" not in partner_url.casefold():
        return None
    script = soup.select_one("script#__NEXT_DATA__")
    if not isinstance(script, Tag):
        return None
    try:
        payload = json.loads(script.string or script.get_text())
    except (json.JSONDecodeError, TypeError):
        return None
    page = payload.get("props", {}).get("pageProps", {})
    content = page.get("content")
    authors = page.get("authors", [])
    if (
        not isinstance(content, str)
        or "bloomberg" not in " ".join(map(str, authors)).casefold()
    ):
        return None
    document = BeautifulSoup(content, "html.parser")
    body = document.body or document.find()
    if not isinstance(body, Tag):
        return None
    if len(_clean_text(body.get_text(" ", strip=True))) < 300:
        return None
    return body


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


def _bloomberg_partner_body(
    soup: BeautifulSoup,
    *,
    canonical_url: str,
) -> Tag | None:
    partner_url = _first_text(
        _meta_content(soup, "property", "og:url"),
        _tag_attribute(soup.select_one("link[rel='canonical']"), "href"),
    )
    partner_host = (
        (urlsplit(partner_url).hostname or "").casefold()
        if partner_url
        else ""
    )
    if (
        partner_host == "johnlothiannews.com"
        or partner_host.endswith(".johnlothiannews.com")
    ):
        slug = urlsplit(canonical_url).path.rstrip("/").rsplit("/", 1)[-1]
        target_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", slug.casefold())
            if not token.isdigit()
        }
        best: tuple[float, Tag] | None = None
        for paragraph in soup.select(".entry-content > p"):
            title = paragraph.select_one(":scope > strong")
            if not isinstance(title, Tag):
                continue
            title_tokens = set(
                re.findall(
                    r"[a-z0-9]+",
                    _clean_text(title.get_text(" ", strip=True)).casefold(),
                )
            )
            if not target_tokens or not title_tokens:
                continue
            score = len(target_tokens & title_tokens) / len(
                target_tokens | title_tokens
            )
            if best is None or score > best[0]:
                best = (score, paragraph)
        if best is not None and best[0] >= 0.75:
            paragraph = best[1]
            title = paragraph.select_one(":scope > strong")
            if isinstance(title, Tag):
                title.decompose()
            for line_break in list(paragraph.select("br")):
                line_break.replace_with("\n")
            lines = [
                _clean_text(line)
                for line in paragraph.get_text("\n", strip=True).splitlines()
                if _clean_text(line)
            ]
            reporting = [
                line
                for line in lines
                if line.casefold() != "bloomberg"
                and not re.fullmatch(r"https?://\S+", line)
            ]
            if reporting:
                document = BeautifulSoup(
                    "<article><p></p></article>",
                    "html.parser",
                )
                body = document.select_one("article")
                output = document.select_one("p")
                if isinstance(body, Tag) and isinstance(output, Tag):
                    output.string = " ".join(reporting)
                    return body
    if (
        partner_host == "mediapart.fr"
        or partner_host.endswith(".mediapart.fr")
    ):
        source_body = soup.select_one(".news__body__center__article")
        if isinstance(source_body, Tag):
            document = BeautifulSoup(str(source_body), "html.parser")
            mediapart_body = document.select_one(
                ".news__body__center__article"
            )
            if isinstance(mediapart_body, Tag):
                for duplicate_visual_text in list(
                    mediapart_body.select(
                        ".dropcap-wrapper > [aria-hidden='true']"
                    )
                ):
                    duplicate_visual_text.decompose()
                for node in list(mediapart_body.select("p")):
                    text = _clean_text(node.get_text(" ", strip=True))
                    if re.match(
                        r"(?i)^read more of this bloomberg report "
                        r"published by the\b",
                        text,
                    ):
                        node.decompose()
                if len(mediapart_body.select("p")) >= 2:
                    return mediapart_body
    if (
        partner_host == "parcelindustry.com"
        or partner_host.endswith(".parcelindustry.com")
    ):
        parcel_body = soup.select_one(
            "article.article .fulltext-txt, article.article #contentText"
        )
        if isinstance(parcel_body, Tag):
            teaser = re.sub(
                r"\s+Read more\s*!?\s*$",
                "",
                _clean_text(parcel_body.get_text(" ", strip=True)),
                flags=re.IGNORECASE,
            )
            document = BeautifulSoup("<article><p></p></article>", "html.parser")
            paragraph = document.select_one("p")
            article = document.select_one("article")
            if isinstance(paragraph, Tag) and isinstance(article, Tag):
                paragraph.string = teaser
                return article

    if (
        partner_host == "pv-magazine.com"
        or partner_host.endswith(".pv-magazine.com")
    ):
        pv_magazine_body = soup.select_one(".pvmagazine-post-content")
        if isinstance(pv_magazine_body, Tag):
            return pv_magazine_body
    if (
        partner_host == "eco-business.com"
        or partner_host.endswith(".eco-business.com")
    ):
        source_body = soup.select_one(
            ".eb-article__body-content"
        )
        if isinstance(source_body, Tag):
            document = BeautifulSoup(str(source_body), "html.parser")
            eco_business_body = document.select_one(
                ".eb-article__body-content"
            )
            if isinstance(eco_business_body, Tag):
                for node in list(
                    eco_business_body.select(
                        ".eb-article__eb-circle-banner"
                    )
                ):
                    node.decompose()
                return eco_business_body

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


def _bloomberg_parcel_industry_teaser(soup: BeautifulSoup) -> bool:
    partner_url = _first_text(
        _meta_content(soup, "property", "og:url"),
        _tag_attribute(soup.select_one("link[rel='canonical']"), "href"),
    )
    hostname = (
        (urlsplit(partner_url).hostname or "").casefold()
        if partner_url
        else ""
    )
    if not (
        hostname == "parcelindustry.com"
        or hostname.endswith(".parcelindustry.com")
    ):
        return False
    return any(
        _tag_text(anchor).casefold() == "read more"
        for anchor in soup.select(
            "article.article .fulltext-txt a, article.article #contentText a"
        )
    )


def _bloomberg_pv_magazine_teaser(soup: BeautifulSoup) -> bool:
    partner_url = _first_text(
        _meta_content(soup, "property", "og:url"),
        _tag_attribute(soup.select_one("link[rel='canonical']"), "href"),
    )
    hostname = (
        (urlsplit(partner_url).hostname or "").casefold()
        if partner_url
        else ""
    )
    if not (
        hostname == "pv-magazine.com"
        or hostname.endswith(".pv-magazine.com")
    ):
        return False
    body = soup.select_one(".pvmagazine-post-content")
    if not isinstance(body, Tag):
        return False
    text = _clean_text(body.get_text(" ", strip=True))
    return bool(
        re.search(
            r"\bclick\s+here\s+to\s+read\s+the\s+(?:rest|full\s+story)\b",
            text,
            re.IGNORECASE,
        )
    )


def _bloomberg_partner_full_story_teaser(soup: BeautifulSoup) -> bool:
    """Recognize partner copies that explicitly link to Bloomberg for the rest."""
    partner_url = _first_text(
        _meta_content(soup, "property", "og:url"),
        _tag_attribute(soup.select_one("link[rel='canonical']"), "href"),
    )
    hostname = (
        (urlsplit(partner_url).hostname or "").casefold()
        if partner_url
        else ""
    )
    if hostname == "mediapart.fr" or hostname.endswith(".mediapart.fr"):
        if any(
            re.match(
                r"(?i)^read more of this bloomberg report "
                r"published by the\b",
                _clean_text(node.get_text(" ", strip=True)),
            )
            for node in soup.select(
                ".news__body__center__article p, "
                "[itemprop='articleBody'] p"
            )
        ):
            return True
    for node in soup.select("p, div"):
        text = _clean_text(node.get_text(" ", strip=True))
        explicit_full_story = re.match(
            r"(?i)^(?:"
            r"click\s+here\s+to\s+read\s+the\s+full\s+story|"
            r"read\s+(?:the\s+)?full\s+article\s+here\s+"
            r"(?:via|at|on)\s+bloomberg"
            r")\b",
            text,
        )
        excerpt_read_more = re.search(
            r"(?i)\bread\s+more\s+at\s+bloomberg\s*\.?\s*$",
            text,
        )
        if not explicit_full_story and not excerpt_read_more:
            continue
        if any(
            "bloomberg.com/" in str(anchor.get("href") or "").casefold()
            for anchor in node.select("a[href]")
        ):
            return True
    return False


def _bloomberg_short_source_link_excerpt(
    soup: BeautifulSoup,
    *,
    plain_text: str,
) -> bool:
    """Recognize short partner summaries that only point to Bloomberg."""
    if len(plain_text) >= 1_000:
        return False
    partner_url = _first_text(
        _meta_content(soup, "property", "og:url"),
        _tag_attribute(soup.select_one("link[rel='canonical']"), "href"),
    )
    hostname = (
        (urlsplit(partner_url).hostname or "").casefold()
        if partner_url
        else ""
    )
    if hostname == "bloomberg.com" or hostname.endswith(".bloomberg.com"):
        return False
    if any(
        re.search(
            r"(?i)bloomberg\.com/(?:news/)?(?:articles/)?\d{4}-\d{2}-\d{2}/",
            str(anchor.get("href") or ""),
        )
        for anchor in soup.select("a[href]")
    ):
        return True
    return bool(
        re.search(
            r"(?im)^https?://(?:www\.)?bloomberg\.com/"
            r"(?:news/)?(?:articles/)?\d{4}-\d{2}-\d{2}/\S+\s*$",
            plain_text,
        )
    )


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
    """Recover stories and editorial indexes from legacy feature templates."""
    story = soup.select_one(
        ".dvz-page-wrapper.dvz-feature .feature-wrapper"
    )
    if isinstance(story, Tag):
        paragraphs = [
            _clean_text(paragraph.get_text(" ", strip=True))
            for paragraph in story.select("p")
        ]
        substantive = [text for text in paragraphs if text]
        if (
            len(substantive) >= 3
            and sum(len(text) for text in substantive) >= 700
        ):
            return story

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
        elif block_type == "tabularData":
            table = parsed.new_tag("table")
            definitions: list[dict[str, Any]] = []
            rows: list[dict[str, Any]] = []
            for child in block.get("content", []):
                if not isinstance(child, dict):
                    continue
                if child.get("type") == "columns":
                    data = child.get("data")
                    if isinstance(data, dict) and isinstance(
                        data.get("definitions"),
                        list,
                    ):
                        definitions = [
                            value
                            for value in data["definitions"]
                            if isinstance(value, dict)
                        ]
                elif child.get("type") == "row":
                    rows.append(child)
            if definitions:
                thead = parsed.new_tag("thead")
                heading_row = parsed.new_tag("tr")
                for definition in definitions:
                    cell = parsed.new_tag("th")
                    cell.string = (
                        _string_or_none(definition.get("title")) or ""
                    )
                    heading_row.append(cell)
                thead.append(heading_row)
                table.append(thead)
            if rows:
                tbody = parsed.new_tag("tbody")
                for row in rows:
                    row_tag = parsed.new_tag("tr")
                    for source_cell in row.get("content", []):
                        if not isinstance(source_cell, dict):
                            continue
                        cell = parsed.new_tag("td")
                        cell.string = _clean_text(text_content(source_cell))
                        row_tag.append(cell)
                    if row_tag.select_one("td") is not None:
                        tbody.append(row_tag)
                if tbody.select_one("tr") is not None:
                    table.append(tbody)
            if table.select_one("tr") is not None:
                wrapper.append(table)

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
    if wrapper.select_one(
        "p, h2, h3, h4, h5, h6, blockquote, ul, ol, table, iframe"
    ):
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
    *,
    extract_ft_embedded_media: bool = False,
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
        if extract_ft_embedded_media:
            media_nodes, paragraph = _ft_structured_media_nodes(
                document,
                raw_paragraph,
            )
            for media_node in media_nodes:
                article.append(media_node)
            if not paragraph:
                continue
            node = document.new_tag("p")
            node.string = paragraph
            article.append(node)
            continue
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


_FT_STRUCTURED_IMAGE_RE = re.compile(
    r"\[(?P<url>https?://[^\]\s]+)\]",
    flags=re.IGNORECASE,
)
_FT_STRUCTURED_CREDIT_PREFIXES = tuple(
    sorted(
        {
            "China National Space Administration/Getty Images",
            "Charlie Bibby/Financial Times",
            "Andrew Milligan/Getty Images",
            "Felix Bensman/Dreamstime",
            "Jeff Kravitz/FilmMagic",
            "JMEnternational/Redferns",
            "AFP via Getty Images",
            "Radharc Images/Alamy",
            "Katrina Campbell",
            "Catherine Ashmore",
            "Yoshiyuki Tamai",
            "Julia Savchenko",
            "Collier Schorr",
            "London Play",
            "Mark Allan",
            "FT montage",
            "Bloomberg",
            "Reuters",
            "Getty Images",
            "Getty",
            "Dreamstime",
            "EPA",
            "AFP",
            "AP",
        },
        key=len,
        reverse=True,
    )
)
_FT_STRUCTURED_BODY_BOUNDARY_RE = re.compile(
    r"(?<=[a-z)])(?=(?:"
    r"The|A(?=\s)|An(?=\s)|As(?=\s)|At(?=\s)|After(?=\s)|"
    r"Australia|Britain|Credit|Despite|In(?=\s)|It(?=\s)|"
    r"Late(?=\s)|Mark(?=\s)|Muslim|Nasa|On(?=\s)|President|"
    r"Scams|Still(?=\s)|That(?=\s)|This(?=\s)|Through(?=\s)|"
    r"UK(?=\s)|Venezuelan|When(?=\s)|What(?=\s)|"
    r"[\"“]))"
)


def _ft_structured_credit_and_body(
    value: str,
    *,
    credit_hint: str | None = None,
) -> tuple[str | None, str]:
    """Split FT's flattened ``© creditBody`` representation."""
    clean = value.strip()
    folded = clean.casefold()
    for credit in _FT_STRUCTURED_CREDIT_PREFIXES:
        if folded.startswith(credit.casefold()):
            return clean[: len(credit)], clean[len(credit) :].strip()
    if credit_hint:
        normalized_hint = re.sub(
            r"[^a-z0-9]+",
            "",
            credit_hint.casefold(),
        )
        candidates: list[tuple[float, int]] = []
        for match in re.finditer(
            r"(?<=[a-z0-9)])(?=[A-Z])|(?<=\S)\s+(?=[A-Z“])",
            clean[:240],
        ):
            boundary = match.start()
            body = clean[match.end() :].strip()
            if len(body) < 50:
                continue
            normalized_credit = re.sub(
                r"[^a-z0-9]+",
                "",
                clean[:boundary].casefold(),
            )
            similarity = SequenceMatcher(
                None,
                normalized_credit,
                normalized_hint,
            ).ratio()
            candidates.append((similarity, boundary))
        if candidates:
            similarity, boundary = max(candidates)
            if similarity >= 0.72:
                return (
                    clean[:boundary].strip() or None,
                    clean[boundary:].strip(),
                )
    inferred_boundary = _ft_infer_structured_credit_boundary(clean)
    if inferred_boundary is not None:
        return (
            clean[:inferred_boundary].strip() or None,
            clean[inferred_boundary:].strip(),
        )
    boundary = _FT_STRUCTURED_BODY_BOUNDARY_RE.search(clean)
    if boundary is None:
        return clean or None, ""
    return (
        clean[: boundary.start()].strip() or None,
        clean[boundary.start() :].strip(),
    )


def _ft_infer_structured_credit_boundary(value: str) -> int | None:
    """Find prose glued to an unknown photo credit without a delimiter."""
    candidates: list[tuple[float, int]] = []
    agency_suffix = re.compile(
        r"(?i)(?:reuters|getty(?:\s+images)?|afp|ap|epa(?:-efe)?|"
        r"shutterstock|alamy|pa\s+wire|financial\s+times|"
        r"magnum\s+photos|eyevine|avalon\.red)$"
    )
    finite_verb = re.compile(
        r"(?i)\b(?:is|are|was|were|has|have|had|will|would|"
        r"can|could|may|might|must|agreed|filed|became|become|"
        r"comes|come|takes|took|began|starts|started|read|cannot)\b"
    )
    for match in re.finditer(
        r"(?<=[a-z0-9)])(?=[A-Z])|(?<=\S)\s+(?=[A-Z“])",
        value[:180],
    ):
        boundary = match.start()
        prefix = value[:boundary].strip()
        body = value[match.end() :].strip()
        if not 3 <= len(prefix) <= 130 or len(body) < 80:
            continue
        opening_words = " ".join(body.split()[:25])
        if finite_verb.search(opening_words) is None:
            continue
        joined = match.start() == match.end()
        score = 4.0 if joined else 0.0
        if agency_suffix.search(prefix):
            score += 8.0
        if "/" in prefix:
            score += 2.0
        if len(prefix.split()) <= 6:
            score += 1.0
        if all(
            re.match(r"^[A-Z][^\s]*$", word)
            for word in prefix.replace("/", " ").split()
        ):
            score += 2.0
        score += min(len(prefix), 80) / 80
        if re.search(r"[!?;]", prefix):
            score -= 6.0
        if re.search(r"[a-z]{3,}\.\s+[A-Z]", prefix):
            score -= 5.0
        if re.search(
            r"(?i)\b(?:is|was|were|has|have|had|to|in|"
            r"for|with|from)\b",
            prefix,
        ):
            score -= 3.0
        candidates.append((score, boundary))
    if not candidates:
        return None
    score, boundary = max(candidates)
    return boundary if score >= 2.5 else None


def _ft_structured_caption_and_body(
    value: str,
    *,
    credit_hint: str | None = None,
) -> tuple[str | None, str | None, str]:
    """Recover caption, credit and following prose from a flattened image."""
    clean = value.strip()
    if not clean:
        return None, None, ""
    if "©" in clean:
        caption, credit_tail = clean.rsplit("©", 1)
        credit, body = _ft_structured_credit_and_body(
            credit_tail,
            credit_hint=credit_hint,
        )
        return _clean_text(caption) or None, credit, _clean_text(body)
    boundary = _FT_STRUCTURED_BODY_BOUNDARY_RE.search(clean)
    if boundary is None:
        return None, None, _clean_text(clean)
    return (
        _clean_text(clean[: boundary.start()]) or None,
        None,
        _clean_text(clean[boundary.start() :]),
    )


def _ft_structured_media_nodes(
    document: BeautifulSoup,
    raw_paragraph: str,
) -> tuple[list[Tag], str]:
    """Turn image annotations flattened into FT JSON-LD back into figures."""
    matches = list(_FT_STRUCTURED_IMAGE_RE.finditer(raw_paragraph))
    if not matches:
        return [], _clean_text(raw_paragraph)
    figures: list[Tag] = []
    trailing_body = ""
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else None
        tail = raw_paragraph[match.end() : end]
        description_start = (
            matches[index - 1].end() if index > 0 else 0
        )
        description = raw_paragraph[description_start : match.start()]
        credit_match = re.search(
            r"\((?:photo(?:graph)?|image)\s+(?:by|:)\s*"
            r"(?P<credit>[^()]+)\)\s*$",
            description,
            flags=re.IGNORECASE,
        )
        credit_hint = (
            _clean_text(credit_match.group("credit"))
            if credit_match is not None
            else None
        )
        caption, credit, body = _ft_structured_caption_and_body(
            tail,
            credit_hint=credit_hint,
        )
        figure = document.new_tag("figure")
        image = document.new_tag("img")
        image["src"] = match.group("url")
        figure.append(image)
        if caption or credit:
            figcaption = document.new_tag("figcaption")
            figcaption.string = " ".join(
                value
                for value in (
                    caption,
                    f"Photo: {credit}" if credit else None,
                )
                if value
            )
            figure.append(figcaption)
        figures.append(figure)
        if index == len(matches) - 1:
            trailing_body = body
    return figures, trailing_body


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


def _nyt_escaped_legacy_interactive_body(
    soup: BeautifulSoup,
) -> Tag | None:
    """Recover rendered graphics emitted outside the legacy article shell."""
    listings = soup.select_one(
        "body > .control-width .listings, "
        "body > div.control-width .listings"
    )
    if isinstance(listings, Tag):
        paragraphs = [
            text
            for paragraph in listings.select("p")
            if (text := _tag_text(paragraph))
        ]
        if (
            len(paragraphs) >= 5
            and sum(len(text) for text in paragraphs) >= 500
        ):
            document = BeautifulSoup("<article></article>", "html.parser")
            article = document.article
            if not isinstance(article, Tag):
                return None
            for entry in listings.select("li"):
                for source_node in entry.select(":scope > h4, :scope > p"):
                    copy = BeautifulSoup(
                        str(source_node),
                        "html.parser",
                    ).find(source_node.name)
                    if isinstance(copy, Tag):
                        article.append(copy)
                for source_image in entry.select("img[src]"):
                    figure = document.new_tag("figure")
                    image = document.new_tag("img")
                    image["src"] = str(source_image["src"])
                    alt = _tag_attribute(source_image, "alt")
                    if alt:
                        image["alt"] = alt
                    figure.append(image)
                    caption = _tag_text(
                        source_image.find_parent(class_="img")
                        .select_one(".img-caption")
                        if isinstance(
                            source_image.find_parent(class_="img"),
                            Tag,
                        )
                        else None
                    )
                    credit = _tag_text(entry.select_one(".img-credit"))
                    if caption or credit:
                        figcaption = document.new_tag("figcaption")
                        figcaption.string = " ".join(
                            value for value in (caption, credit) if value
                        )
                        figure.append(figcaption)
                    article.append(figure)
            return article

    contribution_form = soup.select_one("#g-graphic.g-form")
    if isinstance(contribution_form, Tag):
        text = _clean_text(contribution_form.get_text(" ", strip=True))
        if (
            len(text) >= 300
            and contribution_form.select_one("form, textarea, input")
        ):
            return contribution_form
    return None


def _wsj_is_legacy_video(soup: BeautifulSoup) -> bool:
    if soup.select_one(
        "#masterVideoCenter, .vcrPlayerArea, .js_videoPlayer #videoPlayer"
    ):
        return True
    return any(
        re.search(r"""articleType\s*:\s*["']Video\s*-\s*WSJ["']""", value)
        for script in soup.select("script")
        if (value := script.string or script.get_text())
    )


def _bloomberg_john_lothian_summary(soup: BeautifulSoup) -> bool:
    """John Lothian newsletters carry short summaries, never full stories."""
    partner_url = _first_text(
        _meta_content(soup, "property", "og:url"),
        _tag_attribute(soup.select_one("link[rel='canonical']"), "href"),
    )
    hostname = (
        (urlsplit(partner_url).hostname or "").casefold()
        if partner_url
        else ""
    )
    return (
        hostname == "johnlothiannews.com"
        or hostname.endswith(".johnlothiannews.com")
    )


def _wsj_legacy_video_body(
    soup: BeautifulSoup,
    *,
    canonical_url: str,
) -> Tag | None:
    """Recover descriptions and transcripts from the old WSJ Video Center."""
    if not _wsj_is_legacy_video(soup):
        return None
    description = _first_text(
        _tag_text(
            soup.select_one(
                "#videoPlayerDescription [itemprop='description'], "
                "#currentVideoInfo > p"
            )
        ),
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "property", "og:description"),
    )
    transcript = _tag_text(soup.select_one(".vcrTranscriptContent"))
    video_url = _first_text(
        _tag_attribute(soup.select_one("#videoTitle[href]"), "href"),
        canonical_url,
    )
    if not description and not transcript and not video_url:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    if description:
        paragraph = document.new_tag("p")
        paragraph.string = description
        article.append(paragraph)
    if transcript:
        heading = document.new_tag("h2")
        heading.string = "Transcript"
        article.append(heading)
        paragraph = document.new_tag("p")
        paragraph.string = transcript
        article.append(paragraph)
    normalized_video_url = _normalized_url(
        video_url,
        base_url=canonical_url,
    )
    if normalized_video_url:
        iframe = document.new_tag("iframe")
        iframe["src"] = normalized_video_url
        iframe["title"] = "WSJ video"
        article.append(iframe)
    return article


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
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    if len(sections) < 2:
        text = _clean_text(candidate.get_text(" ", strip=True))
        if (
            len(text) < _MINIMUM_BODY_CHARACTERS
            or candidate.select_one("img[src], iframe") is None
        ):
            return None
        paragraph = document.new_tag("p")
        paragraph.string = text
        article.append(paragraph)
        for media in candidate.select("img[src], iframe"):
            media_copy = BeautifulSoup(
                str(media),
                "html.parser",
            ).find(media.name)
            if isinstance(media_copy, Tag):
                article.append(media_copy)
        return article
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
    return len(text) < 1_000 and bool(re.search(r"\.{3,}$", text))


def _wsj_subscription_truncation(
    soup: BeautifulSoup,
    *,
    content_type: ContentType,
    plain_text: str,
    selected_sign_in: bool,
) -> bool:
    """Reject metered WSJ previews while retaining substantial recovered copy."""
    if content_type != ContentType.ARTICLE:
        return False
    if soup.select_one("[class*='ArticleRoadblock' i]") or any(
        _clean_text(node.get_text(" ", strip=True))
        .casefold()
        .startswith("to read the full story")
        for node in soup.select("p, h2, h3, h4")
    ):
        return True
    if len(plain_text) >= 1_000:
        return False
    declared_word_count = _wsj_declared_word_count(soup)
    extracted_word_count = len(
        re.findall(
            r"[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*",
            plain_text,
        )
    )
    if (
        declared_word_count is not None
        and extracted_word_count >= max(
            1,
            int(declared_word_count * 0.85),
        )
    ):
        return False
    if selected_sign_in:
        return True
    copyright_footer = any(
        (
            (text := _clean_text(node.get_text(" ", strip=True))).casefold()
            .startswith("copyright ©")
            and "dow jones & company" in text.casefold()
        )
        for node in soup.select("p")
    )
    if not copyright_footer:
        return False
    modern_body_paragraphs = [
        node
        for node in soup.select("p[data-type='paragraph']")
        if _clean_text(node.get_text(" ", strip=True))
    ]
    has_metered_controls = bool(
        soup.select_one(
            "[class*='ListenToArticle' i], "
            "[class*='MinutesLabel' i], "
            "h2[class*='SectionLabel' i]"
        )
    )
    return bool(has_metered_controls or len(modern_body_paragraphs) <= 3)


def _wsj_declared_word_count(soup: BeautifulSoup) -> int | None:
    """Read WSJ's own word count so genuine short reports are not previews."""
    raw = _first_text(
        _meta_content(soup, "name", "article:word_count"),
        _meta_content(soup, "property", "article:word_count"),
    )
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


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
        if _clean_text(heading.get_text(" ", strip=True)).casefold() in {
            "listen to article",
            "listen to this article",
        }:
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


def _bloomberg_author_avatar_url(url: str) -> bool:
    return bool(
        re.search(
            r"(?i)/images/bview/columnists/"
            r"(?:\d+x\d+/)?[^/?#]+\.(?:gif|jpe?g|png|webp)(?:[?#]|$)",
            url,
        )
    )


def _bloomberg_legacy_lightbox_thumbnail_identities(
    soup: BeautifulSoup,
    *,
    base_url: str,
) -> set[str]:
    identities: set[str] = set()
    for thumbnail in soup.select(
        ".thumbnail_container.overlay_container > a.enlarge_image"
    ):
        overlay = thumbnail.find_next_sibling(
            "div",
            class_="simple_overlay",
        )
        image = thumbnail.find("img")
        if (
            not isinstance(overlay, Tag)
            or overlay.find("img") is None
            or not isinstance(image, Tag)
        ):
            continue
        identities.update(
            _image_identity(url)
            for url in _image_urls(image, base_url=base_url)
        )
    return identities


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
        if (
            (parts.hostname or "").casefold() == "img.ksl.com"
            and re.search(
                r"(?:^|&)filter=ksl/(?:\d+x\d+|100x100)(?:&|$)",
                parts.query,
                re.IGNORECASE,
            )
        ):
            full_size = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, "", "")
            )
            if full_size not in promoted:
                promoted.append(full_size)
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
    if source.select_one("#div_with_disclaimer_id"):
        document = BeautifulSoup(str(source), "html.parser")
        cleaned_source = document.select_one("#articleText")
        if isinstance(cleaned_source, Tag):
            source = cleaned_source
            for disclaimer in source.select("#div_with_disclaimer_id"):
                disclaimer.decompose()
    text = _clean_text(source.get_text(" ", strip=True))
    if len(text) < _MINIMUM_BODY_CHARACTERS:
        return None
    if source.select_one("#bwbodyimg:has(img)"):
        document = BeautifulSoup(str(source), "html.parser")
        preserved = document.select_one("#articleText")
        if isinstance(preserved, Tag):
            return preserved
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
                if re.search(r"<[a-z][^>]*>", legacy_caption, re.I)
                else legacy_caption
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


def _nyt_document_card_body(soup: BeautifulSoup) -> Tag | None:
    """Preserve Oak articles whose entire body is a linked source document."""
    card_link = soup.select_one(
        "section[name='articleBody'] a.thumbnail-link[href*='/interactive/']"
    )
    if not isinstance(card_link, Tag):
        return None
    card = card_link.find_parent("div")
    if not isinstance(card, Tag):
        return None
    read_link = card.select_one("a[href] strong")
    if (
        not isinstance(read_link, Tag)
        or "read document" not in _clean_text(
            read_link.get_text(" ", strip=True)
        ).casefold()
    ):
        return None
    href = _normalized_url(
        card_link.get("href"),
        base_url="https://www.nytimes.com/",
    )
    if not href:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    description = _first_text(
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "property", "og:description"),
    )
    if description:
        paragraph = document.new_tag("p")
        paragraph.string = description
        article.append(paragraph)
    heading_text = _tag_text(card.select_one("h2"))
    if heading_text:
        heading = document.new_tag("h2")
        heading.string = heading_text
        article.append(heading)
    iframe = document.new_tag("iframe")
    iframe["src"] = href
    iframe["title"] = heading_text or "Source document"
    article.append(iframe)
    return article


def _nyt_single_image_comics_body(soup: BeautifulSoup) -> Tag | None:
    """Recover intentionally image-only reviews published in comics format."""
    description = _first_text(
        _meta_content(soup, "name", "description"),
        _meta_content(soup, "property", "og:description"),
    )
    if not description or "comics format" not in description.casefold():
        return None
    article_body = soup.select_one("section[name='articleBody']")
    if not isinstance(article_body, Tag):
        return None
    if _clean_text(article_body.get_text(" ", strip=True)):
        return None
    source_image = soup.select_one(
        "article figure img[src], article img[itemprop='url'][src]"
    )
    if not isinstance(source_image, Tag):
        return None
    source = _tag_attribute(source_image, "src")
    if not source:
        return None
    document = BeautifulSoup("<article></article>", "html.parser")
    article = document.article
    if not isinstance(article, Tag):
        return None
    paragraph = document.new_tag("p")
    paragraph.string = description
    article.append(paragraph)
    figure = document.new_tag("figure")
    image = document.new_tag("img")
    image["src"] = source
    image["alt"] = _tag_attribute(source_image, "alt") or description
    figure.append(image)
    article.append(figure)
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
                    if "<" in caption
                    else html_module.unescape(caption)
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
    if (
        soup.find(
            string=lambda value: isinstance(value, Comment)
            and "shortarticle" in value.casefold()
        )
        and soup.select_one(
            ".articleSpanImage img[src], .articleInline img[src]"
        )
        and len(
            _clean_text(
                " ".join(
                    node.get_text(" ", strip=True)
                    for node in soup.select("[itemprop='articleBody']")
                )
            )
        )
        < _MINIMUM_BODY_CHARACTERS
    ):
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
    if (
        "/interactive/" in url
        and any(
            re.search(
                r"""DV\.(?:flexLoad|load)\(\s*["']"""
                r"""(?:https?:)?//(?:www\.)?documentcloud\.org/""",
                script.string or script.get_text(),
                flags=re.IGNORECASE,
            )
            for script in soup.select(".interactive-graphic script")
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


def _nyt_unhydrated_interactive_shell(
    soup: BeautifulSoup,
    *,
    content_type: ContentType,
    plain_text: str,
    blocks: list[ContentBlock],
    images: list[ImageCandidate],
) -> bool:
    """Reject a short NYT interactive whose actual media never hydrated."""
    if content_type != ContentType.INTERACTIVE or len(plain_text) >= 500:
        return False
    state = _nyt_preloaded_state(soup)
    if not any(
        isinstance(value, dict)
        and value.get("__typename") == "InteractiveBlock"
        for value in state.values()
    ):
        return False
    if any(image.should_archive for image in images):
        return False
    return not any(
        block.type in {BlockType.IMAGE, BlockType.EMBED, BlockType.TABLE}
        for block in blocks
    )


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
    if spec.publisher == "bloomberg":
        legacy_body = soup.select_one("#story_content")
        description = _meta_content(soup, "name", "description")
        if legacy_body is None or not description:
            return False
        paragraphs = [
            _clean_text(node.get_text(" ", strip=True))
            for node in legacy_body.find_all("p", recursive=False)
        ]
        paragraphs = [
            text
            for text in paragraphs
            if text
            and not re.match(
                r"(?i)^to contact the (?:reporter|editor)\b",
                text,
            )
        ]
        description_words = set(
            re.findall(r"[a-z0-9]+", description.casefold())
        )
        body_words = set(re.findall(r"[a-z0-9]+", plain_text.casefold()))
        return bool(
            80 <= len(plain_text) < 120
            and len(paragraphs) == 1
            and paragraphs[0] == plain_text
            and len(description_words) >= 8
            and len(description_words & body_words)
            / len(description_words)
            >= 0.9
            and not re.search(r"(?:\.\.\.|…)\s*$", plain_text)
        )
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
            and (
                (
                    "sports briefing" in page_text
                    and (
                        "by the associated press" in page_text
                        or "by associated press" in page_text
                    )
                )
                or (
                    headline.casefold().startswith("corrections:")
                    and re.fullmatch(
                        r"(?i)no corrections appeared in print on .+",
                        plain_text,
                    )
                )
            )
        )
    if spec.publisher == "wsj":
        section = _string_or_none(news_article.get("articleSection"))
        display_type = _meta_content(
            soup,
            "name",
            "article.type.display",
        )
        return bool(
            len(plain_text) >= _MINIMUM_BODY_CHARACTERS
            and (
                (section and "wire" in section.casefold())
                or (
                    display_type
                    and "dow jones newswires" in display_type.casefold()
                )
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


def _wsj_inset_table_body(soup: BeautifulSoup) -> Tag | None:
    """Render archived WSJ graphics data-table JSON into semantic tables."""
    decoder = json.JSONDecoder()
    payloads: list[dict[str, Any]] = []
    marker = re.compile(
        r"\bvar\s+insetData_[A-Za-z0-9_]+\s*=\s*"
        r"function\s*\(\s*\)\s*\{\s*return\s*",
    )
    for script in soup.find_all("script"):
        value = script.string or script.get_text()
        if not value or "insetData_" not in value:
            continue
        for match in marker.finditer(value):
            try:
                payload, _ = decoder.raw_decode(value[match.end() :])
            except (json.JSONDecodeError, TypeError):
                continue
            if (
                isinstance(payload, dict)
                and isinstance(payload.get("data"), list)
                and payload["data"]
            ):
                payloads.append(payload)
    if not payloads:
        return None
    document = BeautifulSoup(
        "<article data-jojo-source='wsj-inset-tables'></article>",
        "html.parser",
    )
    article = document.article
    if not isinstance(article, Tag):
        return None
    for payload in payloads:
        rows = [row for row in payload["data"] if isinstance(row, dict)]
        if not rows:
            continue
        configured_columns = payload.get("settings", {}).get("columns", [])
        columns = [
            str(column["name"])
            for column in configured_columns
            if isinstance(column, dict)
            and isinstance(column.get("name"), str)
            and column["name"] in rows[0]
        ]
        if not columns:
            columns = [str(key) for key in rows[0]]
        headline = _string_or_none(payload.get("headline"))
        if headline:
            heading = document.new_tag("h2")
            heading.string = headline
            article.append(heading)
        description = _string_or_none(payload.get("description"))
        if description and (
            not headline or description.casefold() != headline.casefold()
        ):
            paragraph = document.new_tag("p")
            paragraph.string = description
            article.append(paragraph)
        table = document.new_tag("table")
        header = document.new_tag("thead")
        header_row = document.new_tag("tr")
        for column in columns:
            cell = document.new_tag("th")
            cell.string = column
            header_row.append(cell)
        header.append(header_row)
        table.append(header)
        table_body = document.new_tag("tbody")
        for row in rows:
            table_row = document.new_tag("tr")
            for column in columns:
                cell = document.new_tag("td")
                cell.string = _clean_text(
                    BeautifulSoup(
                        f"<span>{row.get(column, '')}</span>",
                        "html.parser",
                    ).get_text(" ", strip=True)
                )
                table_row.append(cell)
            table_body.append(table_row)
        table.append(table_body)
        article.append(table)
        source = _string_or_none(payload.get("source"))
        if source:
            source_paragraph = document.new_tag("p")
            source_paragraph.string = f"Source: {source}"
            article.append(source_paragraph)
    return article if article.select_one("table") is not None else None


def _remove_noise(soup: BeautifulSoup, spec: PublisherSpec) -> None:
    for comment in list(
        soup.find_all(string=lambda value: isinstance(value, Comment))
    ):
        comment.extract()
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
        _remove_ft_body_chrome(soup)
        _remove_ft_newsletter_promos(soup)
        _strip_ft_copyright_suffixes(soup)
    if spec.publisher == "bloomberg":
        _remove_bloomberg_promos(soup)
    if spec.publisher == "nyt":
        _remove_nyt_promos(soup)
    if spec.publisher == "reuters":
        _remove_reuters_promos(soup)
        _normalize_reuters_legacy_press_release_media(soup)
    if spec.publisher == "wsj":
        _remove_wsj_promos(soup)


def _trim_bloomberg_subscription_tail(soup: BeautifulSoup) -> None:
    """Drop Bloomberg Professional subscription shells after real excerpts."""
    marker = next(
        (
            node
            for node in soup.select("p, div")
            if _clean_text(node.get_text(" ", strip=True))
            .casefold()
            .startswith(
                "to continue reading this article you must be a bloomberg "
                "professional service subscriber"
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


def _remove_bloomberg_damaged_attribution(soup: BeautifulSoup) -> None:
    """Drop a standalone joint byline whose contributor was lost upstream."""
    for node in list(soup.select("p")):
        text = _clean_text(node.get_text(" ", strip=True))
        if re.fullmatch(r"Bloomberg News\s+and", text, re.IGNORECASE):
            node.decompose()


def _remove_bloomberg_promos(soup: BeautifulSoup) -> None:
    # Zillow guest articles can append a home-search CTA, a labelled related
    # list, and an author recirculation bio inside Bloomberg's story body.
    for node in list(soup.select("p")):
        text = _clean_text(node.get_text(" ", strip=True))
        if re.fullmatch(
            r"To find mid-size homes for sale near you, .{1,240}",
            text,
            re.IGNORECASE,
        ):
            node.decompose()
            continue
        if re.fullmatch(
            r"Related items from Zillow Blog\s*:?",
            text,
            re.IGNORECASE,
        ):
            related_list = node.find_next_sibling()
            if (
                related_list is not None
                and related_list.name in {"ul", "ol"}
            ):
                related_list.decompose()
            node.decompose()

    # Bloomberg View used a paragraph of tildes as a semantic section break.
    # Preserve it as the schema's divider block instead of emitting a
    # punctuation-only paragraph.
    for node in list(soup.select("p")):
        if re.fullmatch(
            r"(?:~{3,}|-{3,})",
            _clean_text(node.get_text(" ", strip=True)),
        ):
            node.clear()
            node.name = "hr"

    # Some licensed mirrors append a bold Bloomberg credit to the final
    # reporting paragraph. Remove the terminal credit without dropping the
    # preceding sentence.
    for credit in list(soup.select("p > b:last-child, p > strong:last-child")):
        if re.fullmatch(
            r"Bloomberg",
            _clean_text(credit.get_text(" ", strip=True)),
            re.IGNORECASE,
        ):
            credit.decompose()

    # Legacy Bloomberg pages can insert a labelled recirculation list in the
    # middle of the reporting. Remove only the heading and its immediately
    # following all-link list; later reporting must remain intact.
    for heading in list(soup.select("h1, h2, h3, h4, h5, h6")):
        heading_text = _clean_text(heading.get_text(" ", strip=True))
        if not re.fullmatch(
            r"For more on .{1,120},\s*read this next:?",
            heading_text,
            re.IGNORECASE,
        ):
            continue
        related_list = heading.find_next_sibling()
        if related_list is None or related_list.name not in {"ul", "ol"}:
            continue
        items = list(related_list.find_all("li", recursive=False))
        if not items:
            continue
        if not all(
            item.select_one("a[href]")
            and _clean_text(item.get_text(" ", strip=True))
            == _clean_text(
                " ".join(
                    link.get_text(" ", strip=True)
                    for link in item.select("a[href]")
                )
            )
            for item in items
        ):
            continue
        related_list.decompose()
        heading.decompose()

    for node in list(soup.select("p")):
        text = _clean_text(node.get_text(" ", strip=True))
        if (
            re.fullmatch(r"Bloomberg", text, re.IGNORECASE)
            or re.fullmatch(
                r"(?:FIFW\s+)?NSN\s+[A-Z0-9]{10,14}\s*"
                r"<\s*GO\s*>\s+.+",
                text,
                re.IGNORECASE,
            )
            or re.fullmatch(
                r"bc-[a-z0-9]+(?:-[a-z0-9]+)+",
                text,
                re.IGNORECASE,
            )
            or re.fullmatch(
                r"Read more posts from .{1,100}\.",
                text,
                re.IGNORECASE,
            )
            or re.fullmatch(
                r"For other Bloomberg coverage,\s*click here\s*\.?",
                text,
                re.IGNORECASE,
            )
        ):
            node.decompose()

    for text_node in list(
        soup.find_all(
            string=re.compile(
                r"For other Bloomberg coverage,\s*click here\s*\.?",
                re.IGNORECASE,
            )
        )
    ):
        cleaned = re.sub(
            r"\s*For other Bloomberg coverage,\s*click here\s*\.?",
            "",
            str(text_node),
            flags=re.IGNORECASE,
        )
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    # Some 2015 Bloomberg pages append an unlabelled related-story paragraph
    # inside the story section. It contains only multiple Bloomberg article
    # links and no prose outside those anchors.
    for node in list(soup.select("p")):
        links = list(node.select("a[href]"))
        if len(links) < 2:
            continue
        if not all(
            re.search(
                r"(?:bloomberg(?:view)?\.com/)?(?:news/)?articles/",
                str(link.get("href") or ""),
                re.IGNORECASE,
            )
            for link in links
        ):
            continue
        paragraph_text = _clean_text(node.get_text(" ", strip=True))
        linked_text = _clean_text(
            " ".join(link.get_text(" ", strip=True) for link in links)
        )
        if paragraph_text == linked_text:
            node.decompose()

    # Insurance Journal article tags and in-content subscription cards use
    # these structural classes. At this stage ``soup`` is the isolated body
    # clone and no longer contains partner-host metadata.
    for node in list(
        soup.select(
            "p.tagtag, .subscribe-banner, "
            "[class*='subscribe-banner' i], .story-tags"
        )
    ):
        node.decompose()

    # WordPress partner mirrors can leak their comment form into a broadly
    # selected story container.
    for node in list(
        soup.select(
            "#comments, #respond, .comment-respond, .comment-reply-title, "
            ".left_sidebar, .widget-area, section.widget, .post-navigation"
        )
    ):
        node.decompose()

    # CTRM Center's advertising and recirculation widgets can be nested inside
    # an unclosed story paragraph. Remove the widget nodes themselves so the
    # Bloomberg reporting around them remains intact.
    for node in list(
        soup.select(
            ".inPost, .gsfnura, .mostRecentPosts"
        )
    ):
        node.decompose()
    for text_node in list(
        soup.find_all(string=re.compile(r"(?i)sponsored\s+links\s*$"))
    ):
        cleaned = re.sub(
            r"(?i)\s*sponsored\s+links\s*$",
            "",
            str(text_node),
        )
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    # Some partner excerpts append ``Read more at Bloomberg`` to the final
    # reporting paragraph rather than placing the link in its own node. Keep
    # the useful excerpt, but remove the recirculation phrase.
    for node in list(soup.select("p")):
        text = _tag_text(node)
        if not re.search(
            r"(?i)\bread\s+more\s+at\s+bloomberg\s*\.?\s*$",
            text,
        ):
            continue
        if not any(
            "bloomberg.com/" in str(anchor.get("href") or "").casefold()
            for anchor in node.select("a[href]")
        ):
            continue
        for anchor in list(node.select("a[href]")):
            if "bloomberg.com/" in str(anchor.get("href") or "").casefold():
                anchor.decompose()
        for text_node in list(node.find_all(string=True)):
            cleaned = re.sub(
                r"(?i)\s*read\s+more\s+at\s*$",
                "",
                str(text_node),
            )
            if cleaned != str(text_node):
                if cleaned:
                    text_node.replace_with(cleaned)
                else:
                    text_node.extract()
        for text_node in list(node.find_all(string=True)):
            if re.fullmatch(r"\s*\.\s*", str(text_node)):
                text_node.extract()

    # The Daily Economy's older licensed copies append a Bloomberg source
    # link, a duplicate headline/byline/date, and an unrelated stock-image
    # credit after the final reporting sentence. The source marker shares the
    # paragraph with useful prose, so trim from its linked ``Read more`` text
    # onward instead of dropping the whole paragraph.
    for source_link in list(soup.select("p a[href]")):
        if not re.fullmatch(
            r"Read more",
            _tag_text(source_link),
            re.IGNORECASE,
        ):
            continue
        if "bloomberg.com/" not in str(
            source_link.get("href") or ""
        ).casefold():
            continue
        paragraph = source_link.find_parent("p")
        if not isinstance(paragraph, Tag):
            continue
        prior_text = _clean_text(
            "".join(str(item) for item in paragraph.contents).split(
                str(source_link),
                1,
            )[0]
        )
        if len(prior_text) < 120:
            continue
        for item in list(paragraph.contents)[
            list(paragraph.contents).index(source_link) :
        ]:
            if isinstance(item, Tag):
                item.decompose()
            else:
                item.extract()
        next_paragraph = paragraph.find_next_sibling("p")
        if (
            isinstance(next_paragraph, Tag)
            and re.fullmatch(
                r"Image by .{2,160}",
                _tag_text(next_paragraph),
                re.IGNORECASE,
            )
        ):
            next_paragraph.decompose()

    # Licensed CTRM Center copies place a republication disclaimer inside the
    # selected story container. Match both halves of its distinctive wording
    # so ordinary Bloomberg references to republication are preserved.
    for node in list(soup.select("p, span")):
        text = _tag_text(node).casefold()
        if (
            "republished on the ctrm center" in text
            and "if you have any issue with this post" in text
        ):
            node.decompose()

    # Legacy Bloomberg slideshows render the first image twice and keep both a
    # shortened ``Read More`` caption and a full ``Close`` caption in the DOM.
    # Retain each slide's full caption and image exactly once.
    for node in list(
        soup.select(
            ".slideshow_teaser, .slide_caption .cap_preview, "
            ".slider_close, .slider_controls, .slider_nav"
        )
    ):
        node.decompose()
    for anchor in list(soup.select(".slide_caption .cap_show a")):
        if _tag_text(anchor).casefold() == "close":
            anchor.decompose()
    # Older Bloomberg image attachments keep a hidden lightbox copy of the
    # title, credit, and caption next to the visible caption. Keep the
    # lightbox's larger image candidate, but discard its duplicate text.
    for node in list(
        soup.select(".simple_overlay .image_title, .simple_overlay .details")
    ):
        node.decompose()

    # Businessweek inline illustrations sometimes use the caption and image
    # alt text solely for issue promotion. Keep the illustration and its
    # credit, but do not expose the issue date or subscription call as a
    # descriptive caption.
    businessweek_image_promo = re.compile(
        r"Featured in Bloomberg Businessweek\s*,?\s*"
        r"[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4}\.\s*"
        r"Subscribe now\s*\.?",
        re.IGNORECASE,
    )
    for caption in list(soup.select(".inline-media__caption")):
        if businessweek_image_promo.fullmatch(_tag_text(caption)):
            caption.decompose()
    for image in list(soup.select("img[alt]")):
        if businessweek_image_promo.fullmatch(
            _clean_text(str(image.get("alt") or ""))
        ):
            image["alt"] = ""

    # Partner mirrors may place a five-star voting form inside the article
    # container. It is interactive site chrome, not Bloomberg story content.
    for node in list(soup.select("form.rating, form#articleVotesSubmit")):
        node.decompose()

    # Some licensed partner pages append a link back to Bloomberg for the full
    # story, followed by the partner's membership and related-content modules.
    # The reporting before this marker is useful but necessarily partial.
    full_story_marker = next(
        (
            node
            for node in soup.select("p, div")
            if re.match(
                r"(?i)^(?:"
                r"click\s+here\s+to\s+read\s+the\s+full\s+story|"
                r"read\s+(?:the\s+)?full\s+article\s+here\s+"
                r"(?:via|at|on)\s+bloomberg"
                r")\b",
                _tag_text(node),
            )
            and any(
                "bloomberg.com/" in str(anchor.get("href") or "").casefold()
                for anchor in node.select("a[href]")
            )
        ),
        None,
    )
    if isinstance(full_story_marker, Tag):
        tail = full_story_marker
        while isinstance(tail.parent, Tag):
            for sibling in list(tail.next_siblings):
                if isinstance(sibling, Tag):
                    sibling.decompose()
                else:
                    sibling.extract()
            if tail.parent is soup or tail.parent.name in {"article", "main"}:
                break
            tail = tail.parent
        full_story_marker.decompose()

    # Syndicated Bloomberg forecast summaries sometimes append this provider
    # signup sentence after the source-article link.
    for node in list(soup.select("p, div, li")):
        if _tag_text(node).casefold().startswith(
            "click here to receive free and immediate email alerts"
        ):
            node.decompose()

    # Some Yahoo syndication captures contain provider HTML with every opening
    # angle bracket stripped (``/pp``, ``br /``, ``nbsp;/pp``). Preserve the
    # reporting while removing the provider upload/recirculation tail.
    for text_node in list(soup.find_all(string=re.compile(r"(?:nbsp;)?/pp"))):
        malformed = str(text_node)
        malformed = re.split(
            r"(?i)(?:nbsp;)?/ppem\s*uploaded by\b",
            malformed,
            maxsplit=1,
        )[0]
        malformed = re.sub(r"(?i)(?:^|\s)br\s*/", "\n\n", malformed)
        malformed = re.sub(r"(?i)(?:nbsp;)?/pp", "\n\n", malformed)
        text_node.replace_with(malformed.strip())

    for text_node in list(
        soup.find_all(string=re.compile(r"(?i)always-superb editing by"))
    ):
        linkedin_copy = str(text_node)
        linkedin_copy = re.split(
            r"(?is)\s+with\s+.{1,160}?\s+and\s+"
            r"always-superb editing by\b",
            linkedin_copy,
            maxsplit=1,
        )[0]
        text_node.replace_with(linkedin_copy.strip())

    shell_text = _clean_text(soup.get_text(" ", strip=True))
    shell_folded = shell_text.casefold()
    if "abitech analysis" in shell_folded:
        for card in list(soup.select(".card")):
            card.decompose()
    if (
        len(shell_text) < 400
        and "bias rating" in shell_folded
        and "reliability" in shell_folded
        and "politician portrayal" in shell_folded
    ):
        soup.clear()
        return

    bias_shell = next(
        (
            node
            for node in soup.select("p, h2, h3, h4, div")
            if _clean_text(node.get_text(" ", strip=True)).casefold().startswith(
                (
                    "want to see the in-depth bias analytics",
                    "create your free account to see the in-depth bias analytics",
                )
            )
        ),
        None,
    )
    if isinstance(bias_shell, Tag):
        tail = bias_shell
        while isinstance(tail.parent, Tag):
            for sibling in list(tail.next_siblings):
                if isinstance(sibling, Tag):
                    sibling.decompose()
                else:
                    sibling.extract()
            if tail.parent is soup or tail.parent.name in {"article", "main"}:
                break
            tail = tail.parent
        bias_shell.decompose()
        remaining = _clean_text(soup.get_text(" ", strip=True))
        if len(remaining) < 400 and "bias rating" in remaining.casefold():
            soup.clear()
            return

    """Remove legacy recirculation and standardized article footers."""
    for node in list(
        soup.select(
            ".text-to-speech, .brokerboxarticle, .terminal-tout-v2, "
            ".article-audio-attachment, "
            ".email-form, .similarstoryslide, button.read-more-button, "
            ".inner-page-cta-section, .minimal-detailfull-width-section, "
            ".ipsEntry__signature, [data-role='memberSignature'], "
            ".commentWrapper, .comments, #story_tools_bottom, "
            ".share_list, .entry_sharing, "
            ".youMightAlsoLike, .Pbanner, "
            ".relatedKeywords, .waChannelCta, .b-share-bar, "
            ".liveEventMain_widget, .primeSWrapper, .ts-dots, "
            ".bottomTopics, .topicListContainer, .topicListTitle, .tags"
            ", [id^='views-bootstrap-article-node-view-block-']"
            ", .article-share, .sharedaddy, .sd-sharing"
            ", .ai_podcast_030825, .ai_podcast_bottom_sticky_player_241025"
            ", .popup_ai_pb_overlay, .td_module_wrap, .td_block_wrap"
            ", .news-detail-content-block.ai-post, #story-source-gallery"
            ", .xenforo-comment-widget, .cbcalc-wrap, .ai-block, .lf-funnel"
            ", .usstock_widget"
            ", [data-testid='headline-stack-promo-liner-test-id']"
            ", [data-testid='tags-test-id']"
            ", [class*='GooglePreferredSource_']"
            ", img[src*='groundnews.b-cdn.net']"
            ", [data-animation-role='button'], "
            "[data-content-field='tags']"
        )
    ):
        node.decompose()

    for control in list(soup.select("[role='button'], button")):
        if (
            control.name == "a"
            and control.select_one("img") is not None
            and not _clean_text(control.get_text(" ", strip=True))
        ):
            control.unwrap()
        else:
            control.decompose()

    embedded_most_read = re.compile(
        r"(?is)\s*most read from bloomberg(?: businessweek)?.*$"
    )
    for text_node in list(soup.find_all(string=embedded_most_read)):
        cleaned = embedded_most_read.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)

    australia_briefing = re.compile(
        r"(?is)(?:and\s+)?for\s+a\s+daily\s+wrap\s+of\s+the\s+business,"
        r"\s*finance\s+and\s+economic\s+stories\s+that\s+matter\s+to\s+"
        r"australians,?\s*from\s+"
        r"bloomberg(?:'s|’s)\s+reporters\s+around\s+the\s+globe,\s*"
        r"sign\s+up\s+to\s+our\s+free\s+australia\s+briefing\s+"
        r"newsletter\.\s*"
    )
    for text_node in list(soup.find_all(string=australia_briefing)):
        cleaned = australia_briefing.sub("", str(text_node)).strip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    inside_canada_subscription = re.compile(
        r"(?is)\s*to\s+subscribe\s+to\s+inside\s+canada,\s*"
        r"click\s+here,\s*"
        r"hit\s+[“\"]display\s*&\s*edit[”\"]\s+and\s+then\s+"
        r"[“\"]set alert delivery[”\"]\s*$"
    )
    for text_node in list(
        soup.find_all(string=inside_canada_subscription)
    ):
        cleaned = inside_canada_subscription.sub(
            "",
            str(text_node),
        ).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    # Legacy Bloomberg product press releases keep the substantive release
    # and a standardized Professional-service sales pitch in one ``pre``
    # text node. Truncate only at Bloomberg's distinctive boilerplate opener;
    # the following company profile and media contacts belong to the footer.
    professional_service_footer = re.compile(
        r"(?is)\s+the\s+bloomberg\s+professional(?:\^)?®\s+service\s+"
        r"delivers\s+reliable\s+access\s+to\s+the\s+latest\s+market\s+"
        r"data,\s+financial\s+news,\s+and\s+economic\s+information\s+"
        r"critical\s+to\s+the\s+investment\s+decision\s+process\..*$"
    )
    for text_node in list(
        soup.find_all(string=professional_service_footer)
    ):
        cleaned = professional_service_footer.sub(
            "",
            str(text_node),
        ).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    # Bloomberg Sports product releases append a promotional link followed
    # by ``About`` profiles and press contacts as sibling blocks. Preserve
    # the preceding product announcement, but discard that standardized tail.
    for marker in list(soup.select("p")):
        if not re.fullmatch(
            r"For more information on Bloomberg Sports,\s*please visit "
            r"\S+ and follow us on Twitter\s*\(@BloombergSports\)\s*"
            r"and Facebook\.",
            _tag_text(marker),
            re.IGNORECASE,
        ):
            continue
        for sibling in list(marker.next_siblings):
            if isinstance(sibling, Tag):
                sibling.decompose()
            else:
                sibling.extract()
        marker.decompose()

    embedded_recommendation = re.compile(
        r"(?is)\s*read (?:next:\s*\S.+|also:)\s*$"
    )
    for text_node in list(soup.find_all(string=embedded_recommendation)):
        cleaned = embedded_recommendation.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    maritime_tail = re.compile(
        r"(?is)\s*©\s*\d{4}\s+bloomberg\s+l\.p\.\s*"
        r"subscribe\s+for\s+daily\s+maritime\s+insights\b.*$"
    )
    for text_node in list(soup.find_all(string=maritime_tail)):
        cleaned = maritime_tail.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    for marker in list(soup.select("p, h2, h3, h4")):
        marker_text = (
            _clean_text(marker.get_text(" ", strip=True))
            .casefold()
            .rstrip(":")
        )
        if marker_text not in {
            "related stories",
            "most read from bloomberg",
            "most read from bloomberg businessweek",
            "did you miss?",
            "for more on equity markets",
            "see also",
            "read more",
        }:
            continue
        sibling = marker.find_next_sibling()
        if isinstance(sibling, Tag) and sibling.name in {"ul", "ol"}:
            sibling.decompose()
        marker.decompose()

    for marker in list(soup.select("p")):
        text = _clean_text(marker.get_text(" ", strip=True))
        if not re.search(
            r"(?i)more from bloomberg(?: opinion)?:\s*$",
            text,
        ):
            continue
        sibling = marker.find_next_sibling()
        if isinstance(sibling, Tag) and sibling.name in {"ul", "ol"}:
            sibling.decompose()
        for text_node in list(
            marker.find_all(
                string=re.compile(
                    r"(?i)more from bloomberg(?: opinion)?:\s*$"
                )
            )
        ):
            cleaned = re.sub(
                r"(?i)\s*more from bloomberg(?: opinion)?:\s*$",
                "",
                str(text_node),
            ).rstrip()
            if cleaned:
                text_node.replace_with(cleaned)
            else:
                text_node.extract()

    for text_node in list(
        soup.find_all(
            string=re.compile(
                r"(?i)for related news and information\s*:\s*$"
            )
        )
    ):
        cleaned = re.sub(
            r"(?i)\s*for related news and information\s*:\s*$",
            "",
            str(text_node),
        ).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    for marker in list(soup.select("h2, h3, h4, h5, h6, p")):
        marker_text = _clean_text(marker.get_text(" ", strip=True)).casefold()
        if marker_text not in {
            "more on this topic",
            "see more on",
            "prev post",
            "source link",
            "top tech stories",
        }:
            continue
        tail = marker
        while isinstance(tail.parent, Tag):
            for sibling in list(tail.next_siblings):
                if isinstance(sibling, Tag):
                    sibling.decompose()
                else:
                    sibling.extract()
            if tail.parent is soup or tail.parent.name in {"article", "main"}:
                break
            tail = tail.parent
        marker.decompose()

    for preformatted in list(soup.select("pre")):
        raw_text = preformatted.get_text("\n", strip=False)
        contact_match = re.search(
            r"(?i)(?:^|\n)\s*(?:--\s*bloomberg news\s*\n\s*)?"
            r"to contact (?:the (?:writers?|authors?|reporters?|editors?)|"
            r"bloomberg news)\b",
            raw_text,
        )
        if contact_match:
            retained = raw_text[: contact_match.start()].rstrip()
            if re.fullmatch(
                r"(?i)\s*(?:--|—|–)\s*"
                r"[^\W\d_][\w .,'’\-]{1,100}\s*",
                retained,
            ):
                retained = ""
            if retained:
                preformatted.clear()
                preformatted.append(retained)
            else:
                preformatted.decompose()

    for table in list(soup.select("table")):
        table_text = _clean_text(table.get_text(" ", strip=True))
        if re.match(
            r"(?i)^(?:read more(?:\s+on the topic)?\s*:?\s+\S|"
            r"take the mliv pulse survey\b)",
            table_text,
        ):
            table.decompose()
    footer_patterns = (
        re.compile(r"(?i)^©\s*\d{4}\s+bloomberg\s+l\.?p\.?$"),
        re.compile(r"(?i)^©\s*\d{4}\s+bloomberg$"),
        re.compile(r"(?i)^(?:--|—|–)\s*bloomberg news\.?$"),
        re.compile(
            r"(?i)^please enable javascript to view the comments "
            r"powered by disqus\.?$"
        ),
        re.compile(
            r"(?i)^tweet\s+more business exchange\s+buzz up!?\s+"
            r"digg\s+print\s+email$"
        ),
        re.compile(
            r"(?i)^(?:#<[^<>]{1,100}>#\s*)?-0-\s+"
            r"[a-z]{3}/\d{1,2}/\d{4}\s+"
            r"\d{2}:\d{2}\s+gmt$"
        ),
        re.compile(r"(?i)^author$"),
        re.compile(r"(?i)^and yet equinor still\.*$"),
        re.compile(
            r"(?i)^https?://www\.gata\.org/sites/default/files/"
            r"gata-silver-round-front\.png$"
        ),
        re.compile(
            r"(?i)^get the latest nigerian news delivered to your inbox\.?$"
        ),
        re.compile(
            r"(?i)^follow .{1,100}(?:'|’)s business section "
            r"on twitter\.?$"
        ),
        re.compile(
            r"(?i)^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
        ),
        re.compile(r"(?i)^\*+\s*with bloomberg\.?$"),
        re.compile(
            r"(?i)^(?:(?:notice an issue\?\s*)?arabian post strives to "
            r"deliver the most accurate and reliable information to its "
            r"readers\.\s*)?if you believe you have identified an error "
            r"or inconsistency in this article\b.*$"
        ),
        re.compile(r"(?i)^follow arabian post$"),
        re.compile(
            r"(?i)^select arabian post as your preferred source on "
            r"google and msn news\b.*$"
        ),
        re.compile(
            r"(?i)^written by:\s*.+\s+—\s+with assistance from .+"
            r"@bloomberg$"
        ),
        re.compile(
            r"(?i)^(?:-{1,2}|—|–)\s*with assistance (?:from|by)\b.+"
            r"(?:\.\s*editors?\s*:.+)?$"
        ),
        re.compile(r"(?i)^with assistance (?:from|by)\b.+\.?$"),
        re.compile(
            r"(?i)^with assistance from\b.+\s+(?:--|—|–)\s*"
            r"editors?\s*:\s*.+$"
        ),
        re.compile(
            r"(?i)^for more articles like this,\s*"
            r"please visit us at bloomberg\.com\.?$"
        ),
        re.compile(
            r"(?i)^visit (?:https?://)?(?:www\.)?bloomberg\.com/"
            r"sustainability/? for the latest from bloomberg news about "
            r"energy,\s*natural resources and global business\.?$"
        ),
        re.compile(
            r"(?i)^for more about bloomberg bna,\s*click here\s*\.\s*"
            r"visit (?:https?://)?(?:www\.)?bloomberg\.com/"
            r"sustainability/? for the latest from bloomberg news about "
            r"energy,\s*natural resources and global business\.?$"
        ),
        re.compile(
            r"(?i)^visit the grid for the latest about energy,\s*"
            r"natural resources and global business\.?$"
        ),
        re.compile(
            r"(?i)^read more (?:opinion online|online opinion|online) "
            r"from bloomberg view\s*\.?$"
        ),
        re.compile(r"(?i)^read more bloomberg view editorials\s*\.?$"),
        re.compile(r"(?i)^today(?:'|’)s highlights\s*:\s*.+$"),
        re.compile(
            r"(?i)^read more opinion online from bloomberg view\s*\.\s*"
            r"subscribe to receive a daily e-?mail highlighting new view "
            r"(?:columns,\s*editorials|editorials,\s*columns) "
            r"and op-ed articles\.?$"
        ),
        re.compile(
            r"(?i)^for more quick commentary from bloomberg view,\s*"
            r"go to the ticker\s*\.?$"
        ),
        re.compile(
            r"(?i)^read more breaking commentary from bloomberg view "
            r"(?:(?:columnists|editors)(?:\s+and\s+(?:columnists|editors))?"
            r"\s+)?at the ticker\s*\.?$"
        ),
        re.compile(
            r"(?i)^read more breaking commentary from .{2,100} "
            r"and other bloomberg view columnists and editors "
            r"at the ticker\s*\.?$"
        ),
        re.compile(
            r"(?i)^for more,\s*read this quicktake\s*:\s*\S.+$"
        ),
        re.compile(r"(?i)^\*?\s*link to earlier story\s*:\s*\S.+$"),
        re.compile(r"(?i)^\*{3}\s*end of transcript\s*\*{3}$"),
        re.compile(r"(?i)^running time\s*:?\s*\d{1,3}:\d{2}$"),
        re.compile(r"^_{3,}$"),
        re.compile(r"(?i)^provider id\s*:\s*[0-9a-f]{32}$"),
        re.compile(
            r"(?i)^contributed via\s*:\s*"
            r"bloomberg publisher web service$"
        ),
        re.compile(
            r"(?i)^generated by bloomberg publisher web service$"
        ),
        re.compile(
            r"(?i)^.{2,250}\bcontributed to this report\.?$"
        ),
        re.compile(
            r"(?i)^.{2,250}\bcontributed to this story"
            r"(?:\s+from\s+.{2,100})?\.?$"
        ),
        re.compile(
            r"(?i)^to watch the video,\s*click here\s*\.?$"
        ),
        re.compile(
            r"(?i)^webrep\s+currentvote\s+norating\s+noweight$"
        ),
        re.compile(
            r"(?i)^(?:--|—|–)\s*[^\W\d_][\w .,'’\-]{1,100}$"
        ),
        re.compile(
            r"(?i)^to see the patent,\s*click\s*:\s*[\d,]+\.?$"
        ),
        re.compile(r"(?i)^to see the patent\s*:\s*[\d,]+\.?$"),
        re.compile(
            r"(?i)^to read the publisher(?:'|’)s web page on the book,\s*"
            r"https?://\S+\.?$"
        ),
        re.compile(
            r"(?i)^to buy this book(?:\s+in\s+"
            r"(?:north america|the u\.?s\.?))?\s*,\s*click here\s*\.?$"
        ),
        re.compile(r"^(?:[•·]\s*){3,}$"),
        re.compile(
            r"(?i)^watch charlie rose on bloomberg tv weeknights\b.*$"
        ),
        re.compile(
            r"(?i)^(?:—|–|--?)\s*with\s+"
            r"[^\W\d_][\w .,'’\-]{1,100}$"
        ),
        re.compile(
            r"(?i)^(?:--|—)\s*[\w .,'’&-]+\s+in\s+"
            r"[\w .,'’&-]+\s+(?:\(\+?\d{1,3}\)|\+?\d)"
            r"[\d -]{6,}$"
        ),
        re.compile(
            r"(?i)^(?:--|—)\s*bloomberg radio\s+\+?\d[\d -]{6,}$"
        ),
        re.compile(
            r"(?i)^siehe dazu auch\s*:\s*fortlaufende kurzmeldungen\s*:\s*"
            r"first\s*<go>\s*first word überschriften\s*:\s*"
            r"nh\s+bfw\s*<go>\s*$"
        ),
        re.compile(
            r"(?i)^überschrift des artikels im original\s*:\s*\S.+$"
        ),
        re.compile(
            r"(?i)^[a-z]{2,5}\s+[a-z0-9]{8,14}\s+<go>\s+\S.+$"
        ),
        re.compile(
            r"(?i)^(?:[a-z]{2,5}\s+)?nsn\s+"
            r"[a-z0-9]{8,14}\s+<go>\s+\S.+$"
        ),
        re.compile(
            r"(?is)^to analyze this 13f\s*:.*<go>.*"
            r"to analyze all 13f(?:'|’)s filed,.*<go>.*$"
        ),
        re.compile(
            r"(?i)^emerging-markets market view\s*:\s*\{emmv\b.*$"
        ),
        re.compile(r"(?is)^相關新聞和信息\s*：.*<go>.*$"),
        re.compile(r"(?is)^相关新闻和信息\s*[：:].*<go>.*$"),
        re.compile(r"(?is)^原文标题\s+\S.+$"),
        re.compile(r"(?is)^관련 기사 및 정보 보기\s*:.*<go>.*$"),
        re.compile(r"(?is)^원본 기사\s*:.*$"),
        re.compile(r"(?is)^--\s*취재보조\s*:.*$"),
        re.compile(r"(?is)^본 기사의 번역자\s*:.*$"),
        re.compile(
            r"(?is)^.*\bnsn\s+[a-z0-9]{8,14}\s*<go>\s*$"
        ),
        re.compile(
            r"(?i)^(?:today(?:'|’)s\s+)?muse highlights include "
            r".{2,180}\.?$"
        ),
        re.compile(
            r"(?i)^(?:today(?:'|’)s\s+)?muse highlights include\s*:\s*"
            r".{2,180}\.?$"
        ),
        re.compile(
            r"(?i)^\(?to save a copy of the chart,\s*click here\.\)?$"
        ),
        re.compile(r"(?i)^click here for (?:the )?web link\.?$"),
        re.compile(r"(?i)^for related news and information\s*:?.*$"),
        re.compile(r"(?is)^related news and information\s*:.*$"),
        re.compile(r"(?i)^for more on .{2,160},\s*click here\.?$"),
        re.compile(
            r"(?i)^for the latest verdict and settlement news,\s*"
            r"click here\.?$"
        ),
        re.compile(
            r"(?i)^for the latest new suits news,\s*click here\.\s*"
            r"for copies of recent civil complaints,\s*click here\.?$"
        ),
        re.compile(
            r"(?i)^for the latest litigation department news,\s*"
            r"click here\.?$"
        ),
        re.compile(
            r"(?i)^for the latest lawsuits news,\s*click here\.?$"
        ),
        re.compile(
            r"(?i)^for the latest trial and appeals news,\s*click here\.?$"
        ),
        re.compile(
            r"(?i)^this is a bloomberg podcast\.\s*to download,\s*"
            r"watch or listen (?:to this report )?now,\s*click here\.?$"
        ),
        re.compile(
            r"(?i)^\(?to listen to the podcast,\s*click here\s*\.?\)?$"
        ),
        re.compile(r"(?i)^for more,\s*read this next\s*:\s*$"),
        re.compile(r"(?i)^for more,\s*read this next\s*:\s*\S.+$"),
        re.compile(
            r"(?i)^for more,\s*click here"
            r"(?:,\s*and\s*click here)?\s*\.?$"
        ),
        re.compile(
            r"(?i)^for the video,\s*click here,\s*and for more,\s*"
            r"click here\.?$"
        ),
        re.compile(r"(?i)^for the video,\s*click here\.?$"),
        re.compile(r"(?i)^for the audio,\s*click here\.?$"),
        re.compile(
            r"(?i)^to read more from .{2,180},\s*click here\s*\.?$"
        ),
        re.compile(r"(?i)^read more echoes columns online\s*\.?$"),
        re.compile(r"(?i)^read more from echoes online\s*\.?$"),
        re.compile(r"(?i)^read more bloomberg view columns\s*\.?$"),
        re.compile(
            r"(?i)^read more(?:\s+from)?\s+echoes\s*,?\s*"
            r"bloomberg view(?:'|’)s economic history blog\s*\.?$"
        ),
        re.compile(
            r"(?i)^for (?:more )?(?:copyright|patent|trademark) news,\s*"
            r"click here\.?$"
        ),
        re.compile(
            r"(?i)^link to company news\s*:\s*"
            r"\{[^{}]{1,80}<equity>\s+cn(?:\s+<go>)?\}"
            r"(?:\s*\{[^{}]{1,80}<equity>\s+cn(?:\s+<go>)?\})*\s*$"
        ),
        re.compile(
            r"(?i)^(?:link to company news\s*:\s*"
            r"\{[^{}]{1,80}<equity>\s+cn(?:\s+<go>)?\}\s*){2,}$"
        ),
        re.compile(
            r"(?i)^link to statement\s*:\s*\{\s*https?://[^{}\s]+\s*\}\s*"
            r"link to company news\s*:\s*"
            r"\{[^{}]{1,80}<equity>\s+cn(?:\s+<go>)?\}\s*$"
        ),
        re.compile(
            r"(?i)^link to statement\s*:\s*"
            r"\{\s*nsn\s+[a-z0-9]{8,14}\s+<go>\s*\}\s*$"
        ),
        re.compile(
            r"(?i)^story link\s*:\s*"
            r"\{\s*nsn\s+[a-z0-9]{8,14}\s*<go>\s*\}\s*$"
        ),
        re.compile(
            r"(?i)^bi airm\s*<go>\s+for commercial aircraft "
            r"manufacturers(?:'|’) dashboard\s+bi airl eu\s*<go>\s+"
            r"european airline dashboard\s+bi airmg indd\s*<go>\s+"
            r"monthly orders for new aircraft,\s*parked fleet "
            r"statistics\.?$"
        ),
        re.compile(
            r"(?i)^\(to be sent this nordic credit column,\s*click here\.\s*"
            r"for more credit market news,\s*top cm\.\)$"
        ),
        re.compile(
            r"(?i)^to see the methodology and exact wording of the poll "
            r"questions,\s*click on the attachment tab at the top of "
            r"the story\.?$"
        ),
        re.compile(
            r"(?i)^.{2,100}\bis (?:the )?.{2,100} for bloomberg\.\s*"
            r"follow (?:him|her|them) on twitter\b.*$"
        ),
        re.compile(
            r"(?i)^.{2,100}\bis (?:the )?(?:co-)?author of "
            r".{2,300}\.\s+(?:he|she|they) (?:advises?|writes?|works?)\b"
            r".{2,300}\.\s*follow (?:him|her|them) on twitter\b.*$"
        ),
        re.compile(r"(?i)^(?:\*t\s*)+$"),
        re.compile(
            r"(?i)^to contact the "
            r"(?:authors? of|editors? responsible for|reporters? on) "
            r"this (?:story|article)\s*:"
        ),
        re.compile(
            r"(?i)^to contact the (?:lead )?author of this column\s*:"
        ),
        re.compile(
            r"(?i)^to contact (?:the )?bloomberg news staff for this "
            r"(?:story|article)\s*:"
        ),
        re.compile(
            r"(?i)^to contact the "
            r"(?:writers?|authors?|reporters?|editors?) "
            r"(?:for|of|on|responsible for) (?:this|the|his|her) "
            r"(?:story|article|column|review|slideshow|(?:blog )?post)\s*:?"
        ),
        re.compile(
            r"(?i)^to see a slideshow of photos\b.*"
            r"\{[^{}]{1,30}<go>\}.*\{[^{}]{1,30}<go>\}\.?$"
        ),
        re.compile(
            r"(?i)^click on [“\"]send comment[”\"] in (?:the )?sidebar display "
            r"to send a letter to the editor\.?$"
        ),
        re.compile(
            r"(?i)^(?:(?:-{1,2}|—|–)\s*)?"
            r"editors?\s*:\s*[\w .,'’&-]+$"
        ),
        re.compile(r"(?i)^editors?\s*:\s*$"),
        re.compile(
            r"(?i)^(?:-{1,2}|—|–)\s*[\w .,'’&-]+\.\s*"
            r"editors?\s*:\s*[\w .,'’&-]+$"
        ),
        re.compile(r"(?i)^[a-z0-9._%+-]+@bloomberg\.net\s*[.;]?$"),
        re.compile(
            r"(?i)^join the discussion on the bloomberg businessweek "
            r"business school forum\b"
        ),
        re.compile(
            r"(?i)^©\s*\d{4}\s+trend news agency\.?\s*"
            r"all rights reserved\.?$"
        ),
        re.compile(
            r"(?i)^to contact the (?:senior )?editor responsible for "
            r"bloomberg view(?:'s|’s) editorials\s*:"
        ),
        re.compile(
            r"(?i)^to contact the (?:senior )?editor responsible for "
            r"bloomberg opinion(?:'s|’s) editorials\s*:"
        ),
        re.compile(
            r"(?i)^this (?:column|article) does not necessarily reflect "
            r"the opinion of (?:the editorial board or )?"
            r"bloomberg lp and its owners\.?$"
        ),
        re.compile(
            r"(?i)^\(?this (?:column|article) does not necessarily reflect "
            r"the opinion of (?:the editorial board or )?"
            r"bloomberg lp and its owners\.\)?$"
        ),
        re.compile(
            r"(?i)^\(?this (?:column|article) does not necessarily reflect "
            r"the opinion of bloomberg (?:view|opinion)(?:'|’)s "
            r"editorial board or bloomberg lp,\s*its owners and investors"
            r"\.\)?$"
        ),
        re.compile(
            r"(?is)^this transcript may not be 100% accurate\b.*"
            r"any opinion expressed in the transcript does not necessarily "
            r"reflect the views of bloomberg lp\.?$"
        ),
        re.compile(
            r"(?i)^follow @\w+ for all the latest news, and sign up (?:for|to) "
            r"our daily .+ newsletter\.?$"
        ),
        re.compile(
            r"(?i)^follow @\w+ on twitter for more on .{2,160}\.?$"
        ),
        re.compile(
            r"(?i)^subscribe to .+ on "
            r"(?:itunes|apple) podcasts(?:\s+subscribe to .+ on "
            r"pocket casts)?\.?$"
        ),
        re.compile(
            r"(?i)^subscribe to .+ on pocket casts\.?$"
        ),
        re.compile(
            r"(?i)^subscribe to .+ on pocketcasts?\.?$"
        ),
        re.compile(
            r"(?i)^terminal users\s*:\s*click here to play now\.?$"
        ),
        re.compile(
            r"(?i)^if you(?:'|’)d like to get the daily prophet in "
            r"e-?mail form, right in your inbox, please subscribe "
            r"to this link\s*\.\s*thanks!?"
        ),
        re.compile(
            r"(?i)^start your day with what(?:'|’)s moving markets in asia\. "
            r"sign up here to receive our newsletter\.?$"
        ),
        re.compile(
            r"(?i)^sign up for (?:our new china newsletter|china rising)\s*,"
            r"\s*a (?:new )?weekly dispatch(?:\s+coming soon)? on where china "
            r"stands now and where it(?:'|’)s going next\.?$"
        ),
        re.compile(
            r"(?i)^sign up to receive the brexit bulletin in your inbox, "
            r"and follow @brexit on twitter\.?$"
        ),
        re.compile(
            r"(?i)^sign up to receive the brexit bulletin, a daily briefing "
            r"on the biggest news related to britain(?:'|’)s departure "
            r"from the eu\.?$"
        ),
        re.compile(
            r"(?i)^a version of this column originally appeared in "
            r"bloomberg(?:'|’)s fully charged technology newsletter\. "
            r"you can sign up here\s*\.?$"
        ),
        re.compile(
            r"(?i)^want to hear more\? subscribe on apple podcasts and "
            r"pocket casts for new episodes every week\."
        ),
        re.compile(
            r"(?i)^\(?\s*sign up for the .+ newsletter, your best source "
            r"for .+\)?\.?$"
        ),
        re.compile(
            r"(?i)^want to go deeper inside .+\? sign up for .+ newsletter "
            r"from bloomberg\.?$"
        ),
        re.compile(
            r"(?i)^for a fresh perspective on .+, sign up for our weekly "
            r"newsletter\s*\.?$"
        ),
        re.compile(
            r"(?i)^want\s+more\s+personal\s+finance\s+news\?\s*"
            r"sign\s+up\s+for\s+our\s+weekly\s+personal\s+finance\s+"
            r"newsletter,\s*wealth\s+watch\.?\s*$"
        ),
        re.compile(
            r"(?i)^new to bloomberg opinion today\?\s*"
            r"(?:sign up\s+)?and follow us on twitter and facebook\s*\.?$"
        ),
        re.compile(
            r"(?i)^(?:sign up here\s+)?and follow us on twitter "
            r"and facebook\s*\.?$"
        ),
        re.compile(
            r"(?i)^sign up for bloomberg(?:'|’)s daily technology "
            r"newsletter here\s*\.?$"
        ),
        re.compile(
            r"(?i)^subscribe now to stay ahead with the most trusted "
            r"business news source\.?$"
        ),
        re.compile(
            r"(?i)^(?:follow ht tech on\s+)?facebook\s*,\s*google news\s*,"
            r"\s*and instagram\s*\.\s*for our latest videos,\s*"
            r"subscribe to our youtube channel\s*\.?$"
        ),
        re.compile(
            r"(?i)^catch all the latest tech news\s*,\s*mobile news\s*,"
            r".*for our latest videos,\s*subscribe to our youtube "
            r"channel\s*\.?$"
        ),
        re.compile(
            r"(?i)^sign up for our .+ weekly newsletter, follow us @\w+ "
            r"and subscribe to our podcast\.?$"
        ),
        re.compile(
            r"(?i)^for the best in travel, food, drinks, fashion, cars, "
            r"and life, sign up for the pursuits newsletter\s*\.\s*"
            r"delivered weekly\.?$"
        ),
        re.compile(
            r"(?i)^want to receive this post, and more, into your inbox "
            r"every morning\?\s*sign up here\.?$"
        ),
        re.compile(
            r"(?i)^for more (?:copyright|patent) news,\s*click here\.?$"
        ),
        re.compile(
            r"(?i)^for related stories\s+to see today(?:'|’)s top "
            r"sports stories,\s*see:\s*\{ispo\s*<go>\}\.?$"
        ),
        re.compile(
            r"(?is)^related news and information:\s*"
            r".*\{[^{}]{1,30}<go>\}.*\{[^{}]{1,30}<go>\}.*$"
        ),
        re.compile(
            r"(?i)^to view the source of this information click here\.?$"
        ),
        re.compile(r"(?i)^read more\s*:\s*\S.+$"),
        re.compile(
            r"(?i)^get early returns every morning in your inbox\.\s*"
            r"click here to subscribe\.\s*also subscribe to bloomberg "
            r"all access\b.*$"
        ),
        re.compile(
            r"(?i)^want more bloomberg opinion\?\s*terminal readers "
            r"head to opin\s*<go>\.\s*web readers click here\.?$"
        ),
        re.compile(
            r"(?i)^for more bloomberg opinion,\s*subscribe to our "
            r"newsletter\.?$"
        ),
        re.compile(
            r"(?i)^sign up for the brief,\s*a daily afternoon newsletter "
            r"showcasing bloomberg law(?:'s|’s) top stories\.?$"
        ),
        re.compile(
            r"(?i)^sign up for bloomberg(?:'s|’s) business of sports "
            r"newsletter\b.*$"
        ),
        re.compile(
            r"(?i)^sign up for the equality newsletter for weekly "
            r"reporting\b.*$"
        ),
        re.compile(
            r"(?i)^sign up for the washington edition newsletter to "
            r"find out how the worlds? of money and politics intersect "
            r"in the us capital\.?$"
        ),
        re.compile(
            r"(?i)^sign up for the twice-weekly next africa newsletter "
            r"for the latest business and economic news from the "
            r"continent\.?$"
        ),
        re.compile(
            r"(?i)^sign up here for the twice-weekly next africa "
            r"newsletter,\s*and subscribe to the next africa podcast\b.*$"
        ),
        re.compile(
            r"(?i)^or want more lifestyle and passion stories\?\s*"
            r"click here\.?$"
        ),
        re.compile(
            r"(?i)^generated by readers,\s*the comments included herein "
            r"do not reflect the views and opinions of rigzone\.\s*"
            r"all comments are subject to editorial review\..*$"
        ),
        re.compile(
            r"(?i)^(?:\(bloomberg\)\s*(?:--|—)\s*)?sign up for "
            r"(?:the\s+)?(?:daily\s+)?india "
            r"edition newsletter\b.*$"
        ),
        re.compile(
            r"(?i)^sign up for the business of food newsletter\b.*$"
        ),
        re.compile(
            r"(?i)^want more bloomberg opinion\?\s*opin\s*<go>\.\s*"
            r"or (?:you can )?subscribe to our daily newsletter\.?$"
        ),
        re.compile(
            r"(?i)^[\u200b-\u200f\u2060\ufeff]*"
            r"want more (?:from )?bloomberg opinion\?\s*"
            r"opin\s*<go>(?:\s*on the terminal)?\.\s*"
            r"(?:web readers,?\s*click here\.\s*)?"
            r"or (?:you can )?subscribe to our daily newsletter\.?$"
        ),
        re.compile(
            r"(?i)^[\u200b-\u200f\u2060\ufeff]*"
            r"want more bloomberg opinion\?\s*terminal readers "
            r"head to opin\s*<go>\.\s*or (?:you can )?subscribe to our "
            r"daily newsletter\.?$"
        ),
        re.compile(
            r"(?i)^[\u200b-\u200f\u2060\ufeff]*"
            r"want more bloomberg opinion\?\s*head to opin\s*<go>\.\s*"
            r"or (?:you can )?subscribe to our daily newsletter\.?$"
        ),
        re.compile(
            r"(?i)^more stories like this are available on "
            r"bloomberg\.com\.?$"
        ),
        re.compile(
            r"(?i)^sign up here and follow us on threads,\s*tiktok,\s*"
            r"twitter,\s*instagram and facebook\.?$"
        ),
        re.compile(
            r"(?i)^subscribe to the economic times prime and read the "
            r"et epaper online\.?$"
        ),
        re.compile(
            r"(?i)^\(?catch all the business news\s*,\s*breaking news "
            r"and latest news updates on the economic times\s*\.\)?$"
        ),
        re.compile(r"(?i)^more on bloomberg:?$"),
        re.compile(r"(?i)^read more\s*@\s*bloomberg\.?$"),
        re.compile(
            r"(?i)^you want more news on this market\?\s*click here for "
            r"a curated first word channel\b.*$"
        ),
        re.compile(
            r"(?i)^take the mliv pulse survey\b.*share your thoughts\.?$"
        ),
        re.compile(r"(?i)^continue for free$"),
        re.compile(r"(?i)^you can follow lev menand at @levmenand\.?$"),
        re.compile(
            r"(?i)^follow the market issue situation with our daily "
            r"updates\.?$"
        ),
        re.compile(r"(?i)^what do you think\?$"),
        re.compile(
            r"(?i)^get in-depth insights from our expert contributors,\s*"
            r"and dive into financial and economic trends\.?$"
        ),
        re.compile(
            r"(?i)^new us stocks insights\s*&\s*wraps\b.*"
            r"click here to see and subscribe\.?$"
        ),
        re.compile(
            r"(?i)^read more stories about where the money flows,\s*"
            r"and analysis of the biggest market stories from singapore "
            r"and around the world\.?$"
        ),
        re.compile(
            r"(?i)^click here to stay updated with the latest business "
            r"& investment news in singapore\.?$"
        ),
        re.compile(r"(?i)^source:\s*https?://(?:www\.)?bloomberg\.com/?$"),
        re.compile(r"(?i)^source\s*:\s*bloomberg\.?$"),
        re.compile(r"(?i)^read:\s+.{10,200}$"),
        re.compile(r"(?i)^to view or add a comment,\s*sign in\.?$"),
        re.compile(r"(?i)^thank you for your report!?$"),
        re.compile(
            r"(?i)^please enable javascript to view this content\.?$"
        ),
        re.compile(r"(?i)^uploaded by .{2,100}$"),
        re.compile(r"(?i)^top trending stocks\s*:.*share price\b.*$"),
        re.compile(r"(?i)^get automatic alerts for this topic\.?$"),
        re.compile(r"(?i)^about this source$"),
        re.compile(
            r"(?i)^⚠?\ufe0f?\s*disclaimer:\s*this content is for training "
            r"purposes only\b.*$"
        ),
        re.compile(
            r"(?i)^this article was generated from an automated news "
            r"agency feed without modifications to text\.?$"
        ),
        re.compile(r"(?i)^share this\s*:$"),
        re.compile(r"(?i)^📰\s*source$"),
        re.compile(
            r"(?i)^for complete coverage and additional details,\s*"
            r"visit the original article published by bloomberg\.com\.?$"
        ),
        re.compile(r"(?i)^bloomberg\.com$"),
        re.compile(
            r"(?i)^subscribe to et prime and read the economic times "
            r"epaper online\..*$"
        ),
        re.compile(
            r"(?is)^\(?what(?:'|’)s moving sensex and nifty\b.*"
            r"subscribe to our telegram feeds\s*\.\)?$"
        ),
        re.compile(r"(?i)^read the full article$"),
        re.compile(
            r"(?i)^get the latest insurance news sent straight to "
            r"your inbox\.?$"
        ),
        re.compile(r"(?i)^maritime and shipping$"),
        re.compile(r"(?i)^discussion$"),
        re.compile(
            r"(?i)^the post .+ first appeared on bloomberg\.?$"
        ),
        re.compile(
            r"(?i)^©\s*\d{4}\s+the block\.\s*all rights reserved\..*$"
        ),
        re.compile(
            r"(?i)^unlock full access to podcast analytics,\s*"
            r"audience demographics\b.*$"
        ),
        re.compile(
            r"(?i)^recipients will be able to read the full text of "
            r"the article after submitting their email address\b.*$"
        ),
        re.compile(r"(?i)^原文標題\s*.+$"),
        re.compile(r"(?i)^interested in profit loss\s*\?$"),
        re.compile(r"(?i)^interested in claims\s*\?$"),
        re.compile(r"(?i)^listen to this article in summarized format$"),
        re.compile(r"(?i)^most popular$"),
        re.compile(r"(?i)^want to stay up to date\?$"),
        re.compile(r"(?i)^get more podcast analytics$"),
        re.compile(
            r"(?i)^ai-analyzed african market trends delivered to "
            r"your inbox\b.*$"
        ),
        re.compile(r"(?i)^the source\s*:\s*bloomberg$"),
        re.compile(
            r"(?i)^get push alerts the moment our analysts spot setups "
            r"around news events\b.*$"
        ),
        re.compile(
            r"(?i)^sign up here for the daily next africa newsletter "
            r"and subscribe to the next africa podcast\b.*$"
        ),
        re.compile(r"(?i)^advertisement\s*:\s*$"),
        re.compile(r"(?i)^here are more articles you may enjoy\.?$"),
        re.compile(r"(?i)^trade these moves with signalpro$"),
        re.compile(
            r"(?i)^related coverage:\s*.+"
        ),
        re.compile(
            r"(?i)^each image keeps its publisher,\s*caption or article "
            r"title,\s*citation text\b.*$"
        ),
        re.compile(r"(?i)^was this article valuable\?$"),
        re.compile(
            r"(?i)^estimates show your actual share of cashback\b.*"
            r"see full vip trader hub\s*→?$"
        ),
        re.compile(r"(?i)^interested in ai\s*\?$"),
        re.compile(
            r"(?i)^want more bloomberg opinion\?\s*terminal readers,?\s*"
            r"head\s*to\s*opin\s*<go>\.\s*or subscribe to our daily "
            r"newsletter\.?$"
        ),
        re.compile(
            r"(?i)^move the slider to your real monthly trading volume\b.*$"
        ),
        re.compile(
            r"(?i)^disclaimer:\s*the block is an independent media "
            r"outlet that delivers news,\s*research,\s*and data\b.*$"
        ),
        re.compile(
            r"(?i)^previous article\s+next article\b.*$"
        ),
        re.compile(r"(?i)^buy gold$"),
        re.compile(r"(?i)^trending now$"),
        re.compile(
            r"(?i)^build draft survey skills through practical training\b.*$"
        ),
        re.compile(
            r"(?i)^written (?:by|by:)\s+.{2,100}(?:@bloomberg)?$"
        ),
        re.compile(r"(?i)^how much could you earn back per year\?$"),
        re.compile(r"(?i)^related articles$"),
        re.compile(r"(?i)^topics\s+lawsuits\s+claims\s+oklahoma$"),
        re.compile(
            r"(?i)^for complete coverage and additional details,\s*"
            r"visit the original article published by bloomberg"
            r"(?:\.com)?\.?$"
        ),
        re.compile(r"(?i)^cashback calculator$"),
        re.compile(r"(?i)^advanced draft survey$"),
        re.compile(r"(?i)^printer friendly version$"),
        re.compile(
            r"(?i)^trading involves risk of loss\.\s*cashback rates "
            r"are estimates\b.*$"
        ),
        re.compile(r"(?i)^african reviewer\s+view all posts$"),
        re.compile(r"(?i)^s&p 500 top losers$"),
        re.compile(
            r"(?is)^share on facebook \(opens in new window\).*"
            r"share on x \(opens in new window\)\s*x$"
        ),
        re.compile(
            r"(?is)^exclusive stories\s+daily epaper access\s+"
            r"smart market tools\s+curated investment ideas\s+"
            r"ad-lite experience\s+subscription$"
        ),
        re.compile(
            r"(?is)^want to share this article\?\s*upgrade to "
            r"all-access now\b.*$"
        ),
        re.compile(
            r"(?i)^bluesky\s+x\s+threads\s+facebook\s+email$"
        ),
        re.compile(r"(?i)^advertisement\s*\d*$"),
        re.compile(
            r"(?i)^this commercial has not loaded but,?\s*however your "
            r"article continues under\.?$"
        ),
        re.compile(r"(?i)^sign in or create an account$"),
        re.compile(r"^(?:_{5,}|-{5,}|={5,})$"),
        re.compile(
            r"(?i)^.{1,100}\sis\s.{1,100}\sat bloomberg\.\s*"
            r"follow (?:him|her|them) on twitter\b.*"
            r"(?:instagram|facebook)\b.*$"
        ),
    )
    grid_promo = re.compile(
        r"(?i)^visit the grid for the latest about energy,\s*"
        r"natural resources and global business\.?$"
    )
    more_by_author = re.compile(
        r"(?i)^more by .{2,120}(?:\bon twitter\b.*)?:$"
    )
    for marker in list(soup.select("p")):
        if not more_by_author.fullmatch(
            _clean_text(marker.get_text(" ", strip=True))
        ):
            continue
        related = marker.find_next_sibling()
        if isinstance(related, Tag) and related.name in {"ul", "ol"}:
            related.decompose()
            marker.decompose()

    for marker in list(soup.select("h2, h3, h4, p")):
        if not re.fullmatch(
            r"(?i)(?:(?:for more,\s*)?read this next\s*:?|"
            r"for more on .{2,160},\s*check out .{2,80}\s*:|"
            r"related\s*:?)",
            _clean_text(marker.get_text(" ", strip=True)),
        ):
            continue
        related = marker.find_next_sibling()
        if isinstance(related, Tag) and related.name in {"ul", "ol"}:
            related.decompose()
            marker.decompose()

    for marker in list(soup.select("p, h2, h3, h4")):
        if not re.fullmatch(
            r"(?i)more from .{2,120}\s*:",
            _clean_text(marker.get_text(" ", strip=True)),
        ):
            continue
        related = marker.find_next_sibling()
        if isinstance(related, Tag) and related.name in {"ul", "ol"}:
            related.decompose()
            marker.decompose()

    for marker in list(soup.select("p")):
        if (
            _clean_text(marker.get_text(" ", strip=True)).casefold()
            != "daily podcast"
        ):
            continue
        title = marker.find_next_sibling()
        description = (
            title.find_next_sibling()
            if isinstance(title, Tag) and title.name == "p"
            else None
        )
        if not (
            isinstance(description, Tag)
            and description.name == "p"
            and re.search(
                r"(?is)\bpodcast on the bloomberg terminal\b.*"
                r"\bto listen,\s*click here\.?$",
                _clean_text(description.get_text(" ", strip=True)),
            )
        ):
            continue
        description.decompose()
        title.decompose()
        marker.decompose()

    for module in list(soup.select("div.story_inline.assets")):
        if module.select_one("div.author") and module.select_one("div.related"):
            module.decompose()

    for promo in list(soup.select("p")):
        if not grid_promo.fullmatch(
            _clean_text(promo.get_text(" ", strip=True))
        ):
            continue
        related = promo.find_previous_sibling()
        if isinstance(related, Tag) and related.name in {"ul", "ol"}:
            related.decompose()
        promo.decompose()

    for paragraph in list(soup.select("p")):
        text = _clean_text(paragraph.get_text(" ", strip=True))
        trimmed = re.sub(
            r"(?i)\s+follow (?:him|her|them) on twitter"
            r"(?:\s+at)?(?:\s+@\w+)?\s*\.?\s*\)$",
            ")",
            text,
        )
        trimmed = re.sub(
            r"(?i)\s+e-?mail (?:him|her|them) and\s*\)$",
            ")",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+for more dine\s*&\s*deal reviews,\s*"
            r"click here\.\)$",
            ")",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+to buy this book(?:\s+in\s+"
            r"(?:north america|the u\.?s\.?))?,\s*click here\s*\.?$",
            "",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+to listen,\s*go to\s+[a-z]{2,8}\s*<go>\.\)$",
            ")",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+click here for (?:the )?playoff schedule\.?$",
            "",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+click here for other college football "
            r"game schedules\.?$",
            "",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+for details,\s*click here\.\)$",
            ")",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s*\*?\s*link to earlier story\s*:\s*.*$",
            "",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s*\{[a-z]{2,8}\s+\d{5,12}\s+<go>\}\s*$",
            "",
            trimmed,
        )
        trimmed = re.sub(r"(?i)\s+-bloomberg\s*$", "", trimmed)
        trimmed = re.sub(
            r"(?i),\s*accessible on live\s*<go>\s*\.\)$",
            ".)",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+see\s+\{?\s*live\s*<go>\s*\}?\s*\.\)$",
            ")",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+can be accessed at\s+\{?\s*live\s*<go>\s*\}?"
            r"\s*\.\s*",
            ". ",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+\*?\s*for change in stock futures oi,\s*"
            r"see fmon\s*<go>\s*$",
            "",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)\s+click\s+[a-z]{1,8}(?:\s+[a-z]{1,8})?\s*"
            r"<equity>\s+evts\s*<go>\s+to listen\.\)$",
            ")",
            trimmed,
        )
        trimmed = re.sub(
            r"(?i)^([^.]{1,180}\.)\s+follow (?:him|her|them) on twitter"
            r"(?:\s+at)?\s+@\w+\s*\.?$",
            r"\1",
            trimmed,
        )
        if trimmed != text:
            paragraph.clear()
            paragraph.append(trimmed)

    for paragraph in list(soup.select("p")):
        paragraph_text = _clean_text(paragraph.get_text(" ", strip=True))
        assistance_bio = re.fullmatch(
            r"(?is)^\(\s*with assistance (?:from|by)\b.+?\.\s+"
            r"(.+\b(?:bloomberg|muse)\b.+)\)$",
            paragraph_text,
        )
        if assistance_bio is not None:
            paragraph.clear()
            paragraph.append(f"({assistance_bio.group(1)})")

    for paragraph in list(soup.select("p")):
        paragraph_text = _clean_text(paragraph.get_text(" ", strip=True))
        assistance = re.search(
            r"\s+With assistance from\b.{2,300}\.?\s*$",
            paragraph_text,
        )
        if assistance is None:
            continue
        retained = paragraph_text[: assistance.start()].rstrip()
        if not retained.endswith((".", "!", "?")):
            continue
        for text_node in list(paragraph.find_all(string=True)):
            match = re.search(
                r"\s+With assistance from\b.{2,300}\.?\s*$",
                str(text_node),
            )
            if match is not None:
                text_node.replace_with(str(text_node)[: match.start()])
                break

    for marker in list(soup.select("p")):
        if _clean_text(marker.get_text(" ", strip=True)).casefold() != "source":
            continue
        source_link = marker.find_next_sibling()
        if not isinstance(source_link, Tag) or source_link.name != "p":
            continue
        source_text = _clean_text(source_link.get_text(" ", strip=True))
        if not re.fullmatch(
            r"(?i)https?://(?:www\.)?"
            r"(?:bloomberg\.com|businessweek\.com)/\S+",
            source_text,
        ):
            continue
        source_link.decompose()
        marker.decompose()

    for marker in list(soup.select("p")):
        if (
            _clean_text(marker.get_text(" ", strip=True)).casefold()
            != "note to editors"
        ):
            continue
        if not any(
            isinstance(sibling, Tag)
            and sibling.name == "p"
            and re.fullmatch(
                r"(?i)for further information please contact\s*:?",
                _clean_text(sibling.get_text(" ", strip=True)),
            )
            for sibling in marker.next_siblings
        ):
            continue
        for sibling in list(marker.next_siblings):
            sibling.extract()
        marker.decompose()

    for heading in list(soup.select("h2, h3, h4")):
        if _clean_text(heading.get_text(" ", strip=True)).casefold() != "statistics":
            continue
        previous = heading.find_previous_sibling()
        if isinstance(previous, Tag) and previous.name in {"pre", "table"}:
            heading.decompose()
            continue
        if any(
            isinstance(sibling, Tag)
            and sibling.name in {"p", "pre", "table", "figure", "img"}
            and _clean_text(sibling.get_text(" ", strip=True))
            for sibling in heading.next_siblings
        ):
            continue
        heading.decompose()

    contact_footer = re.compile(
        r"(?i)^to contact (?:the )?"
        r"(?:reporters?|writers?|authors?|editors?|bloomberg news staff)\b"
    )
    for heading in list(soup.select("h2, h3, h4")):
        sibling = heading.find_next_sibling()
        while isinstance(sibling, Tag) and sibling.name in {"h2", "h3", "h4"}:
            sibling = sibling.find_next_sibling()
        if (
            isinstance(sibling, Tag)
            and sibling.name == "p"
            and contact_footer.match(
                _clean_text(sibling.get_text(" ", strip=True))
            )
        ):
            heading.decompose()

    for node in list(
        soup.select("p, li, span, em, div, h2, h3, h4, blockquote")
    ):
        text = _clean_text(node.get_text(" ", strip=True))
        if (
            text.casefold() == "watch this next"
            or any(pattern.search(text) for pattern in footer_patterns)
            or (
                node.name in {"p", "li", "span"}
                and "@bloomberg.net" in text.casefold()
                and len(text) <= 400
            )
            or re.fullmatch(r"[\u200b-\u200f\u2060\ufeff]+", text)
            or re.fullmatch(r"(?:\*\s*){2,}", text)
            or (
                node.name in {"h2", "h3", "h4"}
                and re.fullmatch(
                    r"[\s‘’“”'\"….,:;!?—–-]+",
                    text,
                )
            )
            or text == "🫣"
        ):
            node.decompose()

    for paragraph in list(soup.select("p")):
        links = paragraph.find_all("a")
        if len(links) != 1:
            continue
        link = links[0]
        text = _clean_text(paragraph.get_text(" ", strip=True))
        link_text = _clean_text(link.get_text(" ", strip=True))
        href = str(link.get("href") or "")
        if (
            text == link_text
            and link_text
            and paragraph.find_next_sibling("p") is None
            and re.search(
                r"(?i)(?:bloomberg\.com)?/news/articles/\d{4}-\d{2}-\d{2}/",
                href,
            )
        ):
            paragraph.decompose()

    for link in list(soup.select("a")):
        text = _clean_text(link.get_text(" ", strip=True)).casefold()
        href = str(link.get("href") or "").casefold()
        if (
            text == "sign up here"
            and "bloombergbusiness.com/join/" in href
        ):
            link.decompose()

    for listing in list(soup.select("ul")):
        items = listing.find_all("li", recursive=False)
        if len(items) < 2:
            continue
        if all(
            item.find(
                "a",
                attrs={"title": re.compile(r"(?i)^click to view webpage\.?$")},
            )
            is not None
            for item in items
        ):
            listing.decompose()

    personal_finance_newsletter_suffix = re.compile(
        r"(?i)\s*want\s+more\s+personal\s+finance\s+news\?\s*"
        r"sign\s+up\s+for\s+our\s+weekly\s+personal\s+finance\s+"
        r"newsletter,\s*wealth\s+watch\.?\s*$"
    )
    for text_node in list(
        soup.find_all(string=personal_finance_newsletter_suffix)
    ):
        cleaned = personal_finance_newsletter_suffix.sub(
            "", str(text_node)
        ).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    view_promo_suffix = re.compile(
        r"(?i)\s*read more opinion online from bloomberg view\s*\.\s*"
        r"subscribe to receive a daily e-?mail highlighting new view "
        r"(?:editorials,\s*columns|columns,\s*editorials) "
        r"and op-ed articles\.?\s*$"
    )
    for paragraph in list(soup.select("p")):
        text = _clean_text(paragraph.get_text(" ", strip=True))
        cleaned = view_promo_suffix.sub("", text).rstrip()
        if cleaned == text:
            continue
        if cleaned:
            paragraph.clear()
            paragraph.append(cleaned)
        else:
            paragraph.decompose()

    partner_work_suffix = re.compile(
        r"(?i)\s+read more of (?:his|her|their) work here\s*\.?\s*$"
    )
    for paragraph in list(soup.select("p")):
        text = _clean_text(paragraph.get_text(" ", strip=True))
        cleaned = partner_work_suffix.sub("", text).rstrip()
        if cleaned == text:
            continue
        if cleaned:
            paragraph.clear()
            paragraph.append(cleaned)
        else:
            paragraph.decompose()

    disclaimer_suffix = re.compile(
        r"(?i)\s*\(?this\s+(?:column|article)\s+does\s+not\s+necessarily"
        r"\s+reflect\s+the\s+opinion\s+of\s+"
        r"(?:(?:the\s+)?editorial\s+board\s+or\s+)?"
        r"bloomberg\s+lp\s+and\s+its\s+owners\.\)?\s*$"
    )
    for text_node in list(soup.find_all(string=disclaimer_suffix)):
        cleaned = disclaimer_suffix.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    malformed_partner_tail = re.compile(
        r"(?is)(?:/?p)?em\s*uploaded by .*$"
    )
    for text_node in list(soup.find_all(string=malformed_partner_tail)):
        cleaned = malformed_partner_tail.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    for marker in list(soup.select("p")):
        marker_text = _clean_text(marker.get_text(" ", strip=True))
        if not re.fullmatch(
            r"(?i)\.{3}\s*advertisement\s*\.{3}",
            marker_text,
        ):
            continue
        previous_rule = marker.find_previous_sibling("hr")
        next_rule = marker.find_next_sibling("hr")
        if not isinstance(previous_rule, Tag) or not isinstance(next_rule, Tag):
            marker.decompose()
            continue
        current = previous_rule
        while current is not None:
            following = current.next_sibling
            current.extract()
            if current is next_rule:
                break
            current = following

    partner_recruiting_tail = re.compile(
        r"(?is)\s*(?:despite the downturn,\s*trading firms still continue "
        r"to build out their options trading capabilities|"
        r"to discuss these opportunities confidentially)\b.*$"
    )
    for text_node in list(soup.find_all(string=partner_recruiting_tail)):
        cleaned = partner_recruiting_tail.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    inline_signup = re.compile(
        r"(?i)\s*;\s*(?:sign up here)?\s*\.\s*"
    )
    for text_node in list(soup.find_all(string=inline_signup)):
        cleaned = inline_signup.sub(". ", str(text_node), count=1)
        text_node.replace_with(cleaned)

    for heading in soup.select("h1, h2, h3, h4"):
        text = _clean_text(heading.get_text(" ", strip=True))
        if not re.match(r"(?i)^watch (?:this )?next\s*:", text):
            continue
        sibling = heading.find_next_sibling()
        if (
            isinstance(sibling, Tag)
            and sibling.name == "figure"
            and any(
                marker in {
                    str(value).casefold()
                    for value in sibling.get("class", [])
                }
                for marker in ("inline-video", "video")
            )
        ):
            sibling.decompose()
        heading.decompose()

    for heading in list(soup.select("h2, h3, h4")):
        if (
            _clean_text(heading.get_text(" ", strip=True)).casefold()
            != "market-related stories"
        ):
            continue
        for sibling in list(heading.next_siblings):
            sibling.extract()
        heading.decompose()

    # Legacy Bloomberg figures made the image container act like a lightbox
    # button.  Keep the figure and image, but do not preserve browser-only
    # interaction semantics in the archived article body.
    for thumbnail in list(
        soup.select(
            ".thumbnail_container.overlay_container > a.enlarge_image"
        )
    ):
        overlay = thumbnail.find_next_sibling(
            "div",
            class_="simple_overlay",
        )
        if isinstance(overlay, Tag) and overlay.find("img"):
            thumbnail.decompose()

    for node in soup.select(
        "figure [role='button'][aria-label='Open image in viewer']"
    ):
        node.attrs.pop("role", None)
        node.attrs.pop("tabindex", None)
        node.attrs.pop("aria-label", None)

    # Some legacy pages place an otherwise empty print/share control inside
    # the selected story body.
    for node in list(
        soup.select("[class*='SocialShare-'][role='button']")
    ):
        node.decompose()

    for node in list(
        soup.select(".comment-count-v2__link, .disqus-v2__tout")
    ):
        node.decompose()


def _remove_nyt_promos(soup: BeautifulSoup) -> None:
    """Remove NYT sponsorship, subscription and standardized engagement UI."""
    for button in list(
        soup.select(
            "button[aria-label='expand or collapse modal'], "
            "button.ad-slide-skip, button.comments-button, "
            "button[class*='SectionBarShare-shareButton'], "
            "button[class*='SaveToWatchlistButton__saveToWatchlistButton'], "
            "button[class*='LikeButton__likeButton'], "
            "button#comment-callout-comment-button"
        )
    ):
        button.decompose()

    for button in list(soup.select("button")):
        if _clean_text(button.get_text(" ", strip=True)).casefold() in {
            "view more",
            "comment on artsbeat",
        }:
            button.decompose()

    for node in list(
        soup.select("figure.byline, figure[data-testid='byline']")
    ):
        node.decompose()
    patterns = (
        re.compile(r"(?i)^supported by$"),
        re.compile(r"(?i)^share full article$"),
        re.compile(
            r"(?i)^subscriber support helps make times journalism possible\b"
        ),
        re.compile(r"(?i)^\(?want to get .*briefing by email\?"),
        re.compile(
            r"(?i)^sign up here to get (?:this newsletter|"
            r"the briefing)\b"
        ),
        re.compile(
            r"(?i)^if you are not a subscriber to this newsletter\b"
        ),
        re.compile(r"(?i)^browse our full range of times newsletters\b"),
        re.compile(
            r"(?i)^the times is committed to publishing a diversity "
            r"of letters to the editor\b"
        ),
        re.compile(
            r"(?i)^follow the new york times opinion section on\b"
        ),
        re.compile(r"(?i)^for newspaper delivery questions\b"),
        re.compile(r"^_{2,}$"),
        re.compile(r"(?i)^read more$"),
        re.compile(
            r"(?i)^\[?\s*(?:enjoying this article\?\s*)?"
            r"sign up for (?:our|the) .*newsletter\b"
        ),
        re.compile(
            r"(?i)^52 places and much, much more\b.*"
            r"sign up for our travel dispatch newsletter\b"
        ),
        re.compile(
            r"(?i)^want more from modern love\?.*"
            r"sign up for the newsletter\b"
        ),
        re.compile(
            r"(?i)^\[\s*like the science times page\b.*"
            r"sign up for the science times newsletter\b"
        ),
    )
    for node in list(soup.select("p, li, span")):
        text = _clean_text(node.get_text(" ", strip=True))
        if any(pattern.search(text) for pattern in patterns):
            node.decompose()


def _remove_reuters_promos(soup: BeautifulSoup) -> None:
    """Remove Reuters registration UI and licensed-partner subscription tails."""
    for node in list(
        soup.select(
            ".rich-share, [data-testid='rich-share'], "
            ".Image_expand-button, .Slideshow_expand-button, "
            "[aria-label='Expand Image Slideshow']"
        )
    ):
        node.decompose()

    for button in list(soup.select("button")):
        classes = " ".join(button.get("class") or []).casefold()
        if "socialtools" in classes:
            button.decompose()
        else:
            button.unwrap()

    for node in list(
        soup.select(
            "[class*='pagination-v2__container' i][role='button'], "
            "a[role='button']"
        )
    ):
        node.decompose()
    for node in list(soup.select("[role='button']")):
        node.attrs.pop("role", None)
        node.attrs.pop("tabindex", None)

    for marker in list(soup.select("[data-testid^='paragraph-']")):
        if _clean_text(marker.get_text(" ", strip=True)).casefold() != "read more:":
            continue
        candidates: list[Tag] = [marker]
        sibling = marker.find_next_sibling()
        boundary_found = False
        while isinstance(sibling, Tag) and len(candidates) <= 6:
            text = _clean_text(sibling.get_text(" ", strip=True)).casefold()
            if text.startswith(("reporting by ", "editing by ")):
                boundary_found = True
                break
            candidates.append(sibling)
            sibling = sibling.find_next_sibling()
        if not boundary_found:
            continue
        for node in candidates:
            node.decompose()

    for node in list(soup.select("p, div, span")):
        text = _clean_text(node.get_text(" ", strip=True))
        if re.fullmatch(r"[_^]{3,}", text):
            node.decompose()

    for node in list(soup.select("p, h2, h3, h4, h5, h6")):
        text = _clean_text(node.get_text(" ", strip=True)).casefold()
        if text.startswith(
            "register now for free unlimited access to reuters.com"
        ) or text.startswith(
            "the company and law firm names shown above are generated "
            "automatically based on the text of the article"
        ):
            node.decompose()

    wire_copyright_suffix = re.compile(
        r"""(?ix)\s*(?:"""
        r"""copyright\s+business\s+wire\s+\d{4}"""
        r"""|"""
        r"""copyright\s+\d{4},\s*market\s+wire,\s*"""
        r"""all\s+rights\s+reserved\.\s*-0-"""
        r""")\s*$"""
    )
    for text_node in list(soup.find_all(string=wire_copyright_suffix)):
        cleaned = wire_copyright_suffix.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    legacy_legal_suffix = re.compile(
        r"""(?is)\s*(?:"""
        r"""(?:keywords:\s*)?[^\n]{0,500}?"""
        r"""\(c\)\s*reuters\s+(?:19|20)\d{2}\.\s*"""
        r"""all\s+rights\s+reserved\..*$"""
        r"""|"""
        r"""(?:copyright(?:\s+copyright)?|©|ï¿½)\s*(?:©\s*)?"""
        r"""(?:19|20)\d{2}[\s,.][^\n]{0,750}?"""
        r"""all\s+rights\s+reserved\.?.*$"""
        r""")\s*$"""
    )
    for text_node in list(soup.find_all(string=legacy_legal_suffix)):
        cleaned = legacy_legal_suffix.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()

    marker = next(
        (
            node
            for node in soup.select("p")
            if _clean_text(node.get_text(" ", strip=True))
            .casefold()
            .startswith("already a subscriber? log in")
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


def _trim_wsj_roadblock_tail(soup: BeautifulSoup) -> None:
    """Drop the subscription roadblock and recirculation appended after it."""
    marker = soup.select_one("[class*='ArticleRoadblock' i]")
    if not isinstance(marker, Tag):
        marker = next(
            (
                node
                for node in soup.select("p, h2, h3, h4")
                if _clean_text(node.get_text(" ", strip=True))
                .casefold()
                .startswith("to read the full story")
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


def _remove_wsj_promos(soup: BeautifulSoup) -> None:
    """Remove metered-view controls, copyright footers and coupon modules."""
    for button in list(soup.select("button")):
        button.decompose()
    for tagline in list(soup.select("p.articleTagLine")):
        if re.fullmatch(
            r"[_=—–-]+",
            _clean_text(tagline.get_text(" ", strip=True)),
        ):
            tagline.decompose()
    for strap in list(soup.select(".strap-container")):
        heading = strap.select_one("h2, h3, h4, h5, h6, .strap")
        if (
            isinstance(heading, Tag)
            and _clean_text(heading.get_text(" ", strip=True)).casefold()
            == "related video"
        ):
            strap.decompose()
    for rich_text in list(soup.select(".media-object-rich-text")):
        heading = rich_text.select_one("h2, h3, h4, h5, h6")
        if (
            isinstance(heading, Tag)
            and _clean_text(heading.get_text(" ", strip=True)).casefold()
            == "share your thoughts"
        ):
            rich_text.decompose()
    for paragraph in list(soup.select("p")):
        if (
            _clean_text(paragraph.get_text(" ", strip=True))
            .casefold()
            .startswith(
                "to explore and search through all our recipes, "
                "check out the new wsj recipes page"
            )
        ):
            paragraph.decompose()

    for node in list(
        soup.select(
            ".coupon-list, [class*='SavingsUnited' i], "
            "[class*='SnippetSignIn' i], .author-links, .author-info, "
            "[class*='mobile-modal-author' i], .byline-wrap, "
            ".article__byline, .module.automated-news, "
            ".module.editors-picks, .share-bottom, .printSummary, "
            ".article-news-front, [class*='AuthoringContainer'], "
            "[data-block='doNotPrint'], "
            "[data-module-zone='opinion_editors_picks'], "
            "[data-module-zone='contentCarousel'], "
            ".content-carousel, .olympics-carousel, "
            "[class*='-JRStrap'], [class*='-JRNextArticle'], "
            "[class*='-JRMoreArticles'], "
            ".opinion-editors-picks"
        )
    ):
        node.decompose()
    for node in list(soup.select(".media-object.inline")):
        heading = node.select_one("h2, h3, h4, h5, h6")
        if (
            isinstance(heading, Tag)
            and _clean_text(heading.get_text(" ", strip=True))
            .casefold()
            .startswith("more ")
            and node.select_one("ul.articleList") is not None
        ):
            node.decompose()
    for heading in list(soup.select("h2.subhead")):
        if (
            _clean_text(heading.get_text(" ", strip=True)).casefold()
            != "opinion editor's picks"
        ):
            continue
        sibling = heading.find_next_sibling()
        if isinstance(sibling, Tag) and sibling.name in {"ul", "ol"}:
            sibling.decompose()
        heading.decompose()
    for control in list(soup.select("a[role='button']")):
        if _clean_text(control.get_text(" ", strip=True)).casefold() != "see all":
            continue
        collection = next(
            (
                parent
                for parent in control.parents
                if isinstance(parent, Tag)
                and len(parent.select("article")) >= 2
            ),
            None,
        )
        if isinstance(collection, Tag):
            collection.decompose()
        else:
            control.decompose()
    for wrapper in list(soup.select(".theme-nav-wrapper")):
        inset = wrapper.find_parent(
            class_=lambda value: value
            and "article__inset" in " ".join(
                value if isinstance(value, list) else [value]
            )
        )
        (inset if isinstance(inset, Tag) else wrapper).decompose()
    for node in list(soup.select(".media-object.type-InsetRichText")):
        text = _clean_text(node.get_text(" ", strip=True)).casefold()
        if (
            text.startswith("stay informed get a coronavirus briefing")
            and "sign up here" in text
        ):
            node.decompose()
    for node in list(soup.select("p, h2, h3, h4, h5, h6")):
        if node.parent is None:
            continue
        text = _clean_text(node.get_text(" ", strip=True))
        folded = text.casefold()
        classes = " ".join(node.get("class") or []).casefold()
        if (
            text in {".", "\u200b", "\ufeff"}
            or (
                folded.startswith("copyright ©")
                and "dow jones & company" in folded
            )
            or folded.startswith("already a member? sign in")
            or folded in {"listen", "listen to article"}
            or re.fullmatch(r"\(\d+\s+min(?:ute)?s?\)", folded)
            or (
                folded == "videos"
                and "sectionlabel" in classes
            )
            or (
                folded.startswith(
                    "buy side from wsj expert recommendations "
                    "on products and services"
                )
                and node.select_one(
                    "a[href*='wsj.com/buyside']"
                )
            )
        ):
            node.decompose()
            continue
        if (
            "sign up for our" in folded
            and "newsletter" in folded
            and (
                folded.startswith(("—for more wsj", "-for more wsj"))
                or len(folded) <= 400
            )
        ):
            node.decompose()


def _remove_ft_body_chrome(soup: BeautifulSoup) -> None:
    """Remove Next-era sharing, recirculation and follow-topic UI."""
    for component in list(soup.select(".flashcomponent")):
        link = component.select_one("a.flashlink[href]")
        if not isinstance(link, Tag):
            continue
        source = str(link.get("href") or "").strip()
        if not source:
            continue
        iframe = soup.new_tag("iframe")
        iframe["src"] = source
        iframe["title"] = (
            _clean_text(link.get_text(" ", strip=True))
            or "Archived FT interactive"
        )
        iframe["data-interactive-provider"] = "ft-flash"
        component.replace_with(iframe)

    for node in list(
        soup.select(
            "[data-toolbar='share'], "
            ".article-info__byline, "
            ".o-message__content-main, "
            ".story-package[data-track-comp-name='moreOn'], "
            ".insideArticleShare, "
            ".ftlabsaudioplayerholder, "
            ".component-share__button, "
            "button[data-trackable='save-for-later']"
        )
    ):
        node.decompose()
    for node in list(soup.select("p")):
        text = _clean_text(node.get_text(" ", strip=True))
        if re.fullmatch(
            r"(?i)see acast\.com/privacy for privacy and "
            r"opt-out information\.?",
            text,
        ):
            node.decompose()
            continue
        if re.match(r"(?i)^recommended\s*\*", text):
            node.decompose()
            continue
        if re.match(
            r"(?i)^follow @financialtimesfashion on instagram\b",
            text,
        ) and "subscribe to culture call" in text.casefold():
            node.decompose()
            continue
        if re.match(
            r"(?i)^the ft is offering a free \d+-day trial to "
            r"coronavirus business update\b",
            text,
        ):
            node.decompose()

    tail_markers: list[Tag] = list(
        soup.select(
            ".instant-alert-cta__text, "
            ".h2-promoted-content, "
            ".concept-list__title, "
            ".comments__disabled-message"
        )
    )
    for node in soup.select("h2, h3, p"):
        text = _clean_text(node.get_text(" ", strip=True)).casefold()
        if (
            text == "promoted content"
            or text.startswith("follow the topics in this ")
            or (
                text.startswith("get alerts on ")
                and text.endswith(" when a new story is published")
            )
            or (
                text.startswith(
                    "ft subscriber? sign up for the weekly "
                )
                and " newsletter" in text
            )
        ):
            tail_markers.append(node)
    if not tail_markers:
        return
    top = soup.find()
    if not isinstance(top, Tag):
        return
    marker_ids = {id(marker) for marker in tail_markers}
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


def _remove_ft_newsletter_promos(soup: BeautifulSoup) -> None:
    """Remove newsletter cards flattened into FT syndication body paragraphs."""
    for card in list(soup.select("experimental")):
        if card.select_one(
            "a[href*='ep.ft.com'][href*='newsletter'][href*='subscribe']"
        ):
            card.decompose()
    for paragraph in list(soup.select("p")):
        text = _clean_text(
            paragraph.get_text(" ", strip=True)
        ).casefold()
        if (
            text.startswith("sign up to ")
            and "must-read weekly briefing" in text
            and paragraph.select_one(
                "a[href*='ep.ft.com']"
                "[href*='newsletter'][href*='subscribe']"
            )
        ):
            paragraph.decompose()
    for heading in list(soup.select("h2, h3, h4, h5, h6")):
        if (
            _clean_text(heading.get_text(" ", strip=True)).casefold()
            != "related stories"
        ):
            continue
        sibling = heading.find_next_sibling()
        if isinstance(sibling, Tag) and sibling.name in {"ul", "ol"}:
            sibling.decompose()
        heading.decompose()
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
        re.compile(
            r"(?i)^the financial times is making key coronavirus "
            r"coverage free to read\b"
        ),
        re.compile(
            r"(?i)^if you are a subscriber and would like to receive "
            r"alerts when lex articles are published\b"
        ),
        re.compile(r"(?i)^follow .+ with\s*myft and on\s*twitter\b"),
        re.compile(r"(?i)^sign up to our .+ newsletter\b"),
        re.compile(r"(?i)^for more, sign up for our .+ newsletter\b"),
        re.compile(r"(?i)^ft premium subscribers can sign up here\b"),
        re.compile(r"(?i)^lex publishes two popular newsletters\b"),
        re.compile(
            r"(?i)^house\s*&\s*home unlocked\b.*\b"
            r"(?:newsletter|sign up)\b"
        ),
        re.compile(
            r"(?i)^ft subscribers can sign up for the email version\b"
        ),
        re.compile(
            r"(?i)^ft subscribers can click here to receive .* by email\b"
        ),
        re.compile(
            r"(?i)^coronavirus business update\s+sign up here "
            r"for our newsletter\b"
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
        r"""(?ix)(?:"""
        r"""^\s*copyright\s+(?:the\s+)?financial\s+times\s+limited"""
        r"""(?:\s+\d{4})?(?:\.\s*all\s+rights\s+reserved\.|\.)?\s*$"""
        r"""|"""
        r"""\s*[_–—-]\s*copyright\s+(?:the\s+)?"""
        r"""financial\s+times\s+limited(?:\s+\d{4})?"""
        r"""(?:\.\s*all\s+rights\s+reserved\.|\.)?\s*$"""
        r""")"""
    )
    for text_node in list(soup.find_all(string=pattern)):
        cleaned = pattern.sub("", str(text_node)).rstrip()
        if cleaned:
            text_node.replace_with(cleaned)
        else:
            text_node.extract()
    for node in list(soup.select("p")):
        if _clean_text(node.get_text(" ", strip=True)) == ".":
            node.decompose()


def _remove_ap_body_promos(soup: BeautifulSoup) -> None:
    """Remove AP calls-to-action embedded as legacy body paragraphs."""
    for node in list(soup.select("[data-ap-readmore]")):
        node.decompose()
    for button in list(soup.select("button")):
        button.decompose()

    patterns = (
        re.compile(
            r"(?i)\bsign up for (?:the )?ap(?:'s|’s) .*newsletter\b"
        ),
        re.compile(
            r"(?i)^sign up for .{0,120}\bnewsletter\b.{0,120}"
            r"\b(?:the )?ap(?:'s|’s)\b"
        ),
        re.compile(
            r"(?i)^for more lottery results,\s*go to jackpot\.com\b"
        ),
    )
    for row in list(soup.select("tr")):
        cells = [
            _clean_text(cell.get_text(" ", strip=True))
            for cell in row.select(":scope > th, :scope > td")
        ]
        if cells and all(
            not text
            or re.fullmatch(r"[_=—–-]+", text)
            or re.fullmatch(r"[•·]{2,}", text)
            for text in cells
        ):
            row.decompose()
    for table in list(soup.select("table")):
        if not _clean_text(table.get_text(" ", strip=True)):
            table.decompose()

    for node in list(soup.select("p")):
        text = _clean_text(node.get_text(" ", strip=True))
        if (
            text == "."
            or re.fullmatch(r"[_=—–-]+", text)
            or re.fullmatch(r"[•·]{2,}", text)
            or text in {"<", ">"}
        ):
            node.decompose()
            continue
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
    for node in soup.select("p"):
        text = _clean_text(node.get_text(" ", strip=True))
        if re.fullmatch(r"<\^{10,}", text):
            markers.append(node)
            continue
        if (
            text.casefold() != "read more:"
        ):
            continue
        following_paragraphs = [
            sibling
            for sibling in node.find_next_siblings("p")
            if _clean_text(sibling.get_text(" ", strip=True))
        ]
        if len(following_paragraphs) >= 2:
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


def _normalize_reuters_legacy_press_release_media(
    soup: BeautifulSoup,
) -> None:
    """Restore Business Wire media nested inside one legacy body paragraph."""
    for media in list(soup.select("p > #bwbodyimg:has(img)")):
        paragraph = media.parent
        if not isinstance(paragraph, Tag) or paragraph.name != "p":
            continue

        before = BeautifulSoup("<p></p>", "html.parser").p
        after = BeautifulSoup("<p></p>", "html.parser").p
        if not isinstance(before, Tag) or not isinstance(after, Tag):
            continue

        before_nodes = list(media.previous_siblings)
        after_nodes = list(media.next_siblings)
        for node in before_nodes:
            before.append(node.extract())
        for node in after_nodes:
            after.append(node.extract())

        media.extract()
        media.name = "figure"
        caption = media.find("p")
        if isinstance(caption, Tag):
            caption.name = "figcaption"
            caption_text = _clean_text(caption.get_text(" ", strip=True))
            parenthetical_credit = re.fullmatch(
                r"(.+?)\s*\(((?:photographer|photo|credit|"
                r"illustration|graphic)s?\s*:\s*.+)\)",
                caption_text,
                flags=re.IGNORECASE,
            )
            if parenthetical_credit is not None:
                caption.string = (
                    f"{parenthetical_credit.group(1)}\n"
                    f"{parenthetical_credit.group(2)}"
                )

        if _clean_text(before.get_text(" ", strip=True)):
            paragraph.insert_before(before)
        paragraph.insert_before(media)
        if _clean_text(after.get_text(" ", strip=True)):
            paragraph.insert_before(after)
        paragraph.decompose()


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
            bloomberg_embed = (
                node.select_one("a.bbg-embed[href]")
                if spec.publisher == "bloomberg" and name == "p"
                else None
            )
            if isinstance(bloomberg_embed, Tag) and text == _clean_text(
                bloomberg_embed.get_text(" ", strip=True)
            ):
                source = _normalized_url(
                    bloomberg_embed.get("href"),
                    base_url=base_url,
                )
                if source:
                    blocks.append(
                        ContentBlock(
                            type=BlockType.EMBED,
                            position=position,
                            embed_url=source,
                            html=str(node),
                        )
                    )
                    continue
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
        "table",
        "iframe",
    }
    parent = node.parent
    while isinstance(parent, Tag) and parent is not body:
        if (
            parent.name == "figure"
            and parent.select_one("img") is None
        ):
            # Modern scrollytelling packages use <figure> as a layout shell
            # around narrative paragraphs rather than as an image container.
            parent = parent.parent
            continue
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


def _wsj_is_editorial_letter(soup: BeautifulSoup) -> bool:
    """Identify intentionally short WSJ letters without relaxing news gates."""
    values = (
        _meta_content(soup, "name", "article.type"),
        _meta_content(soup, "name", "article.type.display"),
        _meta_content(soup, "name", "article.page"),
    )
    return any(
        value and _clean_text(value).casefold() == "letters"
        for value in values
    )


def _deduplicate_blocks(
    blocks: list[ContentBlock],
    *,
    deduplicate_contained_pull_quotes: bool = False,
) -> list[ContentBlock]:
    contained_pull_quotes: set[int] = set()
    textual_types = {
        BlockType.PARAGRAPH,
        BlockType.QUOTE,
    }
    pull_quote_candidates = (
        enumerate(blocks) if deduplicate_contained_pull_quotes else ()
    )
    for index, block in pull_quote_candidates:
        if block.type not in textual_types or not block.text:
            continue
        normalized = _normalize_block_text(block.text)
        if len(normalized) < 60:
            continue
        decorative_paragraph = (
            block.type == BlockType.PARAGRAPH
            and not normalized.rstrip().endswith((".", "?", "!", "”", '"'))
        )
        if block.type != BlockType.QUOTE and not decorative_paragraph:
            continue
        for other_index, other in enumerate(blocks):
            if (
                other_index == index
                or abs(other_index - index) > 3
                or other.type not in textual_types
                or not other.text
            ):
                continue
            other_normalized = _normalize_block_text(other.text)
            if (
                len(other_normalized) > len(normalized)
                and normalized in other_normalized
            ):
                contained_pull_quotes.add(index)
                break
    seen_text: set[str] = set()
    seen_assets: set[str] = set()
    unique: list[ContentBlock] = []
    for index, block in enumerate(blocks):
        if index in contained_pull_quotes:
            continue
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
    if spec.publisher == "nyt":
        caption, credit = _nyt_caption_credit(caption_container)
    elif spec.publisher == "bloomberg":
        caption, credit = _bloomberg_caption_credit(caption_container)
        if (
            caption
            and _clean_text(caption).casefold() == "olympus digital camera"
        ):
            caption = None
            caption_node = caption_container.select_one(
                "figcaption, [class*='caption' i]"
            )
            if isinstance(caption_node, Tag):
                caption_node.decompose()
        if alt and _clean_text(alt).casefold() == "olympus digital camera":
            alt = None
            image_node.attrs.pop("alt", None)
    else:
        caption, credit = _caption_credit(caption_container)
    if spec.publisher == "reuters" and caption:
        caption = re.sub(
            r"(?i)\s*purchase\s+licensing\s+rights\s*,?\s*"
            r"opens\s+new\s+tab\s*$",
            "",
            caption,
        ).rstrip() or None
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
    elif width is not None and height is not None and max(width, height) <= 64:
        role = ImageRole.ICON
        reasons.append("small-dimensions")
    elif _GRAPHIC_RE.search(context):
        role = (
            ImageRole.INFOGRAPHIC
            if re.search(r"(?i)infographic", context)
            else ImageRole.CHART
        )
        reasons.append("graphic-context")
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
    if spec.publisher == "nyt" and _nyt_author_avatar_image(url):
        role = ImageRole.AUTHOR_AVATAR
        reasons = [*reasons, "author-avatar-url"]
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
    if host == "dims.apnews.com":
        nested_match = re.search(
            r"(?:^|&)url=([^&]+)",
            parts.query,
            flags=re.IGNORECASE,
        )
        if nested_match is not None:
            nested = unquote(nested_match.group(1))
            nested_parts = urlsplit(nested)
            if (
                nested_parts.scheme in {"http", "https"}
                and nested_parts.netloc
            ):
                return _image_identity(nested)
    if host in {"ft.com", "www.ft.com"} and "/images/raw/" in parts.path:
        nested = unquote(parts.path.split("/images/raw/", 1)[1])
        for _ in range(4):
            nested_parts = urlsplit(nested)
            if "/images/raw/" not in nested_parts.path:
                break
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
    if host == "img.ksl.com":
        return urlunsplit(
            (
                parts.scheme.casefold(),
                parts.netloc.casefold(),
                parts.path,
                "",
                "",
            )
        )
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


def _nyt_author_avatar_image(url: str) -> bool:
    parts = urlsplit(url)
    if (parts.hostname or "").casefold() != "static01.nyt.com":
        return False
    return bool(
        re.search(
            r"/(?:author-[^/]+|author-head-[^/]+)/"
            r"[^/]*(?:thumb(?:large|standard)|author-head)[^/]*"
            r"\.(?:avif|gif|jpe?g|png|webp)$",
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


def _ft_missing_legacy_visual(soup: BeautifulSoup) -> bool:
    """Detect migrated caption-only FT pages whose visual asset was lost."""
    body = soup.select_one(
        "article .article-body[itemprop='articleBody'], "
        "article .article-body"
    )
    if not isinstance(body, Tag):
        return False
    paragraphs = [
        _clean_text(node.get_text(" ", strip=True))
        for node in body.select("p")
        if "copyright" not in " ".join(
            str(value).casefold()
            for value in node.get("class", [])
        )
        and _clean_text(node.get_text(" ", strip=True))
    ]
    if len(paragraphs) != 1 or len(paragraphs[0]) >= 350:
        return False
    if body.select_one(
        "img[src], amp-img[src], figure, iframe[src], video, "
        "amp-video, amp-brightcove, object, embed"
    ):
        return False
    text = paragraphs[0]
    return bool(
        (
            re.search(r"\([LR]\)", text)
            and re.search(r"\([LR]\)", text[re.search(r"\([LR]\)", text).end():])
        )
        or re.search(
            r"(?i)\b(?:pictured|poses? for (?:a )?photograph|"
            r"photographer\s*:|photo shows?|shakes? hands with)\b",
            text,
        )
    )


def _bloomberg_teaser_shell(soup: BeautifulSoup) -> bool:
    if bool(
        soup.select_one(
            "[class*='teaser-body'], "
            ".body-content[class*='teaser-content']"
        )
    ):
        return True
    marker = (
        "to continue reading this article you must be a bloomberg "
        "professional service subscriber"
    )
    for node in soup.select("p"):
        text = _clean_text(node.get_text(" ", strip=True)).casefold()
        if marker not in text:
            continue
        current: Tag | None = node
        hidden = False
        while isinstance(current, Tag):
            style = _clean_text(str(current.get("style") or "")).casefold()
            if (
                current.has_attr("hidden")
                or str(current.get("aria-hidden") or "").casefold() == "true"
                or re.search(r"(?:^|;)\s*display\s*:\s*none\b", style)
            ):
                hidden = True
                break
            current = current.parent if isinstance(current.parent, Tag) else None
        if not hidden:
            return True
    return False


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
    if "reuters.com" in (urlsplit(base_url).hostname or "").casefold():
        return _promote_reuters_image_candidates(result)
    return result


def _is_placeholder_image_url(url: str) -> bool:
    decoded = unquote(url).casefold()
    path_leaf = urlsplit(decoded).path.rstrip("/").rsplit("/", 1)[-1]
    if path_leaf in {"null", "none", "undefined"}:
        return True
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
            "/images/reuters.jpg",
            "twitter_ms_fdnoir.png",
            "/javelin/images/social-",
            "/javelin/public/images/social-",
            "/lightsaber/_next/static/media/social-",
            "/~assets/social-default.",
            "yahoo_default_logo",
            "yahoo-finance-default-logo",
            "/m/img/social/og-ft-logo",
            "/__assets/creatives/open-graph/ft-v1.jpg",
            "/img/meta/wsj-social-share.",
            "/img/wsj_logo_black_social.",
            "/img/wsj_profile_lg.",
            "/common/imgs/wsjsection.",
            "/img/social/opengraph/ij-social-default-",
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


def _nyt_caption_credit(container: Tag) -> tuple[str | None, str | None]:
    """Separate legacy NYT credit spans from the visible image caption."""
    caption_node = container.select_one("figcaption, [class*='caption' i]")
    if not isinstance(caption_node, Tag):
        return None, None
    copy = BeautifulSoup(str(caption_node), "html.parser").find()
    if not isinstance(copy, Tag):
        return None, None
    for hidden in copy.select(
        "[class*='visuallyHidden' i], .visually-hidden, .sr-only"
    ):
        hidden.decompose()
    credit_parts: list[str] = []
    credit_nodes = list(
        copy.select(
            "[itemprop='copyrightHolder'], [class*='credit' i], "
            "[data-testid='credit']"
        )
    )
    if not credit_nodes:
        caption, credit = _caption_credit(container)
        if (
            caption
            and credit is None
            and len(caption.split()) <= 12
            and re.search(
                r"(?i)(?:\s+for\s+|/)\s*the new york times$",
                caption,
            )
        ):
            return None, caption
        return caption, credit
    for credit_node in credit_nodes:
        credit_text = _clean_text(credit_node.get_text(" ", strip=True))
        if credit_text and credit_text.casefold() != "credit":
            credit_parts.append(credit_text)
        credit_node.decompose()
    caption = _clean_text(copy.get_text(" ", strip=True)) or None
    credit = _dedupe_lines("\n".join(credit_parts)) or None
    return caption, credit


def _bloomberg_caption_credit(
    container: Tag,
) -> tuple[str | None, str | None]:
    """Keep Bloomberg's explicit figure credit out of the caption field."""
    caption_node = container.select_one("figcaption, [class*='caption' i]")
    if not isinstance(caption_node, Tag):
        return None, None
    copy = BeautifulSoup(str(caption_node), "html.parser").find()
    if not isinstance(copy, Tag):
        return None, None
    credit_parts: list[str] = []
    credit_nodes = list(
        copy.select(
            ".news-figure-credit, [class*='credit' i], "
            "[itemprop='copyrightHolder']"
        )
    )
    if not credit_nodes:
        return _caption_credit(container)
    for credit_node in credit_nodes:
        credit_text = _clean_text(credit_node.get_text(" ", strip=True))
        if credit_text:
            credit_parts.append(credit_text)
        credit_node.decompose()
    caption = _clean_text(copy.get_text(" ", strip=True)) or None
    credit = _dedupe_lines("\n".join(credit_parts)) or None
    if (
        caption
        and credit
        and caption.casefold() == credit.casefold()
    ):
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
    if article_type == "LiveBlogPosting" or re.search(r"/live(?:/|$)", url):
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


def _bloomberg_legacy_published_at(soup: BeautifulSoup) -> str | None:
    """Recover the timestamp rendered by Bloomberg's pre-2015 story template."""
    node = soup.select_one("#story_meta .datestamp noscript")
    if node is None:
        return None
    value = node.get_text(" ", strip=True)
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%a %b %d %H:%M:%S GMT %Y")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


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


def _wsj_legacy_published_at(soup: BeautifulSoup) -> str | None:
    """Read publication dates serialized by WSJ's pre-Oak templates."""
    for script in soup.select("script"):
        value = script.string or script.get_text()
        match = re.search(
            r"""(?:publicationDate\s*:\s*|"""
            r"""setMetaData\(\s*["']apublished["']\s*,\s*)"""
            r"""["'](?P<date>\d{4}-\d{2}-\d{2}"""
            r"""(?:T\d{2}:\d{2}(?::\d{2})?)?)["']""",
            value,
        )
        if match:
            return match.group("date")
    return None


def _wsj_legacy_headline(soup: BeautifulSoup) -> str | None:
    """Read headlines serialized by WSJ's legacy video templates."""
    for script in soup.select("script"):
        value = script.string or script.get_text()
        match = re.search(
            r"""(?:articleHeadline|clickTitle)\s*:\s*"""
            r"""(?P<quote>["'])(?P<headline>.+?)(?P=quote)"""
            r"""(?=\s*[,}])""",
            value,
        )
        if match:
            headline = _clean_text(match.group("headline"))
            headline = re.sub(r"(?i)^wsj\.com\s*-\s*", "", headline)
            if headline:
                return headline
    title = _tag_text(soup.select_one("head > title"))
    if title:
        title = re.sub(r"(?i)\s*-\s*wsj\.com\s*$", "", title).strip()
        if title and title.casefold() not in {
            "the wall street journal",
            "wsj",
            "wsj.com",
        }:
            return title
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
