from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import quote
import warnings

import pytest

from jojo_olds_api.news_models import (
    ArticleStatus,
    BlobReference,
    CaptureCandidate,
    CaptureProvider,
    ContentType,
    ImageRole,
    RawCapture,
)
from jojo_olds_api.news_parser import parse_article


CASES = [
    (
        "ap",
        "https://apnews.com/article/example",
        "<div data-key='article'>",
        "</div>",
        "https://dims.apnews.com/dims4/default/example.jpg",
    ),
    (
        "wsj",
        "https://www.wsj.com/articles/example",
        "<div data-type='article-body'>",
        "</div>",
        "https://images.wsj.net/im-12345",
    ),
    (
        "bloomberg",
        "https://www.bloomberg.com/news/articles/2020-01-01/example",
        "<div class='body-copy-v2'>",
        "</div>",
        "https://assets.bwbx.io/images/users/example/v1/1200x800.jpg",
    ),
    (
        "nyt",
        "https://www.nytimes.com/2020/01/01/world/example.html",
        "<section name='articleBody'>",
        "</section>",
        "https://static01.nyt.com/images/2020/01/01/example.jpg",
    ),
    (
        "reuters",
        "https://www.reuters.com/world/example-2020-01-01/",
        "<div data-testid='article-body'>",
        "</div>",
        (
            "https://cloudfront-us-east-2.images.arcpublishing.com/"
            "reuters/example.jpg"
        ),
    ),
    (
        "ft",
        "https://www.ft.com/content/example",
        "<div class='article__content-body'>",
        "</div>",
        "https://d1e00ek4ebabms.cloudfront.net/example.jpg",
    ),
    (
        "axios",
        "https://www.axios.com/2017/12/10/example-1512927854",
        "<main id='main-content'>",
        "</div>",
        "https://images.axios.com/example.jpg",
    ),
]


def raw_capture(publisher: str, canonical_url: str) -> RawCapture:
    return RawCapture(
        article_id=f"{publisher}:" + ("a" * 64),
        publisher=publisher,
        canonical_url=canonical_url,
        published_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        section="world",
        selected_candidate=CaptureCandidate(
            provider=CaptureProvider.WAYBACK,
            snapshot_url=(
                "https://web.archive.org/web/20200102000000id_/"
                + canonical_url
            ),
            captured_at=datetime(2020, 1, 2, tzinfo=timezone.utc),
            mime_type="text/html",
            status_code=200,
        ),
        retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        final_url=canonical_url,
        http_status=200,
        content_type="text/html",
        quality_score=100,
        raw_html=BlobReference(
            path="objects/html/aa/example.html.gz",
            sha256="b" * 64,
            byte_count=10_000,
            stored_byte_count=3_000,
            content_encoding="gzip",
        ),
    )


@pytest.mark.parametrize(
    "publisher,canonical_url,body_open,body_close,image_url",
    CASES,
)
def test_six_publishers_emit_jojo_article_v1(
    publisher: str,
    canonical_url: str,
    body_open: str,
    body_close: str,
    image_url: str,
):
    html = f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta property="og:image" content="{image_url}">
        <script type="application/ld+json">
        {{
          "@context": "https://schema.org",
          "@type": "NewsArticle",
          "headline": "A complete archived headline",
          "description": "A useful description.",
          "author": [{{"name": "Reporter One"}}, {{"name": "Reporter Two"}}],
          "datePublished": "2020-01-01T03:04:05Z",
          "dateModified": "2020-01-01T04:05:06Z",
          "articleSection": "World",
          "image": "{image_url}"
        }}
        </script>
      </head>
      <body>
        {body_open}
          <p>First paragraph contains enough meaningful reporting to test the
          normalized article output across all configured publishers.</p>
          <div class="advertisement">
            <img src="https://ads.example.test/banner.jpg" width="300" height="250">
          </div>
          <h2>Context</h2>
              <p>Second paragraph adds more reporting, context, and detail so the
              quality evaluator marks this article as a complete extraction rather
              than a short or unsupported page.</p>
              <p>Third paragraph preserves additional evidence from named sources,
              explains the chronology, records the response from the people
              involved, and supplies enough independent context to distinguish a
              complete report from a metered preview containing only its opening
              lines.</p>
          <figure>
            <img src="{image_url}" width="1200" height="800" alt="Editorial photo">
            <figcaption>
              A descriptive caption.
              Photographer: Example Person
              Photographer: Example Person
            </figcaption>
          </figure>
        {body_close}
      </body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher=publisher,
        canonical_url=canonical_url,
        raw_capture=raw_capture(publisher, canonical_url),
        parsed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert result.format_version == "jojo-article/1"
    assert result.publisher == publisher
    assert result.canonical_url == canonical_url
    assert result.headline == "A complete archived headline"
    assert result.section == "World"
    assert [author.name for author in result.authors] == [
        "Reporter One",
        "Reporter Two",
    ]
    assert "First paragraph" in result.plain_text
    assert "advertisement" not in result.body_html
    assert result.quality.status.value == "complete"
    assert result.quality.body_characters >= 200
    assert len(result.images) == 1
    assert result.images[0].role == ImageRole.LEAD
    assert result.images[0].should_archive is True
    assert result.images[0].caption == "A descriptive caption."
    assert result.images[0].credit == "Photographer: Example Person"
    assert result.quality.images_selected == 1
    assert result.source_capture.raw_html is not None


def test_axios_iframe_only_player_is_preserved_as_video():
    canonical_url = (
        "https://www.axios.com/2017/12/15/"
        "bob-gates-leadership-advice-1513300657"
    )
    html = b"""
    <html><head>
      <script type="application/ld+json">{
        "@type":"NewsArticle",
        "headline":"Bob Gates' leadership advice",
        "datePublished":"2017-12-15T12:00:00Z"
      }</script>
    </head><body><main id="main-content">
      <div class="DraftjsBlocks_draftjs__example">
        <iframe data-src="https://content.jwplatform.com/players/example.html"
                title="Axios video"></iframe>
        <p>WATCH: More from Smarter Faster</p>
      </div>
    </main></body></html>
    """

    result = parse_article(
        html,
        publisher="axios",
        canonical_url=canonical_url,
        raw_capture=raw_capture("axios", canonical_url),
    )

    assert result.content_type.value == "video"
    assert result.quality.status.value == "complete"
    assert "body-too-short" not in result.quality.warnings
    assert any(block.type.value == "embed" for block in result.blocks)


def test_axios_visual_fallback_is_classified_as_interactive():
    canonical_url = "https://www.axios.com/2017/12/15/example-chart"
    html = b"""
    <html><head><script type="application/ld+json">{
      "@type":"NewsArticle", "headline":"A chart",
      "datePublished":"2017-12-15T12:00:00Z"
    }</script></head><body><main id="main-content">
      <div class="DraftjsBlocks_draftjs__example">
        <figure class="axios-visual-apple-fallback-image"><svg></svg></figure>
        <p>Data: Example; Chart: Axios</p>
      </div>
    </main></body></html>
    """

    result = parse_article(
        html,
        publisher="axios",
        canonical_url=canonical_url,
        raw_capture=raw_capture("axios", canonical_url),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"


def test_axios_legacy_short_news_card_is_not_treated_as_truncated():
    canonical_url = "https://www.axios.com/2017/01/21/example-card"
    html = b"""
    <html><head><script type="application/ld+json">{
      "@type":"NewsArticle", "headline":"A short update",
      "datePublished":"2017-01-21T12:00:00Z"
    }</script></head><body><main id="main-content">
      <h1>A short update</h1><p>Source: Example</p>
      <a>Axios on facebook</a><a>Axios on facebook</a>
      <a>Go deeper</a>
    </main></body></html>
    """

    result = parse_article(
        html,
        publisher="axios",
        canonical_url=canonical_url,
        raw_capture=raw_capture("axios", canonical_url),
    )

    assert result.quality.status.value == "complete"
    assert "structured-short-record" in result.quality.warnings


def test_wsj_parser_extracts_structured_image_gallery_in_order():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:image"
            content="https://images.wsj.net/im-266300/social">
      <script type="application/ld+json">
      [
        {
          "@type": "NewsArticle",
          "headline": "A Look at High-End Pantries",
          "datePublished": "2020-12-02T20:18:00Z",
          "description": "Homeowners who made the most of their spaces",
          "image": [
            "https://images.wsj.net/im-266300?width=1280&size=1",
            "https://images.wsj.net/im-266300?width=1280&size=1.33333333",
            "https://images.wsj.net/im-266300?width=1280&size=1.77777778"
          ]
        },
        {
          "@type": "ImageGallery",
          "associatedMedia": [
            {
              "@type": "ImageObject",
              "caption": "First pantry.",
              "contentUrl": "https://images.wsj.net/im-266300/"
            },
            {
              "@type": "ImageObject",
              "caption": "Second pantry.",
              "contentUrl": "https://images.wsj.net/im-266301/"
            },
            {
              "@type": "ImageObject",
              "caption": "Third pantry.",
              "contentUrl": "https://images.wsj.net/im-266302/"
            }
          ]
        }
      ]
      </script>
    </head><body><article>
      <h1>A Look at High-End Pantries</h1>
      <p>Homeowners who made the most of their spaces</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "a-look-at-high-end-pantries-11606940328"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert result.quality.body_characters < 100
    assert result.quality.images_referenced == 3
    assert result.quality.images_selected == 3
    assert [image.original_url for image in result.images] == [
        "https://images.wsj.net/im-266300?width=1280&size=1",
        "https://images.wsj.net/im-266301/",
        "https://images.wsj.net/im-266302/",
    ]
    assert len(result.images[0].candidate_urls) == 5
    assert result.plain_text.index("First pantry") < result.plain_text.index(
        "Third pantry"
    )
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_wsj_parser_scopes_tovima_partner_copy_and_removes_promos():
    prose = " ".join(
        f"Paragraph {index} contains licensed Wall Street Journal "
        "reporting with concrete facts and sufficient original detail."
        for index in range(8)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Licensed WSJ Report">
      <meta property="og:url"
            content="https://www.tovima.com/wsj/a-licensed-wsj-report/">
      <meta property="og:image"
            content="https://www.tovima.com/uploads/report.jpg">
      <meta property="article:published_time"
            content="2026-05-06T10:30:00Z">
    </head><body><main><article>
      <div class="post-body main-content article-wrapper">
        <p>{prose}</p>
        <div id="newsletter-home" class="newsletter-home">
          <p>NEWSLETTER TABLE TALK Never miss a story. Subscribe now.</p>
        </div>
        <div class="googlenews"><p>Follow tovima.com on Google News.</p></div>
        <p><img src="https://www.tovima.com/uploads/report.jpg"></p>
      </div>
      <div class="vima-box single__related">
        <h4>Related Articles</h4>
        <p>This unrelated current story must not enter the archive body.</p>
        <img src="https://www.wsj.com/wp-content/themes/whsk_tovima.com/"
             alt="WSJ section">
      </div>
    </article></main></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/world/a-licensed-wsj-report",
    )

    assert result.quality.status.value == "complete"
    assert "Paragraph 7 contains licensed" in result.plain_text
    assert "Never miss a story" not in result.plain_text
    assert "unrelated current story" not in result.plain_text
    assert result.quality.images_selected == 1
    assert result.images[0].original_url.endswith("/report.jpg")


def test_wsj_parser_deduplicates_legacy_renditions_and_branding():
    html = b"""
    <html><head>
      <meta property="og:title" content="Legacy Image Renditions">
      <meta property="article:published_time"
            content="2014-12-21T00:00:00Z">
      <meta property="og:image"
            content="http://si.wsj.net/public/resources/images/BN-GC782_YATES_G_20141221165350.jpg">
      <meta name="twitter:image"
            content="http://si.wsj.net/public/resources/images/BN-GC782_YATES_D_20141221165350.jpg">
    </head><body><article>
      <p>This complete legacy report contains enough substantive text to
      validate image rendition deduplication without relying on a shell. It
      preserves several paragraphs of reporting, named-source responses,
      chronology, supporting evidence and historical context so that it cannot
      be mistaken for a metered preview. The report also explains the outcome,
      records the relevant figures and describes why the development matters
      to readers. Further verified details complete the archived account and
      keep the body comfortably above the conservative WSJ preview boundary.</p>
      <img src="http://si.wsj.net/img/WSJ_Logo_black_social.gif">
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/articles/legacy-image-renditions",
    )

    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 1
    assert len(result.images[0].candidate_urls) == 2


def test_wsj_parser_classifies_embedded_acrostic_as_interactive():
    html = b"""
    <html><head>
      <script type="application/ld+json">
      {
        "@type": "NewsArticle",
        "headline": "The Journal Acrostic",
        "articleSection": "WSJ Puzzles",
        "datePublished": "2020-12-18T21:01:00Z"
      }
      </script>
    </head><body>
      <article>
        <div data-type="article-body">
          <div class="interactive-puzzle-template">
            <iframe class="acrostic-puzzle-frame"
                    src="https://example.com/puzzle/index.html?embed=1">
            </iframe>
          </div>
          <p style="position:absolute;left:-15000px">
            Copyright tracking marker
          </p>
        </div>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "the-journal-acrostic-saturday-example"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.blocks[0].type.value == "embed"
    assert result.blocks[0].embed_url == (
        "https://example.com/puzzle/index.html?embed=1"
    )
    assert result.plain_text == ""


def test_wsj_parser_preserves_downloadable_puzzle_pdfs():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Labyrinth (Saturday Variety Puzzle, August 6)">
      <meta property="article:published_time"
            content="2022-08-06T09:00:00Z">
    </head><body><article>
      <nav>WSJ Puzzles Variety Puzzle</nav>
      <a href="https://s.wsj.net/public/resources/documents/SatPuz.pdf">
        Download PDF
      </a>
      <a href="https://s.wsj.net/public/resources/documents/Answer.pdf">
        Solutions
      </a>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "labyrinth-saturday-variety-puzzle-11659568300"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "interactive"
    assert [block.embed_url for block in result.blocks if block.embed_url] == [
        "https://s.wsj.net/public/resources/documents/SatPuz.pdf",
        "https://s.wsj.net/public/resources/documents/Answer.pdf",
    ]
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_wsj_parser_extracts_amp_story_photo_gallery():
    pages = "".join(
        f"""
        <amp-story-page id="image-{index}">
          <amp-story-grid-layer template="fill">
            <amp-img media="(orientation:portrait)"
                     src="https://images.wsj.net/im-{700 + index}/portrait?pixel_ratio=2">
            </amp-img>
            <amp-img media="(orientation:landscape)"
                     src="https://images.wsj.net/im-{700 + index}?width=1920">
            </amp-img>
          </amp-story-grid-layer>
          <p class="wsj--caption">Historical photograph {index} caption.</p>
          <p class="wsj--credit">Archive {index}</p>
        </amp-story-page>
        """
        for index in range(3)
    )
    structured_images = ", ".join(
        f'{{"@type":"ImageObject","url":"https://images.wsj.net/im-{700 + index}"}}'
        for index in range(3)
    )
    html = f"""
    <html><head>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "A Life in Photos",
        "datePublished": "2024-12-29T21:31:52Z",
        "image": [{structured_images}]
      }}
      </script>
    </head><body>
      <amp-story>{pages}</amp-story>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/story/a-life-in-photos-example",
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert len(result.blocks) == 3
    assert len(result.images) == 3
    assert result.images[1].caption == "Historical photograph 1 caption."
    assert result.images[1].credit == "Credit: Archive 1"


def test_wsj_parser_extracts_legacy_slideshow_photo_gallery():
    slides = "".join(
        f"""
        <div class="slide-wrapper" data-credit="Archive Photographer {index}">
          <img src="http://si.wsj.net/public/resources/images/photo-{index}.jpg"
               alt="image">
          <div class="caption-wrapper"><p>
            Historical photograph {index} caption.
            <span>Archive Photographer {index}</span>
          </p></div>
          <div class="slidesjs-log">{index + 1} of 4</div>
        </div>
        """
        for index in range(4)
    )
    html = f"""
    <html><head>
      <title>A Backstage Look at the Production - WSJ</title>
      <meta property="og:title"
            content="A Backstage Look at the Production">
      <meta property="article:published_time"
            content="2014-12-23T18:11:07Z">
    </head><body>
      <div class="dj-slideshow">{slides}</div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "a-backstage-look-at-the-production-1419363067"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert len(result.blocks) == 4
    assert len(result.images) == 4
    assert result.images[0].caption == "Historical photograph 0 caption."
    assert result.images[0].credit == "Credit: Archive Photographer 0"
    assert result.plain_text.count("Archive Photographer 0") == 1
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_wsj_parser_rejects_modern_metered_preview_and_removes_ui():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="The Trouble With Crowdfunding Campaigns">
      <meta property="article:published_time"
            content="2025-01-19T11:36:44Z">
    </head><body><article>
      <p class="css-1to03ck">Listen</p>
      <p class="css-mb1725">(1 min)</p>
      <p data-type="paragraph">Touched by personal stories of anguish and
      loss, donors sent millions of dollars directly to families.</p>
      <p data-type="paragraph">These competing pleas for generosity have
      uneven results.</p>
      <p class="css-16aepit">Copyright \xc2\xa9 2025 Dow Jones &amp;
      Company, Inc. All Rights Reserved. archive-token</p>
      <h2 class="css-1imb987-SectionLabel">Videos</h2>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/personal-finance/"
            "crowdfunding-example"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "Copyright" not in result.plain_text
    assert "Videos" not in result.plain_text
    assert "Listen" not in result.plain_text


def test_wsj_parser_accepts_complete_short_report_matching_declared_words():
    first = " ".join(["Northrop completed the defense system test."] * 10)
    second = " ".join(["The two missiles were intercepted safely."] * 10)
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Defense System Intercepts Two Missiles">
      <meta property="article:published_time"
            content="2019-12-12T19:55:00Z">
      <meta property="article:word_count" content="120">
    </head><body><article>
      <div itemprop="articleBody">
        <p>{first}</p>
        <div class="paywall"><p>{second}</p></div>
        <p>Copyright © 2019 Dow Jones &amp; Company, Inc.
        All Rights Reserved.</p>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "defense-system-intercepts-two-missiles-11576180509"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "truncated-body" not in result.quality.warnings
    assert "Northrop completed" in result.plain_text
    assert "The two missiles" in result.plain_text
    assert "Copyright" not in result.plain_text
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_wsj_parser_does_not_treat_deliver_in_url_as_liveblog():
    html = b"""
    <html><head>
      <meta property="og:title" content="Could a Recession Be Years Away?">
      <meta property="article:published_time"
            content="2023-07-27T11:00:00Z">
    </head><body><article>
      <p data-type="paragraph">
        Economic expansions do not die of old age, economists like to say;
        they are murdered by the Federal Reserve.
      </p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "soft-landing-could-deliver-another-economic-cycle-e7e2c4ee"
        ),
    )

    assert result.content_type.value == "article"
    assert result.quality.status.value == "partial"
    assert "body-too-short" in result.quality.warnings
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_wsj_parser_does_not_treat_facebook_live_story_as_liveblog():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Network Planning Five Facebook Live Shows">
      <meta property="article:published_time"
            content="2016-08-03T11:00:00Z">
    </head><body><article>
      <p>Network executives announced a new slate of online programs.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "network-planning-five-facebook-live-shows-1470222001"
        ),
    )

    assert result.content_type.value == "article"
    assert result.quality.status.value == "partial"
    assert "body-too-short" in result.quality.warnings
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_wsj_parser_rejects_legacy_sign_in_snippet():
    html = b"""
    <html><head>
      <meta property="og:title" content="A Brownstone Changes Hands">
      <meta property="article:published_time"
            content="2022-09-07T21:13:00Z">
    </head><body><article>
      <p>The buyer acquired a Brooklyn townhouse for $18.3 million,
      according to property records.</p>
      <p class="SnippetSignInText">Already a member?
        <a>Sign In</a>
      </p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/articles/example-brownstone",
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "Already a member" not in result.plain_text


def test_wsj_parser_keeps_substantial_body_and_strips_copyright_footer():
    reporting = " ".join(
        ["Substantive reporting with named sources and verified context."]
        * 30
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Complete Modern Report">
      <meta property="article:published_time"
            content="2025-02-12T17:36:28Z">
    </head><body><article>
      <p data-type="paragraph">{reporting}</p>
      <p class="css-16aepit">Copyright \u00a9 2025 Dow Jones &amp;
      Company, Inc. All Rights Reserved. archive-token</p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/tech/complete-modern-report",
    )

    assert result.quality.status.value == "complete"
    assert "Substantive reporting" in result.plain_text
    assert "Copyright" not in result.plain_text


def test_wsj_parser_accepts_short_dow_jones_newswire_record():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Investment Manager Reports New Stake">
      <meta property="article:published_time"
            content="2022-08-12T20:22:00Z">
      <meta name="article.type.display" content="Dow Jones Newswires">
      <script type="application/ld+json">
        {"@type":"NewsArticle","headline":"Investment Manager Reports New Stake",
        "articleSection":"T Wire",
        "datePublished":"2022-08-12T20:22:00Z"}
      </script>
    </head><body><article>
      <p>By Example Reporter</p>
      <p>The investment manager on Friday reported a 21.8% stake in the
      manufacturer, according to a regulatory filing.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/articles/new-stake-example",
    )

    assert result.quality.status.value == "complete"
    assert "structured-short-record" in result.quality.warnings


def test_wsj_parser_rejects_unmarked_short_standard_article_preview():
    html = b"""
    <html><head>
      <meta property="og:title" content="A Historic Home Goes on Sale">
      <meta property="article:published_time"
            content="2022-07-20T19:18:19Z">
    </head><body><article>
      <p>Selling an old family home can be hard.</p>
      <p>Try selling a historically significant house that has been in the
      family for over 300 years and comes with preservation restrictions.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/articles/historic-home-example",
    )

    assert result.quality.status.value == "partial"
    assert "body-too-short" in result.quality.warnings


def test_wsj_parser_classifies_legacy_slideshow_metadata_as_gallery():
    html = b"""
    <html><head>
      <meta name="page.content.type" content="slideshow">
      <meta property="og:title" content="Ebola Diagnosis in New York">
      <meta property="article:published_time"
            content="2014-10-24T00:00:00Z">
      <meta name="description"
            content="A doctor who returned after treating Ebola patients
                     tested positive for the virus in New York City.">
    </head><body>
      <article><p>
        A doctor who returned after treating Ebola patients tested positive
        for the virus in New York City, prompting a public-health response.
      </p></article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "ebola-diagnosis-in-new-york-1414123908"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"


def test_wsj_parser_removes_legacy_article_tools_and_trending_modules():
    reporting = " ".join(["Historical reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="A Health-Care Defeat">
      <meta property="article:published_time"
            content="2017-04-02T00:00:00Z">
    </head><body>
      <article>
        <div class="wsj-article-headline-wrap">
          <span class="article-breadCrumb-wrapper">
            <ul><li>Opinion</li><li>Commentary</li></ul>
          </span>
        </div>
        <div class="article-content">
          <p>{reporting}</p>
        </div>
        <div id="article_tools">
          <ul><li>Save Article</li><li>Subscribe to WSJ</li></ul>
        </div>
        <div id="trending_now">
          <h2>Most Popular Videos</h2>
          <ul><li>Unrelated video promotion</li></ul>
          <h2>Most Popular Articles</h2>
          <ul><li>Unrelated article promotion</li></ul>
        </div>
      </article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/a-health-care-defeat-1491149327"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Historical reporting sentence." in result.plain_text
    assert "Opinion Commentary" not in result.plain_text
    assert "Save Article" not in result.plain_text
    assert "Most Popular" not in result.plain_text
    assert "Unrelated" not in result.plain_text


def test_wsj_parser_removes_legacy_share_comments_and_journal_reports():
    reporting = " ".join(["Core reporting remains intact."] * 25)
    html = f"""
    <html><head>
      <meta property="og:title" content="Legacy Page Furniture">
      <meta property="article:published_time" content="2018-10-22T00:00:00Z">
    </head><body><article>
      <div class="article-content"><p>{reporting}</p></div>
      <ul class="article_tools"><li>Text Size Regular Medium Large</li>
        <li>Save Article Log In to Save Subscribe to WSJ</li></ul>
      <div id="livefyre-wrapper"><p>Get Livefyre</p><p>FAQ</p></div>
      <div class="module jr-module"><p>College Rankings</p>
        <p>Wealth Management</p></div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/articles/legacy-furniture-1540174021",
    )

    assert result.quality.status.value == "complete"
    assert "Core reporting remains intact." in result.plain_text
    assert "Text Size" not in result.plain_text
    assert "Livefyre" not in result.plain_text
    assert "College Rankings" not in result.plain_text


def test_wsj_parser_extracts_imageobject_legacy_slideshow():
    slides = "".join(
        f"""
        <div class="wsj-slideshow-slide" itemtype="http://schema.org/ImageObject">
          <meta itemprop="contentUrl"
                content="https://images.wsj.net/im-{index}.jpg">
          <p itemprop="caption">Historical image {index}.</p>
          <span itemprop="copyrightHolder">Photographer {index}</span>
        </div>
        """
        for index in range(3)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Buick Show Stopper">
      <meta property="article:published_time" content="2014-12-16T00:00:00Z">
    </head><body><article>
      {slides}
      <div class="wsj-slideshow-slide explore-more-slide">
        <p>More Slideshows</p><p>Unrelated promotion</p>
      </div>
      <div id="livefyre-wrapper"><p>Get Livefyre</p><p>FAQ</p></div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/articles/a-buick-show-stopper-1418751477",
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert len(result.images) == 3
    assert "Historical image 0." in result.plain_text
    assert "More Slideshows" not in result.plain_text
    assert "Livefyre" not in result.plain_text


def test_wsj_parser_recovers_description_from_unsupported_slideshow_shell():
    html = b"""
    <html><head>
      <meta property="og:title" content="A Day at Design Boot Camp">
      <meta name="description" content="Boot campers visit furniture showrooms and study interior design during a three-day workshop.">
      <meta property="article:published_time" content="2014-10-24T00:00:00Z">
    </head><body><article>
      <div class="wsj-article-headline-wrap slideshow-article"></div>
      <div class="wsj-snippet-body">
        <p>We're sorry but this article contains media that is not currently supported.</p>
        <p>If you are not redirected automatically, click this link.</p>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/articles/design-boot-camp-1414169304",
    )

    assert result.content_type.value == "gallery"
    assert result.plain_text.startswith("Boot campers visit")
    assert "not redirected automatically" not in result.plain_text


def test_wsj_parser_marks_short_ellipsis_capture_as_partial():
    html = b"""
    <html><head>
      <meta property="og:title" content="Bangladesh Power Restored">
      <meta property="article:published_time" content="2014-11-02T00:00:00Z">
    </head><body><article><div class="article-content">
      <p>Electricity returned to many areas after a nationwide blackout.</p>
      <p>Officials said engineers were still investigating the cause.</p>
      <p>...</p>
    </div></article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/articles/bangladesh-power-1414915894",
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings


@pytest.mark.parametrize(
    "ending",
    [
        "Revenue was expected to reach 39...",
        "The district had no testing requirement,...",
        "The brokerage reported a quarterly loss....",
    ],
)
def test_wsj_parser_marks_legacy_attached_ellipsis_as_partial(ending):
    html = f"""
    <html><head>
      <meta property="og:title" content="A Truncated Legacy Report">
      <meta property="article:published_time"
            content="2016-11-02T00:00:00Z">
    </head><body><article><div class="article-content">
      <p>Officials released an update concerning the company.</p>
      <p>{ending}</p>
    </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "a-truncated-legacy-report-1478044800"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings


def test_wsj_parser_recovers_embedded_inset_data_tables():
    rows = [
        {
            "Title": f"<b>Book Title {number}</b>",
            "Author/Publisher": f"Author {number}/Publisher",
            "This Week": f"<b>{number}</b>",
            "Last Week": "New",
        }
        for number in range(1, 11)
    ]
    payload = json.dumps(
        {
            "headline": "Hardcover Nonfiction",
            "description": "Bestselling Books Week Ended November 21",
            "source": "NPD BookScan",
            "data": rows,
            "settings": {
                "columns": [
                    {"name": "Title"},
                    {"name": "Author/Publisher"},
                    {"name": "This Week"},
                    {"name": "Last Week"},
                ]
            },
        },
        separators=(",", ":"),
    )
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Bestselling Books Week Ended November 21">
      <meta property="article:published_time"
            content="2020-11-26T14:09:00Z">
    </head><body><article>
      <h2>Hardcover Nonfiction</h2>
      <p>NPD BookScan gathers point-of-sale data from thousands of
      booksellers and major online retailers across the United States.</p>
    </article>
    <script>
      var insetData_218577 = function() {{return {payload};}}
    </script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "bestselling-books-week-ended-november-21-11606399776"
        ),
    )

    table_blocks = [
        block for block in result.blocks
        if block.type.value == "table"
    ]
    assert len(table_blocks) == 1
    assert "Book Title 10" in table_blocks[0].text
    assert result.plain_text.count("Hardcover Nonfiction") == 1
    assert "Source: NPD BookScan" in result.plain_text


def test_wsj_parser_marks_subscription_snippet_as_partial():
    html = b"""
    <html><head>
      <meta property="og:title" content="A Closer Look">
      <meta property="article:published_time"
            content="2026-02-11T14:02:00Z">
    </head><body>
      <article>
        <div class="snippet">
          <p>
            Selections from Rumpelstiltskin by Mac Barnett,
            illustrated by Carson Ellis
          </p>
          <div class="snippet-promotion">
            <div id="cx-snippet-overlay">
              <h3 class="snippet-subheadline">
                Subscribe to WSJ to read the rest of this article
              </h3>
              <p>Already a subscriber? Sign In</p>
              <p>Resume Subscription</p>
              <p>Please click confirm to resume now.</p>
            </div>
          </div>
        </div>
        <div class="resume-subscription-scrim-overlay hide">
          <h2>Resume Subscription</h2>
          <p>We are delighted that you'd like to resume your subscription.</p>
          <p>Please click confirm to resume now.</p>
        </div>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/a-closer-look-76125dc1"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "body-too-short" in result.quality.warnings
    assert "Subscribe to WSJ" not in result.plain_text
    assert "Resume Subscription" not in result.plain_text
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_wsj_parser_trims_full_story_roadblock_and_recirculation():
    reporting = " ".join(["State budget reporting sentence."] * 12)
    recirculation = " ".join(["Unrelated popular headline."] * 80)
    html = f"""
    <html><head>
      <meta property="og:title"
            content="States Push Tax Cuts Amid Big Budgets">
      <meta property="article:published_time"
            content="2022-03-04T12:00:00Z">
    </head><body><article>
      <div data-type="article-body">
        <p data-type="paragraph">{reporting}</p>
        <div class="ArticleRoadblock__Container-sc-test">
          <p class="ScenarioStandard__ReadFullStory-sc-test">
            To Read the Full Story
          </p>
        </div>
        <section><h2>Most Popular news</h2><p>{recirculation}</p></section>
        <section><h2>Recommended Videos</h2></section>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "states-push-tax-cuts-amid-big-budgets-11646593068"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "State budget reporting sentence." in result.plain_text
    assert "To Read the Full Story" not in result.plain_text
    assert "Most Popular news" not in result.plain_text
    assert "Recommended Videos" not in result.plain_text
    assert "Unrelated popular headline" not in result.plain_text
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_wsj_parser_removes_legacy_more_in_and_top_news_modules():
    reporting = " ".join(["WSJ reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="A Complete Legacy WSJ Story">
      <meta property="article:published_time"
            content="2016-02-09T12:00:00Z">
    </head><body><article>
      <div class="article-content">
        <div class="byline article__byline">
          <span>By</span>
          <div class="author mobile-scrim hasMenu">
            <span class="name">Example Reporter</span>
            <ul class="author-info">
              <li><a href="/news/author/example">Biography</a></li>
              <li><a href="mailto:example@wsj.com">example@wsj.com</a></li>
            </ul>
          </div>
        </div>
        <div class="css-jb73ai-AuthoringContainer">
          <p>By</p>
          <a data-testid="author-link">Modern Reporter One</a>
          <p>,</p>
          <a data-testid="author-link">Modern Reporter Two</a>
          <p>and</p>
          <a data-testid="author-link">Modern Reporter Three</a>
        </div>
        <ul class="author-info">
          <li><a class="author icon bio">Biography</a></li>
          <li><a class="author icon twitter">@legacyreporter</a></li>
          <li><a class="author icon email">legacy@wsj.com</a></li>
        </ul>
        <p>{reporting}</p>
        <p class="articleTagLine">—</p>
        <p class="articleTagLine">—Example Contributor</p>
        <p>.</p>
        <p>&#8203;</p>
        <div class="media-object inline">
          <div class="media-object-rich-text">
            <h4>More in WSJ. Magazine</h4>
            <ul class="articleList">
              <li><a href="/articles/unrelated-one">Unrelated story one</a>
                <span class="date">Feb. 9, 2016</span></li>
              <li><a href="/articles/unrelated-two">Unrelated story two</a>
                <span class="date">Feb. 8, 2016</span></li>
            </ul>
          </div>
        </div>
        <div class="media-object inline">
          <div class="media-object-rich-text">
            <h4>More Flower School</h4>
            <ul class="articleList">
              <li><a href="/articles/unrelated-flower-school">
                Unrelated Flower School story
              </a></li>
            </ul>
          </div>
        </div>
        <div class="media-object inline">
          <div class="media-object-rich-text">
            <h4>More From WSJ. Magazine</h4>
            <ul class="articleList">
              <li><a href="/articles/unrelated-more-from">
                Unrelated More From story
              </a></li>
            </ul>
          </div>
        </div>
        <div class="zonedModule" data-module-zone="contentCarousel">
          <div class="content-carousel">
            <ul><li>Unrelated carousel story</li></ul>
            <div class="carousel-label"><p>More From Tech</p></div>
          </div>
        </div>
        <div class="module automated-news">
          <h2>Top News</h2>
          <ul class="items hedSumm">
            <li><h3><a href="/articles/unrelated-three">
              Unrelated story three
            </a></h3></li>
          </ul>
        </div>
        <div class="article-news-front" data-block="doNotPrint">
          <h3>What's News</h3>
          <ul>
            <li><a href="/articles/unrelated-news-front">
              Unrelated news-front headline
            </a></li>
          </ul>
        </div>
        <div aria-label="What to Read Next" data-block="doNotPrint">
          <h2>What to Read Next</h2>
          <a href="/articles/unrelated-read-next">
            Unrelated read-next headline
          </a>
        </div>
        <div data-module-zone="opinion_editors_picks" class="zonedModule">
          <div class="trending_articles opinion-editors-picks">
            <div class="strap secondary">
              <h2 class="subhead">Opinion Editor's Picks</h2>
            </div>
            <ul class="clear">
              <li><a href="/articles/unrelated-opinion-pick">
                Unrelated opinion editor's pick
              </a></li>
            </ul>
          </div>
        </div>
        <div class="module editors-picks">
          <h3 class="section">Explore More</h3>
          <ul class="setList">
            <li><a href="/articles/unrelated-explore-more">
              Unrelated Explore More story
            </a></li>
          </ul>
        </div>
        <ul>
          <li class="share-bottom" id="expShareEmailBottom">
            <a href="mailto:?subject=story">EMAIL</a>
          </li>
          <li class="share-bottom" id="expSharePrintBottom">
            <a class="print" href="#print">PRINT</a>
          </li>
        </ul>
        <div class="printSummary pfHeader">
          <ul><li><p><a href="http://www.djreprints.com">
            www.djreprints.com
          </a></p></li></ul>
        </div>
        <div class="strap-container">
          <h4 class="strap" itemprop="description">Related Video</h4>
        </div>
        <div class="media-object-rich-text">
          <h4>SHARE YOUR THOUGHTS</h4>
          <p>Join the conversation below.</p>
        </div>
        <p><em>To explore and search through all our recipes, check out
          the new <a href="/recipes">WSJ Recipes</a> page.</em></p>
        <div class="css-1r9l7hs-JRStrap">
          <h2>Next in Journal Reports: Investing Monthly</h2>
        </div>
        <div class="css-tczrud-JRNextArticleInfoContainer">
          <p>Unrelated next Journal Reports article summary.</p>
        </div>
        <div class="css-1pfm1qc-JRMoreArticlesStrap">
          <h2>More Journal Reports: Investing Monthly Articles</h2>
        </div>
        <div class="magazine-collection">
          <article><h2>Unrelated magazine story one</h2></article>
          <article><h2>Unrelated magazine story two</h2></article>
          <div>
            <a href="/news/tech/future-of-everything" role="button">
              See All
            </a>
          </div>
        </div>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "a-complete-legacy-wsj-story-1455042405"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "WSJ reporting sentence." in result.plain_text
    assert "\u200b" not in result.plain_text
    assert "\n.\n" not in result.plain_text
    assert "\n—\n" not in result.plain_text
    assert "—Example Contributor" in result.plain_text
    assert "More in WSJ. Magazine" not in result.plain_text
    assert "More From WSJ. Magazine" not in result.plain_text
    assert "Unrelated More From story" not in result.plain_text
    assert "More Flower School" not in result.plain_text
    assert "Unrelated Flower School story" not in result.plain_text
    assert "More From Tech" not in result.plain_text
    assert "Unrelated carousel story" not in result.plain_text
    assert "Biography" not in result.plain_text
    assert "example@wsj.com" not in result.plain_text
    assert "Modern Reporter" not in result.plain_text
    assert "legacyreporter" not in result.plain_text
    assert "legacy@wsj.com" not in result.plain_text
    assert "Unrelated story" not in result.plain_text
    assert "Unrelated magazine story" not in result.plain_text
    assert "Unrelated news-front headline" not in result.plain_text
    assert "What's News" not in result.plain_text
    assert "What to Read Next" not in result.plain_text
    assert "Unrelated read-next headline" not in result.plain_text
    assert "Opinion Editor's Picks" not in result.plain_text
    assert "Unrelated opinion editor's pick" not in result.plain_text
    assert "Explore More" not in result.plain_text
    assert "Unrelated Explore More story" not in result.plain_text
    assert "EMAIL" not in result.plain_text
    assert "PRINT" not in result.plain_text
    assert "www.djreprints.com" not in result.plain_text
    assert "Related Video" not in result.plain_text
    assert "SHARE YOUR THOUGHTS" not in result.plain_text
    assert "Join the conversation" not in result.plain_text
    assert "To explore and search" not in result.plain_text
    assert "Next in Journal Reports" not in result.plain_text
    assert "Unrelated next Journal Reports" not in result.plain_text
    assert "More Journal Reports" not in result.plain_text
    assert "See All" not in result.plain_text
    assert "Top News" not in result.plain_text
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_nyt_parser_recovers_legacy_standalone_slideshow_json():
    slides = []
    for index in range(3):
        slides.append(
            {
                "slide_url": f"historical-slide-{index}",
                "caption": {
                    "full": (
                        f"<p>Historical photograph {index} caption.</p>"
                    )
                },
                "credit": f"Archive Photographer {index}",
                "image_crops": {
                    "thumb": {
                        "width": 190,
                        "height": 126,
                        "url": (
                            "https://static01.nyt.com/images/"
                            f"historical-{index}/"
                            f"historical-{index}-thumb.jpg"
                        ),
                    },
                    "jumbo": {
                        "width": 1024,
                        "height": 683,
                        "url": (
                            "https://static01.nyt.com/images/"
                            f"historical-{index}/"
                            f"historical-{index}-jumbo.jpg"
                        ),
                    },
                },
            }
        )
    payload = json.dumps(
        {
            "headline": "Street Style: London",
            "summary": "Fashion week on the streets of London.",
            "imageslideshow": {"slides": slides},
        }
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Street Style: London">
      <meta property="og:image"
            content="https://static01.nyt.com/images/historical-0/historical-0-facebookJumbo.jpg">
      <meta property="article:published_time"
            content="2015-09-21T00:00:00Z">
    </head><body>
      <script type="application/json">{payload}</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2015/09/21/fashion/"
            "street-style-london.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 3
    assert [image.original_url for image in result.images] == [
        (
            "https://static01.nyt.com/images/historical-0/"
            "historical-0-facebookJumbo.jpg"
        ),
        (
            "https://static01.nyt.com/images/historical-1/"
            "historical-1-jumbo.jpg"
        ),
        (
            "https://static01.nyt.com/images/historical-2/"
            "historical-2-jumbo.jpg"
        ),
    ]
    assert result.images[0].caption == "Historical photograph 0 caption."
    assert result.images[0].credit == "Credit: Archive Photographer 0"


def test_nyt_parser_preserves_legacy_lede_video_destination():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="The Enduring Appeal of the Flamboyant Early '70s">
      <meta property="article:published_time"
            content="2015-12-02T00:00:00Z">
    </head><body>
      <article class="story theme-main">
        <div class="story-body">
          <figure class="promo media video lede" data-videoid="100000004">
            <figcaption>
              <h4 class="headline">In The Air | Glam Rock</h4>
              <a class="video-link"
                 href="https://www.nytimes.com/video/t-magazine/100000004/in-the-air-glam-rock.html">
                Watch in Times Video
              </a>
            </figcaption>
          </figure>
          <p class="story-body-text story-content">
            Fashion designers embrace the underground gender-bending
            aesthetic made famous by David Bowie and New York drag queens.
          </p>
        </div>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2015/12/02/t-magazine/fashion/"
            "glam-rock-fashion.html"
        ),
    )

    assert result.content_type.value == "video"
    assert result.quality.status.value == "complete"
    assert "Fashion designers embrace" in result.plain_text
    assert [
        block.embed_url for block in result.blocks if block.embed_url
    ] == [
        (
            "https://www.nytimes.com/video/t-magazine/100000004/"
            "in-the-air-glam-rock.html"
        )
    ]


def test_parser_classifies_non_editorial_images_without_archiving_them():
    canonical_url = "https://apnews.com/article/example"
    html = b"""
    <html><head>
      <script type="application/ld+json">
      {"@type":"NewsArticle","headline":"Headline","datePublished":"2020-01-01T00:00:00Z"}
      </script>
    </head><body>
      <div data-key="article">
        <p>This is a sufficiently long article paragraph with meaningful
        reporting content that continues for more than two hundred characters
        after the second paragraph has also been included in this sample.</p>
        <p>Additional reporting fills out the article and makes this a useful
        parser fixture rather than a short placeholder document.</p>
        <figure class="author-avatar">
          <img src="https://example.com/avatar.png" width="48" height="48">
        </figure>
        <img class="tracking-pixel" src="https://example.com/pixel.gif"
             width="1" height="1">
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=canonical_url,
    )

    roles = {image.role for image in result.images}
    assert roles == {ImageRole.AUTHOR_AVATAR, ImageRole.TRACKING}
    assert all(image.should_archive is False for image in result.images)
    assert result.quality.images_selected == 0


def test_reuters_parser_excludes_legacy_default_images():
    reporting = " ".join(["Reuters reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="Reuters historical report">
      <meta property="article:published_time"
            content="2017-06-01T00:00:00Z">
      <meta property="og:image"
            content="https://s4.reutersmedia.net/resources_v2/images/rcom-default.png">
    </head><body>
      <div data-testid="article-body">
        <img src="https://s4.reutersmedia.net/resources_v2/images/r-generic-hdr.png">
        <p>{reporting}</p>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/article/"
            "historical-report-idUSL1N1EXAMPLE"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 0
    assert [image.original_url for image in result.images] == [
        (
            "https://s4.reutersmedia.net/resources_v2/images/"
            "r-generic-hdr.png"
        )
    ]
    assert result.images[0].role == ImageRole.LOGO
    assert result.images[0].should_archive is False
    assert "generic-publisher-branding" in (
        result.images[0].selection_reasons
    )


def test_bloomberg_parser_excludes_social_default_images():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="Bloomberg historical report">
      <meta property="article:published_time"
            content="2017-06-01T00:00:00Z">
      <meta property="og:image"
            content="~assets/social-default.jpg">
      <meta name="twitter:image"
            content="https://assets.bwbx.io/s3/javelin/public/javelin/images/social-default-a4f15fa7ee.jpg">
    </head><body>
      <div class="body-copy-v2">
        <img src="https://assets.bwbx.io/javelin/public/images/social-markets-3d32d2f713.jpg">
        <p>{reporting}</p>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/"
            "2017-06-01/historical-report"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 0
    assert len(result.images) == 1
    assert result.images[0].role == ImageRole.LOGO
    assert result.images[0].should_archive is False


def test_bloomberg_parser_deduplicates_image_renditions():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    asset_root = (
        "https://assets.bwbx.io/images/users/example/image-id/v2"
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Bloomberg illustrated report">
      <meta property="article:published_time"
            content="2017-06-01T00:00:00Z">
      <meta property="og:image"
            content="{asset_root}/1200x776.jpg">
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "Bloomberg illustrated report",
        "datePublished": "2017-06-01T00:00:00Z",
        "image": "{asset_root}/740x-1.jpg"
      }}
      </script>
    </head><body>
      <div class="body-copy-v2">
        <figure>
          <img src="{asset_root}/100x-1.jpg"
               alt="Editorial photograph">
          <figcaption>Editorial photograph caption.</figcaption>
        </figure>
        <p>{reporting}</p>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/"
            "2017-06-01/illustrated-report"
        ),
    )

    assert result.quality.images_selected == 1
    assert len(result.images) == 1
    assert result.images[0].role == ImageRole.LEAD
    assert result.images[0].caption == "Editorial photograph caption."
    assert result.images[0].candidate_urls == [
        f"{asset_root}/740x-1.jpg",
        f"{asset_root}/1200x776.jpg",
        f"{asset_root}/100x-1.jpg",
    ]


def test_bloomberg_parser_removes_legacy_image_and_share_controls():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="Bloomberg illustrated report">
      <meta property="article:published_time"
            content="2018-04-25T00:00:00Z">
    </head><body>
      <div class="body-copy-v2">
        <figure class="figure-expandable">
          <div class="image" role="button" tabindex="0"
               aria-label="Open image in viewer">
            <img src="https://assets.bwbx.io/images/users/example/id/v0/-1x-1.jpg"
                 alt="Editorial photograph">
          </div>
          <figcaption>Editorial photograph caption.</figcaption>
        </figure>
        <span class="SocialShare-IconWrapper SocialShare-IconWrapper_variant_button"
              role="button" aria-label="print"></span>
        <button class="comment-count-v2__link"
                aria-label="Go to comments"></button>
        <button class="disqus-v2__tout">Comments</button>
        <p>{reporting}</p>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/"
            "2018-04-25/illustrated-report"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"
    assert "role=\"button\"" not in result.body_html
    assert "tabindex=" not in result.body_html
    assert "Open image in viewer" not in result.body_html
    assert "SocialShare-" not in result.body_html
    assert "Go to comments" not in result.body_html
    assert ">Comments<" not in result.body_html
    assert "Editorial photograph" in result.body_html


def test_bloomberg_parser_keeps_only_full_legacy_lightbox_image():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    result = parse_article(
        f"""
        <html><head>
          <meta property="og:title" content="Election market reaction">
          <meta property="og:image"
                content="http://www.bloomberg.com/image/thumbnail.jpg">
          <meta property="article:published_time"
                content="2012-11-07T18:00:00Z">
        </head><body><div id="story_content">
          <p>{reporting}</p>
          <div class="thumbnail_container overlay_container">
            <a class="enlarge_image" href="/photo/election/257720.html">
              <span>Enlarge image</span>
              <img alt="Election Tickers"
                   src="http://www.bloomberg.com/image/thumbnail.jpg">
            </a>
            <div class="simple_overlay">
              <h3 class="image_title">Election Tickers</h3>
              <img alt="Election Tickers" width="640" height="369"
                   src="http://www.bloomberg.com/image/full-size.jpg">
            </div>
          </div>
        </div></body></html>
        """.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-11-07/"
            "election-market-reaction"
        ),
    )

    assert result.quality.status.value == "complete"
    assert len(result.images) == 1
    assert result.images[0].original_url.endswith("/full-size.jpg")
    assert result.images[0].width == 640
    assert result.images[0].height == 369
    assert "thumbnail.jpg" not in result.body_html


def test_parser_supports_legacy_nyt_story_body_and_pdate():
    canonical_url = (
        "https://www.nytimes.com/2016/01/03/business/example.html"
    )
    body = " ".join(["Historical reporting sentence."] * 30)
    html = f"""
    <html>
      <head>
        <meta name="pdate" content="20160102">
        <meta property="og:title" content="Legacy NYT headline">
      </head>
      <body><div class="story-body"><p>{body}</p></div></body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
        raw_capture=raw_capture("nyt", canonical_url),
    )

    assert result.quality.status.value == "complete"
    assert result.headline == "Legacy NYT headline"
    assert result.published_at == datetime(
        2016,
        1,
        2,
        tzinfo=timezone.utc,
    )
    assert result.quality.body_characters >= 200


def test_parser_combines_split_2012_nyt_article_body_containers():
    canonical_url = (
        "https://www.nytimes.com/2012/01/21/technology/example.html"
    )
    continuation = " ".join(["Continuation reporting sentence."] * 30)
    html = f"""
    <html>
      <head>
        <meta name="pdate" content="20120121">
        <meta property="og:title" content="Historical NYT headline">
      </head>
      <body>
        <div class="articleBody">
          <p itemprop="articleBody">Opening paragraph with the central news.</p>
        </div>
        <aside><p>Related-story navigation must not be included.</p></aside>
        <div class="articleBody">
          <p itemprop="articleBody">{continuation}</p>
        </div>
      </body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
        raw_capture=raw_capture("nyt", canonical_url),
    )

    assert result.quality.status.value == "complete"
    assert "Opening paragraph" in result.plain_text
    assert "Continuation reporting" in result.plain_text
    assert "Related-story navigation" not in result.plain_text
    assert result.extraction.parser_version == "nyt-parser/0.8.54"

def test_nyt_parser_separates_legacy_credits_and_removes_recirculation():
    canonical_url = "https://www.nytimes.com/2017/06/02/example.html"
    reporting = " ".join(["Substantive NYT reporting sentence."] * 20)
    html = f"""
    <html><head>
      <meta property="og:title" content="Legacy visual story">
      <meta property="article:published_time"
            content="2017-06-02T12:00:00Z">
    </head><body><article class="Story-story--2QyGh">
      <button class="SectionBarShare-shareButton--2f1RP">
        Share
      </button>
      <button class="button">View More <i class="expand-icon"></i></button>
      <button class="button">Comment on ArtsBeat</button>
      <button id="comment-callout-comment-button">
        <i class="icon"></i>
      </button>
      <button class="legacy-floating-control">Save story</button>
      <p>{reporting}</p>
      <figure>
        <img src="https://static01.nyt.com/images/editorial.jpg">
        <figcaption class="ResponsiveMedia-caption--1dUVu">
          <span class="ResponsiveMedia-credit--3F-q_"
                itemprop="copyrightHolder">
            <span class="accessibility-visuallyHidden--OUeHR">Credit</span>
            <span>Hiroyuki Ito</span>
          </span>
        </figcaption>
      </figure>
      <div class="Recirculation-moreInRecirculation--2skgO">
        <p>Recommended story text must not enter the article.</p>
        <figure><img
          src="https://static01.nyt.com/images/recommended.jpg">
          <figcaption>Recommended photograph</figcaption>
        </figure>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
        raw_capture=raw_capture("nyt", canonical_url),
    )

    assert result.quality.status.value == "complete"
    assert "Recommended story" not in result.plain_text
    assert len(result.images) == 1
    assert result.images[0].caption is None
    assert result.images[0].credit == "Hiroyuki Ito"
    assert "recommended.jpg" not in result.body_html
    assert "SectionBarShare" not in result.body_html
    assert "<button" not in result.body_html
    assert "Save story" not in result.plain_text
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_rejects_short_unhydrated_interactive_shell():
    summary = (
        "Russian forces shelled the evacuation route as civilians "
        "attempted to flee to safety on Sunday. Several people were killed."
    )
    payload = json.dumps(
        {
            "initialState": {
                "$Article:photo.sprinkledBody.content.0": {
                    "__typename": "ParagraphBlock",
                },
                "$Article:photo.sprinkledBody.content.1": {
                    "__typename": "InteractiveBlock",
                },
            }
        }
    )
    html = f"""
    <html><head>
      <meta property="og:title"
            content="In photos: Evacuation comes under fire">
      <meta property="article:published_time"
            content="2022-03-06T12:00:00Z">
      <meta property="og:image"
            content="https://static01.nyt.com/vi-assets/images/share/1200x675_nameplate.png">
    </head><body>
      <section name="articleBody"><p>{summary}</p></section>
      <script>window.__preloadedData = {payload};</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2022/03/06/world/europe/"
            "in-photos-evacuation-comes-under-fire-near-kyiv.html"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "partial"
    assert "incomplete-interactive" in result.quality.warnings
    assert result.quality.images_selected == 0
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_preserves_legacy_interactive_script_shell():
    canonical_url = (
        "https://www.nytimes.com/interactive/2018/02/23/opinion/"
        "columnists/poisons-in-our-bodies.html"
    )
    result = parse_article(
        b"""
        <html class="page-interactive"><head>
          <meta property="og:title" content="What Poisons Are in Your Body?">
          <meta property="article:published_time" content="2018-02-23T00:00:00Z">
        </head><body><main><article id="story" class="story theme-interactive">
          <header class="interactive-header">
            <h1 class="interactive-headline">What Poisons Are in Your Body?</h1>
          </header>
          <p>By a New York Times columnist.</p>
        </article></main></body></html>
        """,
        publisher="nyt",
        canonical_url=canonical_url,
        raw_capture=raw_capture("nyt", canonical_url),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert "body-too-short" not in result.quality.warnings
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_keeps_hydrated_image_interactive_over_short_metadata():
    canonical_url = (
        "https://www.nytimes.com/interactive/2023/02/13/opinion/"
        "valentines-day-chatgpt-share.html"
    )
    interactive_text = (
        "Someone sent you a Valentine! New York Times Opinion asked "
        "ChatGPT to try its hand at spinning up love letters — here’s "
        "yours. Now make your own! Photo illustration by Ryan Haskins"
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Valentine, From A.I. to You">
      <meta name="description"
            content="We’re asking ChatGPT to capture love.">
      <meta property="article:section" content="Opinion">
    </head><body>
      <section class="interactive-content">
        <div class="interactive-body">
          <div>{interactive_text}</div>
          <img src="https://static01.nyt.com/images/valentine.jpg">
          <button>Now make your own!</button>
        </div>
      </section>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
        raw_capture=raw_capture("nyt", canonical_url),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "opinion"
    assert "Someone sent you a Valentine" in result.plain_text
    assert "capture love" not in result.plain_text
    assert result.images[0].original_url.endswith("valentine.jpg")


def test_bloomberg_parser_extracts_livemint_partner_story_content():
    canonical_url = (
        "https://www.bloomberg.com/opinion/articles/2025-06-04/"
        "texas-is-going-about-its-hollywood-ambitions-all-wrong"
    )
    paragraphs = "".join(
        (
            "<p class='storyParagraph'>Bloomberg licensed reporting "
            f"paragraph {index} contains substantive analysis about the "
            "film industry, public investment, tax credits and economic "
            "development policy.</p>"
        )
        for index in range(1, 7)
    )
    html = f"""
    <html><head>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "Texas Is Going About Its Hollywood Ambitions All Wrong",
        "datePublished": "2025-06-04T18:37:00+05:30",
        "author": {{"name": "Bloomberg News"}}
      }}
      </script>
    </head><body>
      <div class="storyPage_storyContent__3xuFc">{paragraphs}</div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert result.quality.body_characters >= 400
    assert "paragraph 6" in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_arabamerica_partner_excludes_site_chrome_and_audio():
    paragraphs = "".join(
        f"<p>Licensed Bloomberg paragraph {index} contains substantive "
        "reporting about investment, diplomacy, economic development, "
        "business conditions, and regional policy.</p>"
        for index in range(1, 7)
    )
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Palestinian Tycoon Brings His Fortune Home">
      <meta property="og:url"
            content="https://www.arabamerica.com/palestinian-tycoon/">
      <meta property="article:published_time"
            content="2014-01-03T00:00:00Z">
    </head><body><main>
      <nav>Home About Events News Resources</nav>
      <audio><source type="audio/mpeg" src="/word-of-the-day.mp3"></audio>
      <div class="wp-polls">Poll No (86%) Yes (14%) Loading ...</div>
      <ul id="menu-footer-navigation">
        <li>Help &amp; feedback</li><li>Terms &amp; conditions</li>
        <li>Privacy policy</li>
      </ul>
      <p class="copy">Copyright 2026 Arab America</p>
      <div class="content single"><div class="content-in">
        <div class="print">
          <h2>Palestinian Tycoon Brings His Fortune Home</h2>
          <p class="author">posted on: Jan 3, 2014</p>
          <div class="mailmunch-forms-before-post">Subscribe</div>
          {paragraphs}
          <p>David Wainer, Jonathan Ferziger The Daily Star</p>
          <div class="mailmunch-forms-after-post">Newsletter</div>
        </div>
        <div class="related-block">Recommended stories</div>
      </div></div>
    </main></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-01-01/"
            "palestinian-tycoon-seeking-peace-brings-his-fortune-home"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "article"
    assert "Licensed Bloomberg paragraph 6" in result.plain_text
    assert "David Wainer, Jonathan Ferziger" in result.plain_text
    assert "Poll No (86%)" not in result.plain_text
    assert "Help & feedback" not in result.plain_text
    assert "Privacy policy" not in result.plain_text
    assert "Copyright 2026 Arab America" not in result.plain_text
    assert "Recommended stories" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_macdailynews_excerpt_is_partial_without_partner_tail():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Apple getting ahead of legal system">
      <meta property="og:url"
            content="https://macdailynews.com/2013/06/06/apple-ban/">
      <meta property="article:published_time"
            content="2013-06-06T14:30:00Z">
    </head><body><article>
      <div class="entry-content">
        <p>Bloomberg licensed excerpt paragraph one contains substantive
        reporting about the legal dispute and import ban.</p>
        <p>Bloomberg licensed excerpt paragraph two explains the available
        appeals and government review process in detail.</p>
        <p>Bloomberg licensed excerpt paragraph three quotes the companies
        discussing device supply and their customers.</p>
        <p>Read more in the full article <a
          href="https://www.bloomberg.com/news/articles/example">here</a>.</p>
        <blockquote>MacDailyNews Take: partner commentary.</blockquote>
        <p>Related articles: unrelated partner recirculation.</p>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-06-05/"
            "apple-getting-ahead-of-legal-system-to-contain-ban-damage"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "excerpt paragraph three" in result.plain_text
    assert "Read more in the full article" not in result.plain_text
    assert "MacDailyNews Take" not in result.plain_text
    assert "Related articles" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parcel_industry_teaser_excludes_site_recirculation():
    html = b"""
    <html><head>
      <meta property="og:title" content="FedEx Sees Shipment Peak">
      <meta property="og:url"
            content="https://parcelindustry.com/article-3799-example.html">
    </head><body>
      <article class="article">
        <div class="fulltext-txt"><div id="contentText">
          Bloomberg BusinessWeek--FedEx projects its busiest shipping day
          will be Cyber Monday. <a href="https://bloomberg.com">Read more</a>!
        </div></div>
      </article>
      <main>
        <section>Articles Your Parcel Contract is Probably Costing You
          More Than You Think</section>
        <section>Most Read</section>
      </main>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-10-23/"
            "fedex-sees-shipments-peaking"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value != "complete"
    assert "FedEx projects its busiest shipping day" in result.plain_text
    assert "Your Parcel Contract" not in result.plain_text
    assert "Most Read" not in result.plain_text


def test_bloomberg_blogspot_partner_recovers_direct_text_after_blockquote():
    html = b"""
    <html><head>
      <meta property="og:title" content="An Alternate History">
      <meta property="og:url"
            content="https://example.blogspot.com/2010/12/history.html">
      <meta property="article:published_time"
            content="2010-12-08T12:00:00Z">
    </head><body>
      <div class="entry-content">
        <blockquote>
          <p>The opening reports how the musician's imagined life changed
          over several decades and describes the public events he attended.</p>
          <p>The timeline continues through 2009 with enough licensed copy
          to establish that this is the article body rather than a teaser.
          It recounts several later milestones, public appearances, studio
          sessions, charitable projects, and conversations with his family.
          The detailed chronology supplies additional reporting that a
          navigation card or short promotional excerpt would never contain.</p>
        </blockquote>
        An ordinary day follows in the household, and the musician gives
        his blessing before deciding to head for the recording studio.
        <div class="post-footer">Partner labels and sharing controls</div>
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-12-08/"
            "an-alternate-history"
        ),
        allow_generic_syndication=True,
    )

    assert "An ordinary day follows in the household" in result.plain_text
    assert "Partner labels and sharing controls" not in result.plain_text


def test_bloomberg_investinglive_migration_rejects_current_recirc_as_body():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Greek parliament begins second vote: BBG">
      <meta property="og:url"
            content="https://investinglive.com/news/!/greek-vote-20111020">
      <meta property="article:published_time"
            content="2011-10-20T17:51:21Z">
    </head><body>
      <article>
        <h1>Greek parliament begins second vote: BBG</h1>
        <div class="MostPopularList"><p>Current Fed headline unrelated to
        the archived report.</p><p>Current gold-market headline unrelated
        to the archived report.</p></div>
        <div class="TopBrokersComparisons"><h2>Best in 2026</h2>
          <p>Sponsored</p></div>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-10-20/"
            "greek-parliament-begins-second-vote"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value != "complete"
    assert "Current Fed headline" not in result.plain_text
    assert "Best in 2026" not in result.plain_text
    assert "Sponsored" not in result.plain_text


def test_bloomberg_legacy_slideshow_keeps_only_full_captions():
    html = b"""
    <html><head>
      <meta property="og:title" content="Mercedes Reboots the C-Class">
      <meta property="article:published_time"
            content="2014-01-12T12:00:00Z">
    </head><body><article><div class="body-copy">
      <div class="slideshow_teaser">
        <img src="https://assets.bwbx.io/first.jpg">
        <p>First full caption about the new car and its safety systems.</p>
      </div>
      <div class="slider_contain">
        <div class="slider_close">Close</div>
        <div class="the_slide">
          <img src="https://assets.bwbx.io/first.jpg">
          <div class="slide_caption">
            <p class="cap_preview">First shortened caption...
              <a href="#">Read More</a></p>
            <p class="cap_show">First full caption about the new car and
              its safety systems. <a href="#">Close</a></p>
          </div>
        </div>
        <div class="the_slide">
          <img src="https://assets.bwbx.io/second.jpg">
          <div class="slide_caption">
            <p class="cap_preview">Second shortened caption...
              <a href="#">Read More</a></p>
            <p class="cap_show">Second full caption from the production
              line. <a href="#">Close</a></p>
          </div>
        </div>
        <nav class="slider_controls">Previous Next</nav>
        <nav class="slider_nav">Slide 1 Slide 2</nav>
      </div>
      <p>Mercedes is adding smartphone-style technology to its sedan as
      the automaker works to improve safety and compete with rivals.</p>
      <p>The redesigned vehicle includes new sensors, cameras and
      assistance systems that were previously reserved for larger cars.</p>
    </div></article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-01-12/"
            "mercedes-reboots-the-c-class"
        ),
    )

    assert result.plain_text.count("First full caption") == 1
    assert result.plain_text.count("Second full caption") == 1
    assert "shortened caption" not in result.plain_text
    assert "Read More" not in result.plain_text
    assert "Close" not in result.plain_text
    assert "Previous Next" not in result.plain_text


def test_bloomberg_legacy_lightbox_keeps_caption_and_credit_once():
    html = b"""
    <html><head>
      <meta property="og:title" content="Bentley Mulsanne Review">
      <meta property="article:published_time"
            content="2012-07-04T12:00:00Z">
    </head><body><article><div class="body-copy">
      <p>The sedan is designed for owners who divide their time between
      the driver's seat and the rear passenger compartment.</p>
      <div class="image thumbnail decoratable">
        <a class="enlarge_image"><img src="https://example.com/small.jpg"></a>
        <div class="simple_overlay">
          <h3 class="image_title">Bentley Mulsanne</h3>
          <img src="https://example.com/large.jpg">
          <div class="details">
            <p class="photographer_attr">Bentley PR via Bloomberg</p>
            <p class="caption_only">A Bentley Mulsanne on a mountain road.</p>
          </div>
        </div>
        <p class="caption">A Bentley Mulsanne on a mountain road.
        Source: Bentley PR via Bloomberg</p>
      </div>
      <p>Its twin-turbocharged engine provides ample power despite the
      car's substantial weight and long wheelbase.</p>
    </div></article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-07-04/"
            "bentley-mulsanne-review"
        ),
    )

    assert result.plain_text.count("A Bentley Mulsanne on a mountain road") == 1
    assert result.plain_text.count("Bentley PR via Bloomberg") == 1
    assert any(
        image.original_url == "https://example.com/large.jpg"
        for image in result.images
    )


def test_bloomberg_syndicated_forecast_drops_email_alert_signup():
    html = b"""
    <html><head>
      <meta property="og:title" content="Emerging Market Losses Deepen">
    </head><body><article>
      <p>Wall Street firms expect losses in emerging-market bonds to
      intensify as borrowing costs rise and fund outflows continue.</p>
      <p>Investors have withdrawn billions of dollars from developing
      market funds while corporate borrowing spreads have widened.</p>
      <p>Read the full article at
      https://www.bloomberg.com/news/articles/2013-09-09/example</p>
      <p>Click here to receive free and immediate email alerts of the
      latest forecasts.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-09-09/"
            "emerging-market-losses-deepen"
        ),
    )

    assert "Read the full article" not in result.plain_text
    assert "email alerts" not in result.plain_text


def test_bloomberg_partner_mirror_drops_related_post_accordion():
    html = b"""
    <html><head>
      <meta property="og:title" content="Stem Cells Mend Scarred Hearts">
    </head><body><article>
      <p>Researchers found that experimental stem-cell therapy reduced
      scar tissue and helped damaged hearts recover.</p>
      <p>The randomized study followed patients after heart attacks and
      measured changes in functioning heart muscle.</p>
      <div class="accordion ddop">
        <span class="relatedposttitle">Related Posts</span>
        <ul>
          <li><a href="https://partner.example/advertorial">
            Stem Cell Treatment in Mexico by Progencell</a>
            <span class="lastupdated">[Last Updated On: October 11th, 2010]</span>
          </li>
        </ul>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-02-13/"
            "scarred-hearts-can-be-mended"
        ),
    )

    assert "Researchers found" in result.plain_text
    assert "Stem Cell Treatment in Mexico" not in result.plain_text
    assert "Last Updated On" not in result.plain_text


def test_bloomberg_drops_more_on_topic_recirculation_list():
    html = b"""
    <html><head>
      <meta property="og:title" content="Investor Risk Interview">
    </head><body><article>
      <p>The investor said emotion is the enemy and recommended a
      long-term strategy instead of frequent trading.</p>
      <p>That discipline matters most when markets become volatile and
      investors are tempted to sell near a bottom.</p>
      <p><strong>More on </strong><a href="/topic/investing">
        Do-It-Yourself Investing</a><strong>:</strong><br></p>
      <ul>
        <li><a href="/news/articles/2014-10-07/brain-scans">
          Brain Scans Light the Way to Investing</a><br></li>
        <li><a href="/news/articles/2014-10-08/investor-fees">
          An Investor's Guide to Fees</a></li>
      </ul>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-10-07/"
            "investor-risk-interview"
        ),
    )

    assert "emotion is the enemy" in result.plain_text
    assert "More on" not in result.plain_text
    assert "Brain Scans Light" not in result.plain_text
    assert "Guide to Fees" not in result.plain_text


def test_bloomberg_drops_terminal_malformed_related_headlines():
    html = b"""
    <html><head>
      <meta property="og:title" content="T-Mobile Subscriber Growth">
    </head><body><article>
      <p>T-Mobile raised its annual subscriber forecast after customer
      additions exceeded analysts' estimates.</p>
      <p>The company said lower prices and rollover offers helped it win
      customers from larger wireless competitors.</p>
      <p><a href="https://www.bloomberg.com/news/articles/2015-04-20/first">
        Verizon, AT&amp;T Seen Trailing T-Mobile in Wireless User Growth</a>
        T-Mobile Brings Steep Price Cuts to Mobile Business Users</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-04-28/"
            "t-mobile-subscriber-growth"
        ),
    )

    assert "raised its annual subscriber forecast" in result.plain_text
    assert "Verizon, AT&T Seen Trailing" not in result.plain_text
    assert "Steep Price Cuts" not in result.plain_text


def test_bloomberg_drops_packed_terminal_related_news_commands():
    html = b"""
    <html><head>
      <meta property="og:title" content="Sudan Oil Dispute">
    </head><body><article>
      <p>South Sudan assumed control of most of the former state's oil
      production after it seceded in July.</p>
      <p>Negotiators plan to resume talks over transit fees later this
      month, officials from both governments said.</p>
      <p>For Related News &amp; Information:
      On South Sudan's Oil Industry: {TNI SOUTHSUDAN OIL &lt;GO&gt;}
      Top Regional Stories: {AFTO &lt;GO&gt;}
      Most-Read Africa News: {MNI AFRICA &lt;GO&gt;}</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-12-08/"
            "sudan-oil-dispute"
        ),
    )

    assert "assumed control" in result.plain_text
    assert "For Related News" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert "Most-Read Africa News" not in result.plain_text


def test_bloomberg_partner_mirror_drops_star_rating_form():
    html = """
    <html><head>
      <meta property="og:title" content="Shipping Stocks Favored">
    </head><body><article>
      <p>The fund manager favors U.S. shipping stocks because valuations
      remain low while freight demand and vessel utilization improve.</p>
      <p>He discusses the industry outlook and investment strategy in an
      interview originally produced by Bloomberg Television.</p>
      <form class="rating" id="articleVotesSubmit">
        <h3>ΣΑΣ ΑΡΕΣΕ ΤΟ ΑΡΘΡΟ;</h3>
        <h4 class="clasificacion">
          <input name="star" type="radio"><label>★</label>
          <input name="star" type="radio"><label>★</label>
          <input name="star" type="radio"><label>★</label>
          <input name="star" type="radio"><label>★</label>
          <input name="star" type="radio"><label>★</label>
        </h4>
      </form>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-09-21/"
            "shipping-stocks-video"
        ),
    )

    assert "shipping stocks" in result.plain_text
    assert "★" not in result.plain_text
    assert "ΣΑΣ ΑΡΕΣΕ" not in result.plain_text


def test_bloomberg_pv_magazine_teaser_excludes_partner_footer():
    reporting = (
        "France is revising its solar-energy targets and reducing subsidies "
        "as installations grow faster than expected. "
    ) * 12
    html = f"""
    <html><head>
      <meta property="og:title"
            content="France Needs More Solar Subsidy Cuts">
      <meta property="og:url"
            content="https://www.pv-magazine.com/2010/09/08/example/">
    </head><body><main class="pvmagazine-post-container">
      <nav>Applications and Installations Markets and Policy</nav>
      <div class="pvmagazine-post-content">
        <p>{reporting}
          <a href="https://www.bloomberg.com/example">
            Click here to read the rest of the report.
          </a>
        </p>
      </div>
      <div class="pvmagazine-disclaimer-block">
        This content is protected by copyright. Please contact us.
      </div>
      <div class="pvmagazine-author-block">View author posts</div>
      <div class="pvmagazine-comments-block">Please login to comment</div>
    </main></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-09-06/"
            "france-needs-more-solar-subsidy-cuts"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "France is revising" in result.plain_text
    assert "protected by copyright" not in result.plain_text
    assert "View author posts" not in result.plain_text
    assert "Please login" not in result.plain_text


def test_bloomberg_partner_full_story_teaser_drops_membership_tail():
    reporting = (
        "Samsung received permission to erect a test turbine off Scotland "
        "while officials reviewed renewable energy targets. "
    ) * 12
    html = f"""
    <html><head>
      <meta property="og:title" content="Samsung Heavy Gets Permission">
      <meta property="og:url"
            content="https://www.eco-business.com/news/samsung-heavy/">
    </head><body>
      <div class="storyContent">
        <p>{reporting}</p>
        <p><em>Click here to
          <a href="https://www.bloomberg.com/news/articles/example">read</a>
          the full story.</em></p>
        <div class="eb-article__eb-circle-banner">
          <h3>Like this content? Join our growing community.</h3>
          <p>Your support helps to strengthen independent journalism.</p>
        </div>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-05-07/"
            "samsung-heavy-gets-permission"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "Samsung received permission" in result.plain_text
    assert "Click here" not in result.plain_text
    assert "growing community" not in result.plain_text
    assert "strengthen independent journalism" not in result.plain_text


def test_bloomberg_eco_business_full_copy_drops_membership_banner():
    reporting = (
        "Conservation funding should protect diverse habitats while "
        "officials compare the benefits of individual species programs. "
    ) * 12
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Why I hate pandas and you should too">
      <meta property="og:url"
            content="https://www.eco-business.com/opinion/pandas/">
    </head><body>
      <article>
        <section class="eb-article__body-content">
          <p>{reporting}</p>
          <p><em>Timothy Lavin is an editorial board member at
            Bloomberg View. Follow him on Twitter.</em></p>
          <div class="eb-article__eb-circle-banner">
            <div class="eb-item__header">
              <h3>Like this content? Join our growing community.</h3>
              <p>Your support helps to strengthen independent journalism.
                Unlock unlimited access to our content.</p>
            </div>
          </div>
        </section>
        <section class="eb-events">
          <p>Transition under pressure: how geopolitics is shaping
            Asia's clean energy future.</p>
          <p>Unlocking capital for sustainability conference.</p>
        </section>
      </article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-08-27/"
            "why-i-hate-pandas-and-you-should-too"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "Conservation funding" in result.plain_text
    assert "Timothy Lavin" in result.plain_text
    assert "growing community" not in result.plain_text
    assert "strengthen independent journalism" not in result.plain_text
    assert "Unlock unlimited access" not in result.plain_text
    assert "Transition under pressure" not in result.plain_text
    assert "Unlocking capital" not in result.plain_text


def test_bloomberg_john_lothian_digest_keeps_only_target_summary():
    html = b"""
    <html><head>
      <meta property="og:title" content="July 10 environmental newsletter">
      <meta property="og:url"
            content="https://johnlothiannews.com/july-10-newsletter/">
    </head><body><article><div class="entry-content">
      <p>In today's edition: Bloomberg reports the lead story while
        other publications cover unrelated topics.</p>
      <h2>Lead Stories</h2>
      <p><strong>Germany Sees Intensive EU Carbon Talks in September,
        October</strong><br>Bloomberg<br>
        Germany plans talks with other European Union countries about
        the bloc's long-term climate goals and expects intensive
        discussions in autumn.<br>
        <a href="http://jlne.ws/example">http://jlne.ws/example</a></p>
      <p><strong>JPMorgan Probe Shows FERC Priority Policing Energy</strong>
        <br>Bloomberg<br>The agency opened an unrelated investigation.
        <br><a href="http://jlne.ws/other">http://jlne.ws/other</a></p>
      <p><strong>Steep Fuel Prices Driving Efficient Aircraft</strong>
        <br>The New York Times<br>An unrelated aviation summary.</p>
      <h2>Reports</h2>
      <p>View all reports &gt;</p>
    </div></article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-07-09/"
            "germany-sees-intensive-eu-carbon-talks-in-september-october"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "Germany plans talks" in result.plain_text
    assert "JPMorgan" not in result.plain_text
    assert "New York Times" not in result.plain_text
    assert "View all reports" not in result.plain_text
    assert "jlne.ws" not in result.plain_text


def test_bloomberg_insurance_journal_copy_drops_tags_and_subscribe_card():
    reporting = (
        "British insurers agreed to finance infrastructure projects while "
        "officials prepared updated fiscal forecasts for Parliament. "
    ) * 12
    html = f"""
    <html><head>
      <meta property="og:title" content="Insurers invest in infrastructure">
      <meta property="og:url"
            content="https://www.insurancejournal.com/news/example.htm">
    </head><body><article><div class="entry-content">
      <div class="article-content clearfix">
        <p>{reporting}</p>
        <div class="subscribe-banner subscribe-banner-in-content-2">
          <h4>Subscribe for updates about Carriers</h4>
        </div>
        <p class="tagtag"><span class="tagtag">Topics</span>
          <a class="btn btn-primary tagtag">Carriers</a></p>
      </div>
    </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-12-04/"
            "biggest-u-k-insurers-to-invest-41-billion-in-infrastructure"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "British insurers agreed" in result.plain_text
    assert "Topics" not in result.plain_text
    assert "Carriers" not in result.plain_text
    assert "Subscribe for updates" not in result.plain_text


def test_bloomberg_mediapart_summary_is_partial_without_partner_chrome():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Hollande Popularity Sinks Below 20%">
      <meta property="og:url"
            content="https://www.mediapart.fr/en/journal/france/example">
    </head><body><article>
      <p>La redaction de Mediapart</p>
      <p>This article is freely available.</p>
      <p>To support Mediapart subscribe</p>
      <div class="news__body__center__article">
        <p class="dropcap-wrapper">
          <span aria-hidden="true"><span class="dropcap">F</span>rench</span>
          <span class="screen-reader-only">French</span> President Francois
          Hollande's approval rating fell below 20 percent for the first
          time since his election.</p>
        <p>The poll reflected unemployment and turmoil in his personal
        life while his government faced mounting pressure.</p>
        <p>His rating was lower than those of his predecessors at the
        same point in their mandates.</p>
        <p>Read more of this Bloomberg report published by the
        <a href="https://www.sfgate.com/example">
          San Francisco Chronicle</a>.</p>
      </div>
      <ul class="action-links"><li>Comment</li></ul>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-02-06/"
            "hollande-popularity-sinks"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "French President" in result.plain_text
    assert "F rench French" not in result.plain_text
    assert "same point in their mandates" in result.plain_text
    assert "Read more of this Bloomberg report" not in result.plain_text
    assert "La redaction de Mediapart" not in result.plain_text
    assert "freely available" not in result.plain_text
    assert "support Mediapart" not in result.plain_text
    assert "Comment" not in result.plain_text


def test_bloomberg_ctrm_partner_drops_republication_disclaimer():
    reporting = (
        "RWE expanded liquefied natural gas trading in the United States as "
        "banks reduced their commodities operations. "
    ) * 12
    html = f"""
    <html><head>
      <meta property="og:title" content="RWE Expands in LNG">
      <meta property="og:url"
            content="https://www.ctrmcenter.com/news/example/">
    </head><body><article>
      <div class="entry-content">
        <p>{reporting}</p>
      </div>
      <div class="cat_postinfo postinfo clearfix">
        <strong>31 January 2014</strong>
        <p><span class="bio"><strong>Disclaimer:</strong>
          This news article was published by another website and was
          republished on the CTRM Center due to its informative merit and
          relative nature. If you have any issue with this post please
          contact us.
        </span></p>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-01-31/"
            "rwe-expands-in-lng"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "RWE expanded" in result.plain_text
    assert "republished on the CTRM Center" not in result.plain_text
    assert "issue with this post" not in result.plain_text


def test_bloomberg_ctrm_partner_drops_nested_ads_and_recirculation():
    reporting_before = (
        "Economic sanctions are inhibiting legitimate commodity trading "
        "because regulatory guidance remains unclear. "
    ) * 6
    reporting_after = (
        "The survey covered commodity producers, traders, financiers, and "
        "logistics providers across several international markets. "
    ) * 6
    html = f"""
    <html><head>
      <meta property="og:title" content="Commodity Traders and Sanctions">
      <meta property="og:url"
            content="https://www.ctrmcenter.com/news/commodity-trading-news/example/">
    </head><body><article><div class="entry-content">
      <p>{reporting_before}
        <div class="gsfnura gsfnura-3">
          <div class="inPost">Advertising</div>
        </div>
        {reporting_after}
        Sponsored Links
        <div class="gsfnura gsfnura-4">
          <a href="/vendor"><span class="title">
            Amphora answers the question, Why Purchase a CTRM?
          </span></a>
          <a href="/vendor-two"><span class="title">
            Enuit is an award-winning ETRM provider.
          </span></a>
        </div>
        <div class="mostRecentPosts">
          <h4>You may also be interested in</h4>
          <a href="/unrelated"><h3>Unrelated trading gateway story</h3></a>
        </div>
      </p>
    </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-03-31/"
            "commodity-traders-and-sanctions"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "Economic sanctions" in result.plain_text
    assert "survey covered" in result.plain_text
    assert "Advertising" not in result.plain_text
    assert "Sponsored Links" not in result.plain_text
    assert "Amphora" not in result.plain_text
    assert "Enuit" not in result.plain_text
    assert "Unrelated trading gateway" not in result.plain_text


def test_bloomberg_embedded_document_renders_tabular_data():
    reporting = [
        {
            "type": "paragraph",
            "content": [
                {
                    "type": "text",
                    "value": (
                        f"Bloomberg report paragraph {index} contains "
                        "substantive market analysis and context."
                    ),
                }
            ],
        }
        for index in range(1, 7)
    ]
    payload = {
        "story": {
            "body": {
                "type": "document",
                "content": [
                    *reporting,
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "value": "Here are the fund returns:",
                            }
                        ],
                    },
                    {
                        "type": "tabularData",
                        "content": [
                            {
                                "type": "columns",
                                "data": {
                                    "definitions": [
                                        {"title": "Fund"},
                                        {"title": "Strategy"},
                                        {"title": "2023 % Return"},
                                    ]
                                },
                            },
                            {
                                "type": "row",
                                "content": [
                                    {
                                        "type": "cell",
                                        "content": [
                                            {
                                                "type": "text",
                                                "value": "SoMa Partners",
                                            }
                                        ],
                                    },
                                    {
                                        "type": "cell",
                                        "content": [
                                            {
                                                "type": "text",
                                                "value": "Equity",
                                            }
                                        ],
                                    },
                                    {
                                        "type": "cell",
                                        "content": [
                                            {
                                                "type": "text",
                                                "value": "62.1",
                                            }
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        }
    }
    html = f"""
    <html><head>
      <meta property="og:title" content="Hedge Fund Returns">
      <meta property="article:published_time"
            content="2024-01-05T12:22:37Z">
      <script id="__NEXT_DATA__" type="application/json">
        {json.dumps(payload)}
      </script>
    </head><body></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2024-01-05/"
            "hedge-fund-returns"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Here are the fund returns:" in result.plain_text
    assert "SoMa Partners" in result.plain_text
    assert "2023 % Return" in result.plain_text
    assert "<table>" in result.body_html


def test_bloomberg_generic_syndication_removes_partner_controls_and_law_cta():
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2023-08-10/"
        "fashion-antitrust-battle"
    )
    reporting = "".join(
        f"<p>Bloomberg licensed report paragraph {index} explains the "
        "antitrust dispute and court proceedings in substantive detail.</p>"
        for index in range(1, 7)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Fashion Antitrust Battle">
      <meta property="og:url"
            content="https://www.linkedin.com/posts/example-story">
    </head><body><article>
      {reporting}
      <p>3,463 followers</p>
      <p>Report this post</p>
      <section class="comment">
        <p class="comment__text">Unrelated LinkedIn reader comment</p>
      </section>
      <p>"English Bloomberg report remains complete." "Rapport français
      dupliqué." #markets #finance #stocks #funds #research</p>
      <a role="button" href="/article-image">
        <img src="https://media.example.com/article.jpg"
             alt="Fashion company headquarters">
      </a>
      <div class="ellipsis-menu">
        <button aria-label="Open menu">More</button>
      </div>
      <a role="button" class="copy-link">Copy Link</a>
      <!-- <a role="button" class="comment-here">Comment Now</a> -->
      <div class="watchOrListen-bottom-section-v3">
        <p>Unrelated energy video recommendation</p>
      </div>
      <div class="content-loader --widget-container">
        <h2>Most Discussed</h2>
      </div>
      <div class="liveEventMain_widget custom_ad">
        <h3>Live Events</h3>
      </div>
      <div class="primeSWrapper">
        <div class="ts-dots"><span>1</span><span>2</span><span>3</span></div>
      </div>
      <div class="bottomTopics"><ul class="topicList">
        <li>Unrelated topic tag</li>
      </ul></div>
      <div class="tags"><ul id="tag-themes">
        <li>Unrelated regional tag</li>
      </ul></div>
      <div data-animation-role="button"><a>Read the Article</a></div>
      <div data-content-field="tags">Unrelated Squarespace tag</div>
      <div id="views-bootstrap-article-node-view-block-4">
        <h4>Unrelated transport-news recirculation card</h4>
      </div>
      <p>Uploaded by Partner Site Editor</p>
      <p>Top Trending Stocks: SBI Share Price, HDFC Bank Share Price</p>
      <p>Get automatic alerts for this topic.</p>
      <p>About This Source</p>
      <p>⚠️ Disclaimer: This content is for training purposes only and
      should not be considered financial advice.</p>
      <p>This article was generated from an automated news agency feed
      without modifications to text.</p>
      <h3>Share this:</h3>
      <p>📰 Source</p>
      <p>For complete coverage and additional details, visit the original
      article published by Bloomberg.com.</p>
      <p>Subscribe to ET Prime and read the Economic Times ePaper
      Online.and Sensex Today.</p>
      <p>Read the Full Article</p>
      <p>Get the latest insurance news sent straight to your inbox.</p>
      <p>Maritime and shipping</p>
      <p>Discussion</p>
      <div class="ai_podcast_030825">
        <p>Listen to this article in summarized format</p>
      </div>
      <p>Most Popular</p>
      <p>Want to stay up to date?</p>
      <p>Get More Podcast Analytics</p>
      <p>Here are more articles you may enjoy.</p>
      <p>Trade these moves with SignalPro</p>
      <p>Was this article valuable?</p>
      <p>Interested in AI?</p>
      <p>Move the slider to your real monthly trading volume. Figures
      shown are your earnings.</p>
      <p>Trending Now</p>
      <p>Build Draft Survey skills through practical training.</p>
      <p>How much could you earn back per year?</p>
      <p>Related Articles</p>
      <p>Related coverage: Unrelated gold-market headline</p>
      <div class="news-detail-content-block ai-post">
        <p>Partner-generated AI investment summary</p>
      </div>
      <section id="story-source-gallery">
        <p>Each image keeps its publisher and citation text.</p>
      </section>
      <div class="xenforo-comment-widget">
        <p>Unrelated hardware-forum reader comment</p>
      </div>
      <div class="cbcalc-wrap"><p>Cashback Calculator</p></div>
      <hr><p>... ADVERTISEMENT ...</p>
      <p>Bullion dealer promotional copy</p><hr>
      <p>Sign up for the Business of Food newsletter for food news.</p>
      <p>and yet Equinor still....</p>
      <p>https://www.gata.org/sites/default/files/GATA-silver-round-front.png</p>
      <p>Get the latest Nigerian news delivered to your inbox.</p>
      <p>Want more Bloomberg Opinion? Terminal readers head toOPIN
      &lt;GO&gt;. Or subscribe to our daily newsletter.</p>
      <div class="usstock_widget"><h2>S&amp;P 500 Top Gainers</h2></div>
      <div data-testid="headline-stack-promo-liner-test-id">
        Sign up now: Get insights on the biggest stories in Malaysia
      </div>
      <div data-testid="tags-test-id"><a><p>Taiwan</p></a></div>
      <h6>More on this topic</h6>
      <p>Top Tech Stories</p>
      <ul><li>Unrelated technology story</li></ul>
      <p>Written by: Reporter One and Reporter Two — With assistance from
      Editor Three @Bloomberg</p>
      <img src="https://www.bloomberg.com/_next/image?url=https%3A%2F%2Fgroundnews.b-cdn.net%2Finterests%2Ftiny.jpg%3Fwidth%3D24&amp;w=64"
           alt="Unrelated low-resolution topic icon">
      <p>Sign up for The Brief, a daily afternoon newsletter showcasing
      Bloomberg Law’s top stories.</p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "paragraph 6" in result.plain_text
    assert "3,463 followers" not in result.plain_text
    assert "Report this post" not in result.plain_text
    assert "English Bloomberg report remains complete." in result.plain_text
    assert "Rapport français" not in result.plain_text
    assert "#markets" not in result.plain_text
    assert "The Brief" not in result.plain_text
    assert "role=\"button\"" not in result.body_html
    assert "Open menu" not in result.plain_text
    assert "Copy Link" not in result.plain_text
    assert "energy video recommendation" not in result.plain_text
    assert "Most Discussed" not in result.plain_text
    assert "Live Events" not in result.plain_text
    assert "1\n2\n3" not in result.plain_text
    assert "Unrelated topic tag" not in result.plain_text
    assert "Unrelated regional tag" not in result.plain_text
    assert "Read the Article" not in result.plain_text
    assert "Unrelated Squarespace tag" not in result.plain_text
    assert "transport-news recirculation" not in result.plain_text
    assert "Uploaded by Partner" not in result.plain_text
    assert "Top Trending Stocks" not in result.plain_text
    assert "automatic alerts" not in result.plain_text
    assert "About This Source" not in result.plain_text
    assert "training purposes" not in result.plain_text
    assert "automated news agency feed" not in result.plain_text
    assert "LinkedIn reader comment" not in result.plain_text
    assert "Share this:" not in result.plain_text
    assert "📰 Source" not in result.plain_text
    assert "complete coverage" not in result.plain_text
    assert "ET Prime" not in result.plain_text
    assert "Read the Full Article" not in result.plain_text
    assert "insurance news" not in result.plain_text
    assert "Maritime and shipping" not in result.plain_text
    assert "Discussion" not in result.plain_text
    assert "summarized format" not in result.plain_text
    assert "Most Popular" not in result.plain_text
    assert "stay up to date" not in result.plain_text
    assert "Podcast Analytics" not in result.plain_text
    assert "more articles you may enjoy" not in result.plain_text
    assert "SignalPro" not in result.plain_text
    assert "article valuable" not in result.plain_text
    assert "Interested in AI" not in result.plain_text
    assert "real monthly trading volume" not in result.plain_text
    assert "Trending Now" not in result.plain_text
    assert "Draft Survey skills" not in result.plain_text
    assert "earn back per year" not in result.plain_text
    assert "Related Articles" not in result.plain_text
    assert "Related coverage" not in result.plain_text
    assert "AI investment summary" not in result.plain_text
    assert "Each image keeps" not in result.plain_text
    assert "hardware-forum reader comment" not in result.plain_text
    assert "Cashback Calculator" not in result.plain_text
    assert "Bullion dealer" not in result.plain_text
    assert "Business of Food newsletter" not in result.plain_text
    assert "Equinor still" not in result.plain_text
    assert "GATA-silver-round-front" not in result.plain_text
    assert "latest Nigerian news" not in result.plain_text
    assert "head toOPIN" not in result.plain_text
    assert "S&P 500 Top Gainers" not in result.plain_text
    assert "stories in Malaysia" not in result.plain_text
    assert "Taiwan" not in result.plain_text
    assert "More on this topic" not in result.plain_text
    assert "Top Tech Stories" not in result.plain_text
    assert "Unrelated technology story" not in result.plain_text
    assert "Written by: Reporter" not in result.plain_text
    assert "groundnews" not in result.body_html
    assert len(result.images) == 1


def test_bloomberg_signalpro_ai_summary_is_partial():
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2026-07-15/"
        "airbus-set-to-win-a330-jet-deals"
    )
    reporting = "".join(
        f"<p>Bloomberg reporting paragraph {index} describes the aircraft "
        "orders, customers, and negotiations in substantive detail.</p>"
        for index in range(1, 7)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Airbus Set to Win Jet Deals">
      <meta property="og:url"
            content="https://signalpro.markets/news/airbus-jet-deals">
    </head><body><article>
      {reporting}
      <div class="ai-block"><p>SignalPro AI analysis</p></div>
      <div class="cbcalc-wrap"><p>Cashback Calculator</p></div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "SignalPro AI analysis" not in result.plain_text
    assert "Cashback Calculator" not in result.plain_text


def test_bloomberg_repairs_malformed_yahoo_partner_markup():
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2026-03-19/"
        "oil-shock-sends-materials-stocks-lower"
    )
    html = b"""
    <html><head>
      <meta property="og:title" content="Oil Shock Hits Materials Stocks">
      <meta property="og:url"
            content="https://sg.finance.yahoo.com/news/materials-stocks.html">
    </head><body><article>
      <p>Opening Bloomberg paragraph contains substantive market reporting
      about industrial companies and elevated production costs.</p>
      <p>Investors questioned demand.<br>br /At the same time, metals stocks
      pulled back after a record run.nbsp;/ppPrecious metals traded like risk
      assets./ppHowever, chemical producers gained during the conflict.
      /ppemUploaded by Partner Name/em/ppstrongSee Also:/strong</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
        allow_generic_syndication=True,
    )

    assert "metals stocks pulled back" in result.plain_text
    assert "chemical producers gained" in result.plain_text
    assert "br /" not in result.plain_text
    assert "nbsp;/pp" not in result.plain_text
    assert "Uploaded by" not in result.plain_text
    assert "See Also" not in result.plain_text


def test_bloomberg_linkedin_copy_keeps_report_and_removes_social_credits():
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2026-04-01/"
        "markets-react-to-war-developments"
    )
    html = b"""
    <html><head>
      <meta property="og:title" content="Markets React to War Developments">
      <meta property="og:url"
            content="https://www.linkedin.com/posts/reporter-markets">
    </head><body><article>
      <p>Oil prices plunged and Treasury yields declined after the
      announcement. The market reaction showed investors expected talks to
      reduce the immediate risk to global energy supplies. With Isabelle Lee,
      Matthew Griffin and always-superb editing by William Selway Thanks
      several analysts for the smart insights https://lnkd.in/example</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
        allow_generic_syndication=True,
    )

    assert "market reaction showed investors" in result.plain_text
    assert "always-superb editing" not in result.plain_text
    assert "lnkd.in" not in result.plain_text


def test_bloomberg_third_party_rewrite_is_not_marked_complete():
    html = b"""
    <html><head>
      <meta property="og:title" content="Argentina Cuts World Cup Flights">
      <meta property="og:url"
            content="https://internationalinvestment.biz/en/news/flights">
    </head><body><article>
      <p>Bloomberg reported that the airline cut special services because of
      higher fuel costs and weaker demand from football fans.</p>
      <p>As International Investment experts report, the flight cuts show the
      limits of sports tourism when fuel and accommodation are expensive.</p>
      <p>The publisher then provides its own extended analysis of purchasing
      power, currencies, airline economics, and tournament attendance.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2026-05-29/"
            "fifa-world-cup-argentina-cuts-special-flights"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings


def test_bloomberg_abitech_analysis_is_partial_and_drops_feed_cards():
    html = b"""
    <html><head>
      <meta property="og:title" content="Congo Cobalt Supply">
      <meta property="og:url"
            content="https://example.africa/intelligence/congo-cobalt">
    </head><body><main>
      <span>ABITECH Analysis</span>
      <div class="report">
        <p>The analysis describes negotiations with cobalt producers and
        discusses supply-chain effects for battery manufacturers.</p>
        <p>It attributes the underlying announcement to Bloomberg Africa.</p>
      </div>
      <div class="card">
        <a>Other Zambia mining intelligence</a>
      </div>
    </main></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2026-05-14/"
            "congo-eyes-deals-with-cobalt-producers"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value != "complete"
    assert "truncated-body" in result.quality.warnings
    assert "Other Zambia" not in result.plain_text


def test_bloomberg_biggo_rewrite_is_partial_and_drops_google_promo():
    html = b"""
    <html><head>
      <meta property="og:title" content="Software Stocks and AI">
      <meta property="og:url"
            content="https://finance.biggo.com/news/software-stocks">
    </head><body><article>
      <div class="GooglePreferredSource_withDesc__example">
        Once added, BigGo Finance appears first in Google Search Top Stories.
      </div>
      <p>The page summarizes a strategist report about software companies,
      artificial intelligence, valuations, and investor positioning.</p>
      <p>It combines that discussion with separate currency-market analysis
      supplied by the third-party publisher.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2026-02-10/"
            "jpmorgan-strategists-say-ai-fears-overblown"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value != "complete"
    assert "truncated-body" in result.quality.warnings
    assert "BigGo Finance" not in result.plain_text


def test_bloomberg_removes_related_read_more_and_source_link_tails():
    reporting = "".join(
        f"<p>Substantive licensed reporting paragraph {index} contains "
        "enough detail about markets, policy, companies, and investors.</p>"
        for index in range(1, 8)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Licensed Bloomberg Report">
    </head><body><article>
      {reporting}
      <h3>Read more:</h3>
      <ul><li>Unrelated weight-loss drug story</li></ul>
      <p>Source link</p>
      <blockquote>Online Company Registration in India</blockquote>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url="https://www.bloomberg.com/news/articles/2025-05-13/test",
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "paragraph 7" in result.plain_text
    assert "weight-loss drug story" not in result.plain_text
    assert "Source link" not in result.plain_text
    assert "Online Company Registration" not in result.plain_text


def test_bloomberg_drops_unlabelled_related_story_link_paragraph():
    reporting = "".join(
        f"<p>Substantive reporting paragraph {index} covers markets, policy, "
        "companies, investors, and named sources in sufficient detail.</p>"
        for index in range(1, 9)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="South Korea Political Outlook">
    </head><body><section class="article-body">
      {reporting}
      <p>Analysts compared
        <a href="/news/articles/2015-04-01/first-report">the first report</a>
        with
        <a href="/news/articles/2015-04-02/second-report">the second report</a>
        in their assessment.</p>
      <p>
        <a href="http://www.bloombergview.com/articles/2015-04-22/a">
          Unrelated Korea Economy Story</a>
        <a href="http://www.bloomberg.com/news/articles/2015-04-27/b">
          Unrelated Korea Politics Story</a>
        <a href="http://www.bloomberg.com/news/articles/2015-04-09/c">
          Unrelated Korea Crisis Story</a>
      </p>
    </section></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-05-22/"
            "south-korea-political-outlook"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "Analysts compared" in result.plain_text
    assert "Unrelated Korea Economy Story" not in result.plain_text
    assert "Unrelated Korea Politics Story" not in result.plain_text
    assert "Unrelated Korea Crisis Story" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_syndication_trims_linked_source_tail_after_reporting():
    reporting = " ".join(
        [
            "The central bank will acquire Treasury notes with proceeds from "
            "maturing mortgage holdings and support borrowing conditions."
        ]
        * 4
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Fed May Reemerge as Buyer" />
    </head><body><article>
      <p>{reporting}
        <a href="http://www.bloomberg.com/news/2010-08-16/source.html">
          Read more</a>.<br />
        <a href="http://www.bloomberg.com/news/2010-08-16/source.html">
          “Fed May Reemerge as Buyer”</a><br />
        Liz Capo McCormick<br />Bloomberg, August 16, 2010.</p>
      <p><em>Image by <a href="http://www.freedigitalphotos.net/image">
        Francesco Marino / FreeDigitalPhotos.net</a>.</em></p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-08-16/"
            "fed-may-reemerge-as-buyer"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value in {"complete", "partial"}
    assert "maturing mortgage holdings" in result.plain_text
    assert "Read more" not in result.plain_text
    assert "Liz Capo McCormick" not in result.plain_text
    assert "FreeDigitalPhotos.net" not in result.plain_text


def test_bloomberg_syndication_removes_partner_business_social_cta():
    html = """
    <html><head>
      <meta property="og:title" content="Abu Dhabi Fund Plans London Change" />
      <meta property="article:published_time" content="2015-11-09T08:00:00Z" />
    </head><body>
      <article>
        <p>Abu Dhabi's investment authority proposed closing its London
        office after reviewing its international operations.</p>
        <p>The fund said its global investment work would continue.
        -Bloomberg</p>
        <p>Follow Arabian Post</p>
        <p>Select Arabian Post as your preferred source on Google and MSN
        News for trusted business news and Arab politics and updates.</p>
        <p>Follow The National's Business section on Twitter</p>
        <p>mkassem@thenational.ae</p>
        <p>* with Bloomberg</p>
        <p>If you believe you have identified an error or inconsistency in
        this article, please don't hesitate to contact our editorial team at
        editor[at]thearabianpost[dot]com. We are committed to promptly
        addressing any concerns and ensuring the highest level of
        journalistic integrity.</p>
      </article>
    </body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-11-09/"
            "abu-dhabi-investment-authority-proposes-to-close-london-office"
        ),
        allow_generic_syndication=True,
    )

    assert "investment authority proposed" in result.plain_text
    assert "global investment work would continue" in result.plain_text
    assert "Follow The National" not in result.plain_text
    assert "mkassem@thenational.ae" not in result.plain_text
    assert "with Bloomberg" not in result.plain_text
    assert "-Bloomberg" not in result.plain_text
    assert "Follow Arabian Post" not in result.plain_text
    assert "preferred source on Google" not in result.plain_text
    assert "identified an error or inconsistency" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_removes_law_clickthrough_and_chinese_terminal_commands():
    html = """
    <html><head>
      <meta property="og:title" content="European Central Bank Outlook" />
      <meta property="article:published_time" content="2015-05-29T08:00:00Z" />
    </head><body><article>
      <p>标准普尔表示，经济前景改善，但欧洲央行仍可能继续执行量化宽松。</p>
      <p>For the latest lawsuits news, click here.</p>
      <p>Further reporting about the court proceedings remains here.</p>
      <p>相关新闻和信息： 彭博率先报道滚动屏: FIRST &lt;GO&gt;
      中文彭博盘前简报: TNI DAYBOOK BFWCH BBG &lt;GO&gt;</p>
      <p>原文标题 ECB to Likely Continue QE at Least Until Sept. 2016</p>
      <p>For more about Bloomberg BNA, click here. Visit
      www.bloomberg.com/sustainability for the latest from Bloomberg News
      about energy, natural resources and global business.</p>
    </article></body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-05-29/"
            "ecb-likely-to-continue-qe"
        ),
    )

    assert "经济前景改善" in result.plain_text
    assert "Further reporting" in result.plain_text
    assert "latest lawsuits news" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert "原文标题" not in result.plain_text
    assert "Bloomberg BNA" not in result.plain_text
    assert "bloomberg.com/sustainability" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_source_only_figure_is_credit_not_duplicate_caption():
    html = """
    <html><head>
      <meta property="og:title" content="A New Golf Course Opens" />
      <meta property="article:published_time" content="2015-05-27T08:00:00Z" />
    </head><body><article>
      <p>The course opened after years of construction and public review.</p>
      <figure class="inline-image inline-media center">
        <div class="inline-media__unlinked-image">
          <img src="https://media.gotraffic.net/images/course/v1/-1x-1.jpg" />
        </div>
        <figcaption class="inline-media__info">
          <div class="inline-media__caption">
            Source: Trump National Golf Club via Bloomberg
          </div>
          <div class="inline-media__credit">
            Source: Trump National Golf Club via Bloomberg
          </div>
        </figcaption>
      </figure>
      <p>Players tested the course during opening-day ceremonies.</p>
    </article></body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-05-27/"
            "a-new-golf-course-opens"
        ),
    )

    assert len(result.images) == 1
    assert result.images[0].caption is None
    assert result.images[0].credit == (
        "Source: Trump National Golf Club via Bloomberg"
    )
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_hyphen_separator_becomes_divider_block():
    html = """
    <html><head>
      <meta property="og:title" content="A Lesson in Business Ethics" />
      <meta property="article:published_time" content="2015-07-02T08:00:00Z" />
    </head><body><article>
      <p>The opening discussion describes the central ethical question.</p>
      <p>----</p>
      <p>The concluding discussion explains why the lesson still matters.</p>
    </article></body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-07-02/"
            "a-lesson-in-business-ethics"
        ),
    )

    assert "opening discussion" in result.plain_text
    assert "concluding discussion" in result.plain_text
    assert "----" not in result.plain_text
    assert any(block.type.value == "divider" for block in result.blocks)
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_removes_terminal_story_link_and_attachment_control():
    html = """
    <html><head>
      <meta property="og:title" content="Public Views on Financial Reform" />
      <meta property="article:published_time" content="2010-07-13T08:00:00Z" />
    </head><body><article>
      <p>The poll surveyed more than one thousand adults across the U.S.</p>
      <p>To see the methodology and exact wording of the poll questions,
      click on the attachment tab at the top of the story.</p>
      <p>Story link: {NSN L7NUGR6NKMZV&lt;GO&gt;}</p>
    </article></body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-07-13/"
            "public-views-on-financial-reform"
        ),
    )

    assert "surveyed more than one thousand" in result.plain_text
    assert "attachment tab" not in result.plain_text
    assert "Story link" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_removes_terminal_dashboards_and_column_subscription():
    html = """
    <html><head>
      <meta property="og:title" content="Airbus Earnings Outlook" />
      <meta property="article:published_time" content="2014-12-10T08:00:00Z" />
    </head><body><article>
      <p>Airbus expects commercial aircraft demand to keep expanding.</p>
      <p>(The company will host a conference call tomorrow, accessible on
      LIVE &lt;GO&gt;.)</p>
      <p>(Hovnanian Enterprises will hold a conference call at 11 a.m.
      New York time. See {LIVE &lt;GO&gt;}.)</p>
      <p>(Sino-Forest’s conference call at 8:30 a.m. New York time can be
      accessed at {LIVE &lt;GO&gt;}. Callers can dial
      +1-647-427-7450.)</p>
      <p>(Intel held a conference call at 10 a.m. New York time to discuss
      the deal. To listen, click on {LIVE &lt;GO&gt;}.)</p>
      <p>BI AIRM&lt;GO&gt; for commercial aircraft manufacturers’ dashboard
      BI AIRL EU&lt;GO&gt; European airline dashboard BI AIRMG
      INDD&lt;GO&gt; Monthly orders for new aircraft, parked fleet
      statistics</p>
      <p>(To be sent this Nordic Credit column, click here. For more credit
      market news, TOP CM.)</p>
    </article></body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-12-10/"
            "airbus-earnings-outlook"
        ),
    )

    assert "commercial aircraft demand" in result.plain_text
    assert "host a conference call tomorrow.)" in result.plain_text
    assert "Hovnanian Enterprises will hold a conference call" in (
        result.plain_text
    )
    assert "Callers can dial +1-647-427-7450" in result.plain_text
    assert "Intel held a conference call at 10 a.m." in result.plain_text
    assert "To listen, click on" not in result.plain_text
    assert "deal.)" in result.plain_text
    assert "deal..)" not in result.plain_text
    assert "BI AIRM" not in result.plain_text
    assert "Nordic Credit column" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_trims_terminal_event_command_and_social_follow():
    html = """
    <html><head>
      <meta property="og:title" content="Home Depot Profit Rises" />
      <meta property="article:published_time" content="2013-05-21T08:00:00Z" />
    </head><body><article>
      <p>Home Depot reported stronger quarterly profit and sales.</p>
      <p>(Home Depot will hold a conference call for analysts at 9 a.m.
      New York time. Click HD US &lt;Equity&gt; EVTS &lt;GO&gt; to listen.)</p>
      <p>Mark Barton is a presenter on Bloomberg TV. Follow him on Twitter
      @markbartontv</p>
      <p>For more, click here .</p>
      <p>(Richard Vines is a restaurant critic. Opinions expressed are his
      own. Follow him on Twitter @richardvines)</p>
    </article></body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-05-21/"
            "home-depot-profit-rises"
        ),
    )

    assert "hold a conference call" in result.plain_text
    assert "Mark Barton is a presenter" in result.plain_text
    assert "EVTS" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert "Follow him" not in result.plain_text
    assert "@markbartontv" not in result.plain_text
    assert "For more, click here" not in result.plain_text
    assert "Richard Vines is a restaurant critic" in result.plain_text
    assert "@richardvines" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_inline_terminal_event_and_read_summaries_commands():
    html = """
    <html><head>
      <meta property="og:title" content="Terminal command cleanup" />
      <meta property="article:published_time" content="2015-01-15T08:00:00Z" />
    </head><body><article>
      <p>Lennar has scheduled a conference call for 11 a.m. New York time.
      See LEN US &lt;Equity&gt; EVT &lt;GO&gt;.</p>
      <p>The following list comprises the most-read Bloomberg News reports.
      See NI READSUMS &lt;GO&gt; for previous most-read lists.</p>
    </article></body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-01-15/"
            "terminal-command-cleanup"
        ),
    )

    assert "conference call for 11 a.m." in result.plain_text
    assert "most-read Bloomberg News reports" in result.plain_text
    assert "LEN US" not in result.plain_text
    assert "READSUMS" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert "time..)" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_drops_gasgoo_services_and_current_news_widgets():
    html = """
    <html><head>
      <meta property="og:title" content="Lexus sales report" />
      <meta property="article:published_time" content="2011-04-02T08:00:00Z" />
    </head><body><article>
      <p>Luxury vehicle sales rose as manufacturers reported stronger demand.
      The figures exclude non-luxury models.</p>
      <div class="mt-8 border bg-card"><p>Gasgoo offers news and insight.
      Buyer service: buyer-support@gasgoo.com Seller Service:
      seller-support@gasgoo.com</p></div>
      <p>All Rights Reserved. Do not reproduce, copy and use the editorial
      content without permission. Contact us: autonews@gasgoo.com</p>
      <div class="grid"><h4>Weekly Highlights | New Cars</h4>
      <p>[Gasgoo Express] Unrelated current auto-industry news</p></div>
    </article></body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-04-02/"
            "lexus-sales-report"
        ),
    )

    assert "Luxury vehicle sales rose" in result.plain_text
    assert "buyer-support@gasgoo.com" not in result.plain_text
    assert "All Rights Reserved" not in result.plain_text
    assert "Weekly Highlights" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_trims_college_schedule_clickthrough_only():
    html = """
    <html><head>
      <meta property="og:title" content="Week Ahead Events" />
      <meta property="article:published_time" content="2014-12-04T08:00:00Z" />
    </head><body><article>
      <p>Alabama faces Missouri in the conference championship game.
      Click here for other college football game schedules.</p>
      <p>Australian officials release a financial-system report Sunday.</p>
    </article></body></html>
    """

    result = parse_article(
        html.encode(),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-12-04/"
            "week-ahead-events"
        ),
    )

    assert "Alabama faces Missouri" in result.plain_text
    assert "Australian officials" in result.plain_text
    assert "football game schedules" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_drops_midstory_read_next_list_but_keeps_later_reporting():
    opening = "".join(
        f"<p>Opening reporting paragraph {index} covers negotiations, policy, "
        "officials, markets, and the economic outlook in detail.</p>"
        for index in range(1, 6)
    )
    closing = "".join(
        f"<p>Later reporting paragraph {index} covers the submitted proposal, "
        "tax rates, pensions, and central bank funding in detail.</p>"
        for index in range(1, 6)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Creditors Prepare Greece Proposal">
    </head><body><section class="article-body">
      {opening}
      <h2>For more on Greece, read this next:</h2>
      <ul>
        <li><strong><a href="/news/articles/2015-06-02/bank-buffer">
          Greece Redirecting European Bank-Buffer Funds</a></strong></li>
        <li><a href="/news/articles/2015-06-01/leaders">
          European Leaders Discuss Greece</a></li>
        <li><a href="http://www.bloombergview.com/quicktake/greece">
          QuickTake: Greece's Fiscal Odyssey</a></li>
      </ul>
      <h2>Greek Document</h2>
      {closing}
    </section></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-06-02/"
            "creditors-prepare-greece-proposal"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "Opening reporting paragraph 5" in result.plain_text
    assert "For more on Greece" not in result.plain_text
    assert "Greece Redirecting European Bank-Buffer Funds" not in result.plain_text
    assert "Greek Document" in result.plain_text
    assert "Later reporting paragraph 5" in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_partner_story_tags_do_not_leak_into_reporting():
    reporting = "".join(
        f"<p>Reporting paragraph {index} covers the lender, borrowers, "
        "regulation, interest rates, and financial results in detail.</p>"
        for index in range(1, 8)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Microlender Sees Bigger Market">
    </head><body><div class="content">
      <div class="story-body">{reporting}
        <p>Reporting paragraph 8 preserves the final substantive sentence.
          <b>Bloomberg</b></p>
      </div>
      <div class="author-box"><strong>Anto Antony</strong></div>
      <div class="story-tags"><p>Topics:
        <a href="/Search/Link/Keyword/SKS">SKS Microfinance</a>
        <a href="/Search/Link/Keyword/RBI">RBI</a>
      </p></div>
    </div></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-10-05/"
            "microlender-sees-bigger-market"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "Reporting paragraph 8" in result.plain_text
    assert "Topics:" not in result.plain_text
    assert not result.plain_text.rstrip().endswith("RBI")
    assert not result.plain_text.rstrip().endswith("Bloomberg")
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_view_tilde_separator_becomes_divider_block():
    opening = "".join(
        f"<p>Opening paragraph {index} discusses blogs, finance, media, "
        "authors, and public debate in substantive detail.</p>"
        for index in range(1, 5)
    )
    closing = "".join(
        f"<p>Closing paragraph {index} discusses commentary, readers, "
        "publishing, and market analysis in substantive detail.</p>"
        for index in range(1, 5)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="What Blogging Accomplished">
    </head><body><article class="article-body">
      {opening}
      <p>~~~</p>
      {closing}
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/opinion/articles/2014-01-13/"
            "what-blogging-accomplished"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "Opening paragraph 4" in result.plain_text
    assert "Closing paragraph 4" in result.plain_text
    assert "~~~" not in result.plain_text
    assert any(block.type.value == "divider" for block in result.blocks)
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_zillow_guest_article_drops_tail_recirculation():
    reporting = "".join(
        f"<p>Housing paragraph {index} compares prices, square footage, "
        "neighborhoods, schools, and local home values in detail.</p>"
        for index in range(1, 8)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Price of Midsize Homes">
    </head><body><div class="article-body">
      {reporting}
      <p>The final Nashville property is on a quiet tree-lined street.</p>
      <p><strong>To find mid-size homes for sale near you,
        <a href="https://www.zillow.com/homes/">click here</a> and enter
        your city and state into the search bar.</strong></p>
      <p><strong>Related items from
        <a href="https://www.zillow.com/blog">Zillow Blog</a>:</strong></p>
      <ul>
        <li><a href="https://www.zillow.com/blog/median-home/">
          How Much House Can You Get?</a></li>
        <li><a href="https://www.zillow.com/blog/bargains/">
          Budget-Friendly Bargains</a></li>
      </ul>
      <p><em>Catherine Sherman, a real estate writer for Zillow Blog,
        covers industry trends. Read more of her work
        <a href="https://www.zillow.com/blog/author/catherine/">here</a>.
      </em></p>
    </div></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-11-19/"
            "price-of-midsize-homes"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "Housing paragraph 7" in result.plain_text
    assert "quiet tree-lined street" in result.plain_text
    assert "To find mid-size homes" not in result.plain_text
    assert "Related items from" not in result.plain_text
    assert "Budget-Friendly Bargains" not in result.plain_text
    assert "real estate writer for Zillow Blog" in result.plain_text
    assert "Read more of her work" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_drops_terminal_nsn_story_commands_with_titles():
    reporting = "".join(
        f"<p>Health-insurance paragraph {index} reports enrollment, "
        "earnings, forecasts, and government programs in detail.</p>"
        for index in range(1, 9)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Insurer Raises Forecast">
    </head><body><section class="article-body">
      {reporting}
      <p>FIFW NSN NE82I36K50Y4&lt;GO&gt;
        Obamacare Faces New Threat as Court Weighs Appeal</p>
      <p>NSN NE86OT6S972A &lt;GO&gt;
        Rival Boosts Profit Forecast as Enrollment Rises</p>
    </section></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-10-30/"
            "insurer-raises-forecast"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "Health-insurance paragraph 8" in result.plain_text
    assert "FIFW NSN" not in result.plain_text
    assert "NE82I36K50Y4" not in result.plain_text
    assert "Court Weighs Appeal" not in result.plain_text
    assert "Rival Boosts Profit Forecast" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_drops_other_coverage_clickthrough():
    reporting = "".join(
        f"<p>Bankruptcy paragraph {index} reports proceedings, creditors, "
        "court filings, financing, and restructuring in detail.</p>"
        for index in range(1, 9)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Bankruptcy and Restructuring News">
    </head><body><section class="article-body">
      {reporting}
      <p>The airline will honor its interline agreements.
        For other Bloomberg coverage, click here.</p>
      <p>The trustee filed a final motion in federal court.</p>
    </section></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-11-09/"
            "bankruptcy-and-restructuring-news"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "Bankruptcy paragraph 8" in result.plain_text
    assert "airline will honor its interline agreements" in result.plain_text
    assert "For other Bloomberg coverage" not in result.plain_text
    assert "trustee filed a final motion" in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_drops_standalone_partner_credit_and_internal_slug():
    reporting = "".join(
        f"<p>Licensed reporting paragraph {index} covers company strategy, "
        "market prices, executives, and investment plans in detail.</p>"
        for index in range(1, 9)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Company Investment Plans">
    </head><body><article class="article-body">
      {reporting}
      <p><strong>Bloomberg</strong></p>
      <p>bc-icahn-cook</p>
      <p>bc-autos-lincoln (TPN)</p>
      <p>Read more posts from <a href="/view/the-ticker/">The Ticker</a>.</p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-08-22/"
            "company-investment-plans"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "reporting paragraph 8" in result.plain_text
    assert not result.plain_text.rstrip().endswith("Bloomberg")
    assert "bc-icahn-cook" not in result.plain_text
    assert "bc-autos-lincoln" not in result.plain_text
    assert "Read more posts from" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_drops_standalone_terminal_market_commands():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Commodities decline">
      <meta property="article:published_time"
            content="2013-02-21T04:00:00Z">
    </head><body><article>
      <p>European emission permits climbed as traders assessed the outlook
      for industrial demand and changes to regional supply.</p>
      <p>EU Carbon Emissions: NI ECBMKT</p>
      <h2>Livestock</h2>
      <p>Hog futures fell on signs of weakening overseas demand for pork,
      while cattle and feeder-cattle contracts also declined.</p>
      <p>Livestock markets: NI LVMKTS</p>
      <p>All the tennis news and results can be found at TENN &lt;GO&gt;.</p>
      <p>For Related News and Information:<br>
      <meta itemprop="type" content="StoryLink">
      GE Said to Offer Alstom Assets to Ansaldo for EU Deal Nod<br>
      Top Stories:
      <meta itemprop="type" content="FunctionLink">
      TOP</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-02-21/"
            "commodities-at-close"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "European emission permits climbed" in result.plain_text
    assert "Hog futures fell" in result.plain_text
    assert "NI ECBMKT" not in result.plain_text
    assert "NI LVMKTS" not in result.plain_text
    assert "TENN <GO>" not in result.plain_text
    assert "GE Said to Offer Alstom Assets" not in result.plain_text
    assert "Top Stories: TOP" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_drops_legacy_court_roundup_clickthroughs():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Companies in court news">
      <meta property="article:published_time"
            content="2010-04-05T04:00:00Z">
    </head><body><article>
      <h2>New Suits</h2>
      <p>The company was sued over unpaid obligations in state court.</p>
      <p>For more new suits news from last week, click here. For copies
      of recent civil complaints, click here.</p>
      <h2>Court Filings</h2>
      <p>The criminal case was the most-read litigation docket last week.</p>
          <p>To read more of this story, click here.</p>
          <p>To read the story, click here.</p>
          <p>To read Bloomberg coverage, click here.</p>
          <p>For a slideshow on the best dishes, click here.</p>
      <p>The bank bailout hearing will be held today.
          To read Bloomberg coverage, click here.</p>
      <p>Quigley received permission to extend its financing agreement.
      To read more coverage about Quigley, click here and click here.</p>
      <p>The case is U.S. v. Example, 10-cr-100, U.S. District Court.</p>
      <h2>Default News</h2>
      <p>Local Insight debt fell after a possible covenant breach.</p>
      <p>For more verdict and settlement news from last week, click here.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-04-05/"
            "companies-in-court-news"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "company was sued" in result.plain_text
    assert "most-read litigation docket" in result.plain_text
    assert "The case is U.S. v. Example" in result.plain_text
    assert "The bank bailout hearing will be held today." in result.plain_text
    assert "Quigley received permission" in result.plain_text
    assert "Default News" in result.plain_text
    assert "Local Insight debt fell" in result.plain_text
    assert "read more coverage about Quigley" not in result.plain_text
    assert "click here" not in result.plain_text
    assert "news from last week" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_author_bio_drops_handleless_twitter_prompt():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="The future is bright">
      <meta property="article:published_time"
            content="2012-12-16T04:00:00Z">
    </head><body><article>
      <p>New technologies may improve daily life even when familiar
      predictions about flying cars do not arrive on schedule.</p>
      <p>A second reporting paragraph considers the economic forces that
      determine which inventions reach consumers.</p>
      <p>(Virginia Postrel is a Bloomberg View columnist. She is the author
      of The Future and Its Enemies and is writing a book on glamour.
      Follow her on Twitter. The opinions expressed are her own.)</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-12-16/"
            "no-flying-cars-but-the-future-is-bright"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Virginia Postrel is a Bloomberg View columnist" in result.plain_text
    assert "The opinions expressed are her own" in result.plain_text
    assert "Follow her on Twitter" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_drops_social_bio_prefix_and_related_story_paragraph():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="A cultural and political roundup">
      <meta property="og:url"
            content="https://www.bloomberg.com/news/articles/2015-07-10/cultural-political-roundup">
      <meta property="article:published_time"
            content="2015-07-10T04:00:00Z">
    </head><body><article>
      <p>The article reports on the event and explains why the decision
      matters to participants and observers.</p>
      <p>Editors interviewed designers, officials, and residents to compare
      the available evidence and describe how the changes may affect the
      wider market over the coming year.</p>
      <p>The reporting also examines financing, production schedules,
      consumer demand, and the practical constraints facing each proposal.
      Participants said the final outcome will depend on costs, regulation,
      and whether the public continues to support the project.</p>
      <p>Analysts cautioned that early forecasts remain uncertain, although
      several recent transactions provide useful comparisons for evaluating
      the expected benefits and risks.</p>
      <p>Read more Echoes <a href="/view/echoes/">online</a>.
      (Kirsten Salyer is social media editor for Bloomberg View.
      Follow her on Twitter.)</p>
      <p>Jeremy Allen is the photo editor for Bloomberg Pursuits.
      Follow him on Instagram at: <a href="https://instagram.com/editor/">
      @editor</a>.</p>
      <p>Related Story:
      <a href="https://www.bloomberg.com/news/articles/2015-07-01/one">
      First unrelated headline</a>
      <a href="https://www.bloombergview.com/articles/2015-07-02/two">
      Second unrelated headline</a></p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-07-10/"
            "cultural-political-roundup"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "article reports on the event" in result.plain_text
    assert "Kirsten Salyer is social media editor" in result.plain_text
    assert "Jeremy Allen is the photo editor" in result.plain_text
    assert "Read more Echoes online" not in result.plain_text
    assert "Follow her on Twitter" not in result.plain_text
    assert "Follow him on Instagram" not in result.plain_text
    assert "Related Story" not in result.plain_text
    assert "unrelated headline" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_generic_syndication_drops_damaged_joint_byline():
    reporting = " ".join(
        [
            "The cable groups agreed to combine after negotiations covering "
            "valuation, broadband customers, financing, and regulatory review."
        ]
        * 8
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Cable Groups Agree to Combine">
      <meta property="og:url"
            content="https://www.ocregister.com/2015/05/26/cable-deal/">
    </head><body><article>
      <div class="article-body"><div class="body-copy">
        <p>{reporting}</p>
        <p>Bloomberg News and</p>
      </div></div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-05-26/cable-deal"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "regulatory review" in result.plain_text
    assert "Bloomberg News and" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_mql5_syndication_selects_only_post_content():
    reporting = "".join(
        f"<div>Licensed report paragraph {index} explains German investor "
        "confidence, emerging markets, exports, and economic forecasts."
        + (
            '<a href="/en/signals/111434#!tab=history">'
            "https://www.mql5.com/en/signals/111434</a>"
            if index == 8
            else ""
        )
        + "</div>"
        for index in range(1, 9)
    )
    html = f"""
    <html><head>
      <meta property="og:title"
            content="German Investor Confidence Damped by Emerging Markets">
      <meta property="og:url"
            content="https://www.mql5.com/en/blogs/post/649202">
    </head><body>
      <article class="body" id="bodyContent">
        <div class="left-panel">All Blogs Analytics Trading Systems</div>
        <div class="postContent view">
          <div class="container"><div class="content">{reporting}</div></div>
        </div>
        <div class="twoColumns limited">
          <h2>Unrelated trading robot recommendation</h2>
        </div>
        <div class="column right"><ul class="thumbs">
          <li>I Build Gold EAs. Here Is What I Check Before Trusting One</li>
        </ul></div>
      </article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-09-15/"
            "german-investor-confidence"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "report paragraph 8" in result.plain_text
    assert "All Blogs" not in result.plain_text
    assert "trading robot recommendation" not in result.plain_text
    assert "I Build Gold EAs" not in result.plain_text
    assert "/signals/" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_short_partner_paywall_excerpt_is_partial():
    html = b"""
    <html><head>
      <meta property="og:title" content="Hotel Expansion">
      <meta property="og:url"
            content="https://economictimes.indiatimes.com/hotel-expansion">
    </head><body>
      <article class="artData paywall">
        <div class="artText">
          <p>Major global hotel chains are expanding in India while
          consumer spending slows, according to people familiar with
          the plans.</p>
        </div>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url="https://www.bloomberg.com/news/articles/2026-06-09/test",
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings


def test_bloomberg_accepts_complete_legacy_one_paragraph_news_brief():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Ambac Files Amended Reorganization Plan">
      <meta name="description"
            content="Ambac Financial Group Inc. filed an amended
                     reorganization plan in U.S. Bankruptcy Court in
                     New York.">
      <meta name="date" content="2011-09-22T01:17:49Z">
    </head><body>
      <div id="story_content">
        <p><a class="web_ticker">Ambac Financial Group Inc. (ABKFQ)</a>
        filed an amended reorganization plan in U.S. Bankruptcy Court
        in <a href="/new-york/">New York</a>.</p>
        <div id="disqus_thread"></div>
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-09-22/"
            "ambac-files-amended-reorganization-plan"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.body_characters < 120
    assert "structured-short-record" in result.quality.warnings
    assert result.plain_text.endswith("New York .")


def test_bloomberg_accepts_businessweek_magazine_micro_profile():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="What I Wear to Work: Jacqueline Del Rosario">
      <meta name="description"
            content="Dr. Jacquie gauges her mood before selecting a look.">
      <meta property="article:published_time"
            content="2015-08-19T21:20:44Z">
    </head><body>
      <article class="article businessweek"
               itemtype="http://schema.org/Article">
        <meta class="primary-category"
              content="businessweek-magazine">
        <div class="article-body__content">
          <p>Jacqueline Del Rosario, 53, is founder and chief executive
          officer of Recapturing the Vision International in Miami.</p>
        </div>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-08-19/"
            "what-i-wear-to-work-dr-jacquie"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.body_characters < 150
    assert result.quality.warnings == ["structured-short-record"]
    assert "Recapturing the Vision" in result.plain_text


def test_bloomberg_partner_read_full_article_link_is_partial():
    html = b"""
    <html><head>
      <meta property="og:title" content="China Mobile Uses Hotspots">
      <meta property="og:url"
            content="https://www.partner.example/china-mobile-hotspots">
    </head><body>
      <article class="storyContent">
        <p>China Mobile plans to expand its wireless hotspot network
        after customers switched carriers for faster mobile data.</p>
        <p>Analysts expect the investment program to continue as demand
        for smartphone internet access grows across the country.</p>
        <p>....</p>
        <p><a href="https://www.bloomberg.com/news/articles/2011-01-18/example">
          Read full article here via Bloomberg
        </a></p>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-01-18/example"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "Read full article" not in result.plain_text


def test_bloomberg_partner_inline_read_more_at_link_is_partial():
    html = b"""
    <html><head>
      <meta property="og:title" content="Fashion Retailer CEO Steps Down">
      <meta property="og:url"
            content="https://mr-mag.com/fashion-retailer-ceo/">
    </head><body><article><div class="contentSingle">
      <p>The online fashion retailer said its chief executive will step
      down as the company moves to the next phase of development.
      Read more at <em><a href="https://www.bloomberg.com/news/articles/2015-09-02/example">
      Bloomberg</a></em>.</p>
    </div></article>
    <aside class="left_sidebar widget-area">
      <section class="widget"><p>Join Our Mailing list</p>
      <h3>Daily Commute</h3><a href="/unrelated">Unrelated fashion story</a>
      </section>
    </aside>
    <nav class="post-navigation">Previous Next</nav>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-09-02/example"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "online fashion retailer" in result.plain_text
    assert "Read more at" not in result.plain_text
    assert "Join Our Mailing list" not in result.plain_text
    assert "Daily Commute" not in result.plain_text
    assert "Unrelated fashion story" not in result.plain_text
    assert "Previous Next" not in result.plain_text


def test_bloomberg_short_partner_summary_with_source_link_is_partial():
    html = b"""
    <html><head>
      <meta property="og:title" content="Russia Seeks Asian Grain Market">
      <meta property="og:url"
            content="https://publish.illinois.edu/example/">
    </head><body><article><div class="entry-content">
      <p>Russia plans to create a Far East grain corridor to compete
      with established exporters in Asian markets.</p>
      <p>The industry group is holding a conference to attract investment
      in storage, processing, and transportation infrastructure.</p>
      <p><a href="http://www.bloomberg.com/news/2012-02-22/example.html">
      http://www.bloomberg.com/news/2012-02-22/example.html</a></p>
    </div></article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-02-22/example"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings


def test_bloomberg_wordpress_partner_drops_comment_form():
    reporting = (
        "Russia plans a grain export corridor to compete in Asian markets "
        "while producers invest in transportation infrastructure. "
    ) * 8
    html = f"""
    <html><head>
      <meta property="og:title" content="Russia Seeks Asian Grain Market">
      <meta property="og:url"
            content="https://publish.illinois.edu/example/">
    </head><body><div id="main"><article>
      <div class="entry-content"><p>{reporting}</p></div>
    </article>
    <div id="comments"><h5 class="nocomments">No comments yet.</h5></div>
    <div id="respond" class="comment-respond">
      <h3 class="comment-reply-title">Leave a Reply</h3>
      <form><label>Comment *</label><label>Name (required)</label>
      <label>Email (will not be published) (required)</label></form>
    </div></div></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-02-22/example"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "grain export corridor" in result.plain_text
    assert "No comments yet" not in result.plain_text
    assert "Leave a Reply" not in result.plain_text
    assert "Email (will not be published)" not in result.plain_text


def test_bloomberg_newsbreak_uses_embedded_article_not_feed_cards():
    content = "".join(
        f"<p>Licensed Bloomberg rare-earth report paragraph {index} "
        "contains substantive reporting and sourcing details.</p>"
        for index in range(1, 7)
    )
    payload = json.dumps(
        {
            "props": {
                "pageProps": {
                    "authors": ["Bloomberg News"],
                    "content": f"<body>{content}</body>",
                }
            }
        }
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Rare Earth Producer">
      <meta property="og:url"
            content="https://www.newsbreak.com/news/rare-earth-producer">
    </head><body>
      <main>
        <p>Visible licensed excerpt.</p>
        <section><a href="/unrelated" target="_blank">
          <p class="textoverflow-3">Unrelated airport charging story</p>
        </a></section>
      </main>
      <script id="__NEXT_DATA__" type="application/json">{payload}</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url="https://www.bloomberg.com/news/articles/2026-02-05/test",
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "paragraph 6" in result.plain_text
    assert "airport charging" not in result.plain_text


def test_bloomberg_bias_rating_shell_is_not_marked_complete():
    shell = " ".join(["Automated policy and politician portrayal score."] * 12)
    html = f"""
    <html><head>
      <meta property="og:title" content="Bias Analytics Shell">
      <meta property="og:url" content="https://www.biasly.com/news/example/">
    </head><body><article class="storyContent">
      <p>Bias Rating -36% Somewhat Left Reliability N/A Policy Leaning N/A
      Politician Portrayal 36% Negative</p>
      <p>Create your free account to see the in-depth bias analytics and
      more.</p>
      <p>{shell}</p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2024-11-26/example"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value != "complete"
    assert "Bias Rating" not in result.plain_text


def test_bloomberg_parser_keeps_listen_to_article_as_article():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="A Bloomberg Text Article">
      <meta property="article:published_time"
            content="2020-01-04T12:00:00Z">
    </head><body><article>
      <div class="body-copy-v2">
        <h2>LISTEN TO THIS ARTICLE</h2>
        <div class="share-article-button__4bd0b16b">
          <h2>SHARE THIS ARTICLE</h2>
        </div>
        <audio controls>
          <source type="audio/mpeg"
                  src="https://assets.bwbx.io/s3/readings/example.mp3">
        </audio>
        <p><em>Want to receive this post in your inbox every day?
          Sign up for the newsletter.</em></p>
        <p>{reporting}</p>
        <table><tr><th class="news-rsf-table-string">Related stories</th></tr>
          <tr><td>Unrelated recommended story</td></tr></table>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2020-01-04/"
            "a-bloomberg-text-article"
        ),
    )

    assert result.content_type.value == "article"
    assert result.quality.status.value == "complete"
    assert "Bloomberg reporting sentence." in result.plain_text
    assert "SHARE THIS ARTICLE" not in result.plain_text
    assert "Want to receive this post" not in result.plain_text
    assert "Related stories" not in result.plain_text
    assert any(block.type.value == "embed" for block in result.blocks)


def test_bloomberg_parser_removes_regional_and_economics_newsletter_promos():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="A Bloomberg Economy Article">
      <meta property="article:published_time"
            content="2021-11-25T12:00:00Z">
    </head><body><article>
      <div class="body-copy-v2">
        <p>Sign up for the New Economy Daily newsletter, follow us
        @economics and subscribe to our podcast.</p>
        <p>Sign up for our Middle East newsletter and follow us
        @middleeast for news on the region.</p>
        <ul><li><span>For the best in travel, food, drinks, fashion, cars,
        and life, sign up for the Pursuits newsletter. Delivered weekly.
        </span></li></ul>
        <p>{reporting}</p>
        <p>Customers can sign up for a free subscription to the service.</p>
        <p>Follow @bpolitics for all the latest news, and sign up for our
        daily Balance of Power newsletter.</p>
        <p>(Sign up for the Green Daily newsletter, your best source for
        climate news and insights on the latest in science.)</p>
        <p>Want to go deeper inside the video game business? Sign up for
        Game On, a new weekly newsletter from Bloomberg.</p>
        <p>For a fresh perspective on the stories that matter for Australian
        business and politics, sign up for our weekly newsletter.</p>
        <p>Sign up for our Beyond Brexit weekly newsletter, follow us
        @Brexit and subscribe to our podcast.</p>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2021-11-25/"
            "a-bloomberg-economy-article"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "New Economy Daily newsletter" not in result.plain_text
    assert "Middle East newsletter" not in result.plain_text
    assert "Pursuits newsletter" not in result.plain_text
    assert "sign up for a free subscription" in result.plain_text
    assert "Balance of Power newsletter" not in result.plain_text
    assert "Green Daily newsletter" not in result.plain_text
    assert "Game On, a new weekly newsletter" not in result.plain_text
    assert "fresh perspective on the stories" not in result.plain_text
    assert "Beyond Brexit weekly newsletter" not in result.plain_text


def test_bloomberg_parser_removes_standardized_article_footers():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="A Bloomberg Markets Article">
      <meta property="article:published_time"
            content="2016-06-20T12:00:00Z">
    </head><body><article>
      <div class="body-copy-v2">
        <p>{reporting}</p>
        <p>This column does not necessarily reflect the opinion of
        Bloomberg LP and its owners.</p>
        <p>To contact the editor responsible for this story:
        James Greiff at jgreiff@bloomberg.net</p>
        <p>To contact the author of this story:
        Matt Levine at mlevine51@bloomberg.net</p>
        <p>This column does not necessarily reflect the opinion of the
        editorial board or Bloomberg LP and its owners.</p>
        <p>To contact the senior editor responsible for Bloomberg View’s
        editorials: David Shipley at davidshipley@bloomberg.net.</p>
        <p>THIS TRANSCRIPT MAY NOT BE 100% ACCURATE AND MAY CONTAIN
        MISSPELLINGS. ANY OPINION EXPRESSED IN THE TRANSCRIPT DOES NOT
        NECESSARILY REFLECT THE VIEWS OF BLOOMBERG LP.</p>
        <p>To contact the editors responsible for this story:
        Alan Crawford and Andrew Atkinson.</p>
        <p>To contact the reporters on this story:
        Brian Parkin and Nicholas Brautlecht.</p>
        <p>To contact the authors of this story:
        Tara Lachapelle and Brooke Sutherland.</p>
        <p>For more copyright news, click here.</p>
        <p>For more patent news, click here.</p>
        <p>Click here for web link</p>
        <p>For Related News and Information:</p>
        <p>For Related News and Information:
        Most-read stories: MNI CHINA &lt;GO&gt;</p>
        <p>For more on Bernanke’s speech, click here.</p>
        <p>Link to Company News:{{AAPL US &lt;Equity&gt; CN &lt;GO&gt;}}</p>
        <p>(Jonathan Weil is a Bloomberg View columnist.
        Follow him on Twitter.)</p>
        <p>(Catherine Hickley writes for Muse. The opinions expressed are
        her own. For more Dine &amp; Deal reviews, click here.)</p>
        <p>For related stories
        To see today’s top sports stories, see: {{ISPO &lt;GO&gt;}}.</p>
        <p>Related News and Information:
        Top legal stories: {{TLAW &lt;GO&gt;}}
        Bloomberg legal resources: {{BLAW &lt;GO&gt;}}
        Most read legal stories: {{MNI LAW &lt;GO&gt;}}</p>
        <p>Related News and Information:
        Osborne to Announce Sweeping Devolution for English Cities
        U.K. Election Angst Turns to Pound Euphoria</p>
        <p>----------------------------------------------------------------</p>
        <h2>=======================================================</h2>
        <p>**</p>
        <p>(This column does not necessarily reflect the opinion of
        Bloomberg LP and its owners.)</p>
        <p>This column does not necessarily reflect the opinion of Bloomberg
        View's editorial board or Bloomberg LP, its owners and investors.</p>
        <p>Gernot Wagner writes the Risky Climate column.
        This column does not necessarily reflect the opinion of Bloomberg LP
        and its owners.</p>
        <div>Want to receive this post, and more, into your inbox every
        morning? Sign up here</div>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2016-06-20/"
            "a-bloomberg-markets-article"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Bloomberg reporting sentence." in result.plain_text
    assert "does not necessarily reflect" not in result.plain_text
    assert "To contact the editor responsible" not in result.plain_text
    assert "To contact the author of this story" not in result.plain_text
    assert "editorial board or Bloomberg LP" not in result.plain_text
    assert "its owners and investors" not in result.plain_text
    assert "senior editor responsible for Bloomberg View" not in result.plain_text
    assert "THIS TRANSCRIPT MAY NOT BE 100% ACCURATE" not in result.plain_text
    assert "editors responsible for this story" not in result.plain_text
    assert "reporters on this story" not in result.plain_text
    assert "authors of this story" not in result.plain_text
    assert "For more copyright news" not in result.plain_text
    assert "For more patent news" not in result.plain_text
    assert "Click here for web link" not in result.plain_text
    assert "For Related News and Information" not in result.plain_text
    assert "For more on Bernanke" not in result.plain_text
    assert "Link to Company News" not in result.plain_text
    assert "Jonathan Weil is a Bloomberg View columnist." in result.plain_text
    assert "Follow him on Twitter" not in result.plain_text
    assert "Catherine Hickley writes for Muse." in result.plain_text
    assert "Dine & Deal reviews" not in result.plain_text
    assert "top sports stories" not in result.plain_text
    assert "Related News and Information" not in result.plain_text
    assert "----------------------------------------------------------------" not in result.plain_text
    assert "=======================================================" not in result.plain_text
    assert "\n**\n" not in f"\n{result.plain_text}\n"
    assert "Risky Climate column." in result.plain_text
    assert "Want to receive this post, and more" not in result.plain_text


def test_bloomberg_parser_preserves_bbg_embed_as_embed_block():
    html = b"""
    <html><head>
      <meta property="og:title" content="Osborne Discusses EU Membership">
      <meta property="article:published_time"
            content="2015-11-11T12:00:00Z">
    </head><body><article><div class="body-copy">
      <p>U.K. Chancellor George Osborne discussed the importance of
      European Union membership for London financial markets.</p>
      <p>He said an agreement could be reached after negotiations.</p>
      <p><a class="bbg-embed"
        href="https://twitter.com/George_Osborne/status/664385505946181632"
        data-type="iframe"
        data-embed-id="bbg://iframely/twitter-status">
        https://twitter.com/George_Osborne/status/664385505946181632
      </a></p>
    </div></article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-11-11/"
            "osborne-discusses-eu-membership"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "https://twitter.com/" not in result.plain_text
    embeds = [block for block in result.blocks if block.type.value == "embed"]
    assert len(embeds) == 1
    assert embeds[0].embed_url == (
        "https://twitter.com/George_Osborne/status/664385505946181632"
    )


def test_bloomberg_parser_removes_legacy_watch_next_video():
    html = b"""
    <html><head>
      <meta property="og:title" content="Apple Shows Off Updated iPad">
      <meta property="article:published_time"
            content="2016-03-21T14:00:00Z">
    </head><body><section itemprop="articleBody">
      <div class="article-body__content">
        <p>Apple showed off a new tablet with features from its larger model
        while executives discussed demand for portable computers.</p>
        <p>The company said customers could order the device later this week,
        with deliveries expected to begin before the end of the month.</p>
        <h2><strong>Watch This Next: Why Apple Has a Big iPhone
        Problem</strong></h2>
        <figure class="inline-video inline-media center">
          <img src="https://assets.bwbx.io/related-video.jpg">
          <figcaption>Here's Why Apple Has a Big iPhone Problem</figcaption>
        </figure>
        <h2>Watch Next: Facebook, Dollars and Sneakers</h2>
        <figure class="inline-video inline-media center">
          <img src="https://assets.bwbx.io/second-related-video.jpg">
          <figcaption>Facebook, Dollars and Sneakers</figcaption>
        </figure>
      </div>
    </section></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2016-03-21/"
            "apple-shows-off-updated-ipad"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Watch This Next" not in result.plain_text
    assert "Watch Next" not in result.plain_text
    assert "Here's Why Apple" not in result.plain_text
    assert "Facebook, Dollars and Sneakers" not in result.plain_text
    assert all(
        "related-video" not in image.original_url
        for image in result.images
    )


def test_bloomberg_parser_removes_legacy_inline_newsletter_nested_in_paragraph():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="A Legacy Bloomberg Article">
      <meta property="article:published_time"
            content="2017-02-20T12:00:00Z">
    </head><body>
      <div class="body-copy">
        <p>Opening article paragraph.</p>
        <p><aside class="inline-newsletter">
          <div class="inline-newsletter__main">
            <div class="inline-newsletter__content">
              The most important market news of the day.
              Get our markets daily newsletter. Sign Up
            </div>
          </div>
        </aside>{reporting}</p>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2017-02-20/"
            "legacy-inline-newsletter"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Opening article paragraph." in result.plain_text
    assert "Bloomberg reporting sentence." in result.plain_text
    assert "markets daily newsletter" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_recovers_legacy_feature_landing_page():
    intro = " ".join(
        [
            "The entertainment business rewards unusual partnerships, "
            "ambitious productions and creative risks across the industry."
        ]
        * 8
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="The Showbiz Issue 2017">
      <meta property="article:published_time"
            content="2017-04-24T00:00:00Z">
    </head><body>
      <div class="dvz-content2">
        <div class="introWrap"><div class="intro">{intro}</div></div>
        <div class="index">
          <a href="/features/story-one">This Is Spinal Tap’s Lawsuit</a>
          <a href="/features/story-two">The Celebrity Techsplainer</a>
          <a href="/features/story-three">A Theme Park Empire</a>
        </div>
        <div class="cover">
          <img src="https://www.bloomberg.com/features/showbiz/cover.gif">
          <p>Featured in Bloomberg Businessweek.</p>
        </div>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url="https://www.bloomberg.com/features/2017-showbiz-issue",
    )

    assert result.quality.status.value == "complete"
    assert "entertainment business rewards" in result.plain_text
    assert "Celebrity Techsplainer" in result.plain_text
    assert result.quality.images_selected == 1


def test_bloomberg_parser_recovers_legacy_feature_story():
    html = """
    <html><head>
      <title>Two Filipino Doctors Find the American Dream</title>
      <meta property="article:published_time"
            content="2016-09-15T00:00:00Z">
    </head><body>
      <section class="dvz-page-wrapper dvz-feature">
        <div class="feature-wrapper">
          <p class="intro fullW">{intro}</p>
          <h1 class="padded">Rommel Go</h1>
          <div class="feature-image lazy paddedsm">
            <div class="photo"><img src="../img/doctors/doctor-one.jpg"></div>
            <p>Photographer: Wes Frazer for Bloomberg Businessweek</p>
          </div>
          <p class="sectionbreak">{rommel}</p>
          <h1 class="padded">Maria Rabin-Go</h1>
          <div class="feature-image lazy paddedsm">
            <div class="photo"><img src="../img/doctors/doctor-two.jpg"></div>
            <p>{maria}</p>
          </div>
        </div>
      </section>
    </body></html>
    """.format(
        intro=" ".join(["The doctors built a rural Alabama practice."] * 12),
        rommel=" ".join(["Patients gradually learned to trust him."] * 12),
        maria=" ".join(["The first years in Alabama were hard."] * 12),
    ).encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/features/2016-america-divided/"
            "immigrant-doctors"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "rural Alabama practice" in result.plain_text
    assert "first years in Alabama" in result.plain_text
    assert result.quality.images_selected == 2


def test_bloomberg_parser_removes_legacy_brexit_and_podcast_promos():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="Markets Report">
      <meta property="article:published_time"
            content="2016-09-15T00:00:00Z">
    </head><body><article>
      <p>{reporting}</p>
      <p>Substantive Bloomberg introduction.Most Read from BloombergNYC’s
      Most Exciting New RestaurantMore People Call in Sick Today</p>
      <p>Sign up to receive the Brexit Bulletin, a daily briefing on the
      biggest news related to Britain's departure from the EU.</p>
      <p>Subscribe to Bloomberg Benchmark on Pocketcast</p>
      <p>Watch This Next</p>
      <p>©2017 Bloomberg L.P.</p>
      <p>For more articles like this, please visit us at bloomberg.com</p>
      <p>Sign up for China Rising, a new weekly dispatch on where China
      stands now and where it's going next.</p>
      <p>Sign up for our new China newsletter, a new weekly dispatch coming
      soon on where China stands now and where it's going next.</p>
      <p>New to Bloomberg Opinion Today? and follow us on Twitter and
      Facebook.</p>
      <p>Sign up for Bloomberg’s daily technology newsletter here.</p>
      <p>Sign up for Bloomberg’s Business of Sports newsletter for the
      context you need on the collision of power, money and sports.
      Delivered weekly.</p>
      <p>Sign up for the Equality newsletter for weekly reporting from
      Claire Suddath on how gender, race and class are shaping capitalism
      in America and beyond.</p>
      <p>Sign up for the Washington Edition newsletter to find out how the
      worlds of money and politics intersect in the US capital.</p>
      <p>Sign up for the twice-weekly Next Africa newsletter for the latest
      business and economic news from the continent.</p>
      <p>Sign up here for the twice-weekly Next Africa newsletter, and
      subscribe to the Next Africa podcast on Apple or Spotify.</p>
      <p>Or want more Lifestyle and Passion stories? Click here</p>
      <p>Generated by readers, the comments included herein do not reflect
      the views and opinions of Rigzone. All comments are subject to
      editorial review. Off-topic comments will be removed.</p>
      <p>Source : Bloomberg</p>
      <p>Sign up for the India Edition newsletter by Menaka Doshi – an
      insider's guide to the emerging economic powerhouse, delivered
      weekly.</p>
      <p>Want more Bloomberg Opinion? OPIN &lt;GO&gt;. Or you can subscribe
      to our daily newsletter.</p>
      <p>For more Bloomberg Opinion, subscribe to our newsletter.</p>
      <p>Want more from Bloomberg Opinion? OPIN &lt;GO&gt;. Web readers,
      click here. Or subscribe to our daily newsletter.</p>
      <p>​​​​​Want more Bloomberg Opinion? OPIN &lt;GO&gt;. Or you can
      subscribe to our daily newsletter.</p>
      <p>Sign up here and follow us on Threads, TikTok, Twitter, Instagram
      and Facebook.</p>
      <p>Subscribe to The Economic Times Prime and read the ET ePaper
      online.</p>
      <p>Source: https://www.bloomberg.com</p>
      <p>And for a daily wrap of the business, finance and economic stories
      that matter to Australians, from Bloomberg's reporters around the
      globe, sign up to our free Australia Briefing newsletter.This episode
      was produced by Jojo Producer.</p>
      <p>(Catch all the Business News , Breaking News and Latest News
      Updates on The Economic Times .)</p>
      <p>Legitimate closing analysis.More From Bloomberg:</p>
      <ul><li>Unrelated opinion headline</li></ul>
      <h3>More On Bloomberg</h3>
      <p>Read More @ Bloomberg</p>
      <p>You want more news on this market? Click here for a curated First
      Word channel of actionable news from Bloomberg and select sources.
      It can be customized to your preferences.</p>
      <p>Take the MLIV Pulse survey Is betting on early rate cuts a winning
      trade this year? Share your thoughts.</p>
      <p>Continue For Free</p>
      <p>Legitimate technology conclusion.Read next: Unrelated AI story</p>
      <p>Legitimate truck conclusion. Read also:</p>
      <p>You can follow Lev Menand at @LevMenand.</p>
      <p>Follow the market issue situation with our daily updates</p>
      <p>WHAT DO YOU THINK?</p>
      <p>Get in-depth insights from our expert contributors, and dive into
      financial and economic trends</p>
      <p>New US Stocks Insights &amp; Wraps “Equity Insights” are short
      stories on equity market color. The “S&amp;P Month in Review” comes
      on the last day of the month. Click here to see and subscribe.</p>
      <p>Read more stories about where the money flows, and analysis of the
      biggest market stories from Singapore and around the World</p>
      <p>Click here to stay updated with the Latest Business &amp;
      Investment News in Singapore</p>
      <p>Legitimate wind-project report. © 2025 Bloomberg L.P. Subscribe
      for Daily Maritime Insights Sign up for newsletter and never miss an
      update.</p>
      <h3>Did You Miss?</h3>
      <ul><li>Unrelated private-credit headline</li></ul>
      <p>For more on equity markets:</p>
      <ul><li>Unrelated market-wrap headline</li></ul>
      <p>See Also:</p>
      <table><tr><td>Read More on the Topic</td>
      <td>Unrelated defense headline</td></tr></table>
      <table><tr><td>Take the MLIV Pulse survey</td>
      <td>Share your thoughts.</td></tr></table>
      <p>Legitimate opinion conclusion.More From Bloomberg Opinion:</p>
      <ul><li>Unrelated opinion-column headline</li></ul>
      <p>READ: Unrelated Bloomberg recommendation headline</p>
      <p>To view or add a comment, sign in</p>
      <p>Thank you for your report!</p>
      <p>Please enable JavaScript to view this content.</p>
      <p>Advertisement 2</p>
      <p>This commercial has not loaded but, however your article continues
      under.</p>
      <p>Sign In or Create an Account</p>
      <p>and follow us on Twitter and Facebook.</p>
      <p><a href="https://bloombergbusiness.com/join/opinion-signup">Sign up
      here</a> and follow us on Twitter and Facebook.</p>
      <p>Subscribe now to stay ahead with the most trusted business news
      source.</p>
      <p>Facebook, Google News, and Instagram. For our latest videos,
      subscribe to our YouTube channel.</p>
      <p>Catch all the Latest Tech News, Mobile News, Laptop News, Gaming
      news, Wearables News, How To News, also keep up with us on Whatsapp
      channel, Twitter, Facebook, Google News, and Instagram. For our latest
      videos, subscribe to our YouTube channel.</p>
      <div class="email-form">
        <h2>Subscribe for Daily Maritime Insights</h2>
        <p>Sign up for gCaptain’s newsletter and never miss an update</p>
      </div>
      <button class="read-more-button">...Read More</button>
      <section class="inner-page-cta-section">
        <h2>KEEPING THE ENERGY INDUSTRY CONNECTED</h2>
        <p>By subscribing, you agree to our Privacy Policy.</p>
      </section>
      <div class="minimal-detailfull-width-section">
        <h2>Latest Articles</h2>
        <p>Unrelated energy article</p>
      </div>
      <p>To contact the senior editor responsible for Bloomberg Opinion’s
      editorials: Editor Name at editor@example.com.</p>
      <p>To view the source of this information click here.</p>
      <p>Read More: Unrelated Bloomberg recommendation</p>
      <p>Get Early Returns every morning in your inbox. Click here to
      subscribe. Also subscribe to Bloomberg All Access and get much, much
      more. You’ll receive our unmatched global news coverage.</p>
      <p>Want more Bloomberg Opinion? Terminal readers head to OPIN
      &lt;GO&gt;. Web readers click here.</p>
      <p>Most Read from Bloomberg</p>
      <ul><li>Unrelated most-read article</li></ul>
      <div class="commentWrapper"><button>Add Comment</button></div>
      <div class="youMightAlsoLike">You Might Also Like</div>
      <div class="Pbanner">Add as a Reliable News Source</div>
      <div class="relatedKeywords">Read more news on</div>
      <p class="waChannelCta">Join our WhatsApp channel</p>
      <div class="b-share-bar"><button>Share via Email</button></div>
      <p><em>Richard Vines is Chief Food Critic at Bloomberg. Follow him on
      Twitter @richardvines and Instagram @richard.vines.</em></p>
      <ul>
        <li><a title="Click to view webpage.">Unrelated article one</a></li>
        <li><a title="Click to view webpage.">Unrelated article two</a></li>
      </ul>
      <section class="photGallery similarstoryslide">
        <h3>Related Stories</h3>
        <p>Unrelated syndicated recommendation</p>
      </section>
      <h3 id="marketrelated-stories">Market-related stories</h3>
      <p>Unrelated market recommendation</p>
      <p>Prev Post</p>
      <p>Unrelated previous article</p>
      <p>Next Post</p>
      <p>Unrelated next article</p>
      <h3>More on this topic</h3>
      <h3>See more on</h3>
      <p>United States</p>
      <p>Want to see the in-depth bias analytics and more.</p>
      <p>By creating an account, you agree to our Terms and Privacy Policy.</p>
      <p>Log In</p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url="https://www.bloomberg.com/news/articles/2016-09-15/x",
    )

    assert result.quality.status.value == "complete"
    assert "Bloomberg reporting sentence." in result.plain_text
    assert "Substantive Bloomberg introduction." in result.plain_text
    assert "Most Exciting New Restaurant" not in result.plain_text
    assert "Brexit Bulletin" not in result.plain_text
    assert "Pocketcast" not in result.plain_text
    assert "Watch This Next" not in result.plain_text
    assert "Bloomberg L.P." not in result.plain_text
    assert "For more articles like this" not in result.plain_text
    assert "China Rising" not in result.plain_text
    assert "new China newsletter" not in result.plain_text
    assert "New to Bloomberg Opinion Today" not in result.plain_text
    assert "daily technology newsletter" not in result.plain_text
    assert "Business of Sports newsletter" not in result.plain_text
    assert "Equality newsletter" not in result.plain_text
    assert "Washington Edition newsletter" not in result.plain_text
    assert "Next Africa newsletter" not in result.plain_text
    assert "Lifestyle and Passion stories" not in result.plain_text
    assert "views and opinions of Rigzone" not in result.plain_text
    assert "Source : Bloomberg" not in result.plain_text
    assert "India Edition newsletter" not in result.plain_text
    assert "subscribe to our daily newsletter" not in result.plain_text
    assert "For more Bloomberg Opinion" not in result.plain_text
    assert "follow us on Threads" not in result.plain_text
    assert "Economic Times Prime" not in result.plain_text
    assert "Source: https://www.bloomberg.com" not in result.plain_text
    assert "Australia Briefing newsletter" not in result.plain_text
    assert "This episode was produced by Jojo Producer." in result.plain_text
    assert "Catch all the Business News" not in result.plain_text
    assert "Legitimate closing analysis." in result.plain_text
    assert "Unrelated opinion headline" not in result.plain_text
    assert "More On Bloomberg" not in result.plain_text
    assert "Read More @ Bloomberg" not in result.plain_text
    assert "curated First Word channel" not in result.plain_text
    assert "MLIV Pulse survey" not in result.plain_text
    assert "Continue For Free" not in result.plain_text
    assert "Legitimate technology conclusion." in result.plain_text
    assert "Unrelated AI story" not in result.plain_text
    assert "Legitimate truck conclusion." in result.plain_text
    assert "You can follow Lev Menand" not in result.plain_text
    assert "market issue situation" not in result.plain_text
    assert "WHAT DO YOU THINK" not in result.plain_text
    assert "expert contributors" not in result.plain_text
    assert "New US Stocks Insights" not in result.plain_text
    assert "where the money flows" not in result.plain_text
    assert "Latest Business & Investment News" not in result.plain_text
    assert "Legitimate wind-project report." in result.plain_text
    assert "Daily Maritime Insights" not in result.plain_text
    assert "Unrelated private-credit headline" not in result.plain_text
    assert "Unrelated market-wrap headline" not in result.plain_text
    assert "See Also" not in result.plain_text
    assert "Unrelated defense headline" not in result.plain_text
    assert "MLIV Pulse survey" not in result.plain_text
    assert "Legitimate opinion conclusion." in result.plain_text
    assert "Unrelated opinion-column headline" not in result.plain_text
    assert "Unrelated Bloomberg recommendation headline" not in result.plain_text
    assert "To view or add a comment" not in result.plain_text
    assert "Thank you for your report" not in result.plain_text
    assert "enable JavaScript" not in result.plain_text
    assert "Advertisement 2" not in result.plain_text
    assert "commercial has not loaded" not in result.plain_text
    assert "Sign In or Create an Account" not in result.plain_text
    assert "follow us on Twitter" not in result.plain_text
    assert "most trusted business news source" not in result.plain_text
    assert "latest videos" not in result.plain_text
    assert "Catch all the Latest Tech News" not in result.plain_text
    assert "Daily Maritime Insights" not in result.plain_text
    assert "Read More" not in result.plain_text
    assert "ENERGY INDUSTRY CONNECTED" not in result.plain_text
    assert "Latest Articles" not in result.plain_text
    assert "Bloomberg Opinion’s editorials" not in result.plain_text
    assert "source of this information" not in result.plain_text
    assert "Unrelated Bloomberg recommendation" not in result.plain_text
    assert "Get Early Returns" not in result.plain_text
    assert "Want more Bloomberg Opinion" not in result.plain_text
    assert "Unrelated most-read article" not in result.plain_text
    assert "Add Comment" not in result.plain_text
    assert "You Might Also Like" not in result.plain_text
    assert "Reliable News Source" not in result.plain_text
    assert "WhatsApp channel" not in result.plain_text
    assert "Share via Email" not in result.plain_text
    assert "Chief Food Critic" not in result.plain_text
    assert "Unrelated article one" not in result.plain_text
    assert "Unrelated syndicated recommendation" not in result.plain_text
    assert "Unrelated market recommendation" not in result.plain_text
    assert "Unrelated previous article" not in result.plain_text
    assert "Unrelated next article" not in result.plain_text
    assert "More on this topic" not in result.plain_text
    assert "United States" not in result.plain_text
    assert "in-depth bias analytics" not in result.plain_text
    assert "Terms and Privacy Policy" not in result.plain_text


def test_nyt_parser_recovers_legacy_listings_rendered_outside_article():
    entries = "".join(
        f"""
        <li>
          <h4>Dance Event {index}</h4>
          <p>{"A detailed critical preview of the coming performance. " * 8}</p>
          <img src="https://graphics8.nytimes.com/images/dance-{index}.jpg">
        </li>
        """
        for index in range(1, 7)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Fall Arts Preview - Dance">
      <meta property="article:published_time" content="2014-09-04">
    </head><body>
      <div class="shell"><article class="story theme-interactive">
        <div class="interactive-graphic"><div id="g-graphic"></div></div>
      </article></div>
      <div class="control-width"><div class="listings listings-dance">
        <ol>{entries}</ol>
      </div></div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2014/09/04/arts/"
            "fall-arts-preview-times-100-calendar-dance-events.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Dance Event 6" in result.plain_text
    assert "detailed critical preview" in result.plain_text
    assert result.quality.images_selected == 6


def test_nyt_parser_recovers_standalone_legacy_contribution_form():
    instructions = " ".join(
        ["Position the pin and tell us where your story takes place."] * 8
    )
    legal = " ".join(
        ["By submitting, you confirm that the contribution is original."] * 8
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Show Us Your Memorable Walk in NYC">
      <meta property="article:published_time" content="2015-04-20">
    </head><body>
      <div id="g-graphic" class="g-form stage-1">
        <h1>Show Us Your Memorable Walk in NYC</h1>
        <div class="g-intro-text"><span>{instructions}</span></div>
        <div class="g-form-group"><h3>Share your story</h3>
          <form><textarea name="story"></textarea>
            <p class="g-form-legal">{legal}</p>
          </form>
        </div>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2015/04/20/magazine/"
            "newyorkcity-walks-form.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Share your story" in result.plain_text
    assert "contribution is original" in result.plain_text


def test_nyt_parser_classifies_legacy_documentcloud_page_as_interactive():
    html = """
    <html><head>
      <meta property="og:title" content="Grand Jury Testimony">
      <meta name="description"
            content="The full grand jury testimonies from two witnesses.">
      <meta property="article:published_time" content="2015-08-05">
    </head><body><article class="story theme-interactive">
      <div class="interactive-graphic">
        <script>
          DV.flexLoad("//www.documentcloud.org/documents/2187046-testimony.js",
                      {container: "#viewer"});
        </script>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2015/08/04/opinion/"
            "opdoc-verbatim-testimonies.html"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert any(
        block.embed_url
        == "https://www.documentcloud.org/documents/2187046-testimony"
        for block in result.blocks
    )


def test_wsj_parser_removes_coronavirus_subscription_and_theme_nav():
    reporting = " ".join(["WSJ reporting sentence."] * 35)
    html = f"""
    <html><head>
      <meta property="og:title" content="Coronavirus Research">
      <meta property="article:published_time"
            content="2020-08-09T00:00:00Z">
    </head><body><article><div class="article-content">
      <p>{reporting}</p>
      <div class="media-object type-InsetRichText inline article__inset">
        <div class="media-object-rich-text">
          <h4>STAY INFORMED</h4>
          <p>Get a coronavirus briefing six days a week, and a weekly
          Health newsletter once the crisis abates: Sign up here.</p>
        </div>
      </div>
      <div class="media-object type-InsetDynamic article__inset">
        <div class="theme-nav-wrapper">
          <p>Coronavirus</p><p>Free Resources</p><p>Live Updates</p>
          <p>Symptoms</p><p>Daily Video Briefing</p>
        </div>
      </div>
    </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/coronavirus-research-11596978001"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "WSJ reporting sentence." in result.plain_text
    assert "STAY INFORMED" not in result.plain_text
    assert "Sign up here" not in result.plain_text
    assert "Free Resources" not in result.plain_text
    assert "Daily Video Briefing" not in result.plain_text


def test_ft_parser_removes_acast_privacy_boilerplate_from_podcast():
    html = """
    <html><head>
      <meta property="og:title" content="A European policy podcast">
      <meta property="article:published_time"
            content="2020-11-16T00:00:00Z">
    </head><body><article>
      <audio data-audio-subtype="podcast"
             src="https://rss.acast.com/episode.mp3"></audio>
      <p>EU leaders are facing several policy showdowns this week, and the
      guests explain what is at stake for governments and citizens.</p>
      <p>See acast.com/privacy for privacy and opt-out information.</p>
      <p>A transcript for this podcast is currently unavailable, view our
      accessibility guide.</p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/dbe9efff-1e09-404d-8184-3dadf892d276"
        ),
    )

    assert result.content_type.value == "audio"
    assert result.quality.status.value == "complete"
    assert "policy showdowns" in result.plain_text
    assert "acast.com/privacy" not in result.plain_text
    assert "transcript for this podcast" in result.plain_text


def test_bloomberg_parser_removes_green_daily_and_all_access_promos():
    reporting = " ".join(["Bloomberg climate reporting sentence."] * 30)
    html = f"""
    <html><head>
      <script type="application/ld+json">{{
        "@type": "NewsArticle",
        "headline": "The Rise of Green Finance",
        "datePublished": "2020-03-04T11:00:22Z",
        "description": "Sign up to receive the Green Daily newsletter."
      }}</script>
    </head><body><article><div class="body-copy-v2">
      <p><span class="news-designed-for-consumer-media">
        <a href="https://example.com/signup">Sign up to receive</a>
        the Green Daily newsletter in your inbox every weekday.
      </span></p>
      <p>{reporting}</p>
      <p>Emily Chasan writes about climate-conscious investors.
        <span class="news-designed-for-consumer-media">
          Sign up to receive the Green Daily newsletter in your inbox
          every weekday.
        </span>
      </p>
      <p><span class="news-designed-for-consumer-media">
        For even more: Subscribe to Bloomberg All Access for full global
        news coverage and two in-depth daily newsletters.
      </span></p>
    </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2020-03-04/"
            "the-rise-of-green-finance"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.description is None
    assert "Bloomberg climate reporting sentence." in result.plain_text
    assert "Emily Chasan writes about" in result.plain_text
    assert "Green Daily" not in result.plain_text
    assert "Bloomberg All Access" not in result.plain_text


def test_bloomberg_parser_rejects_newsletter_cta_description():
    html = """
    <html><head>
      <script type="application/ld+json">{
        "@type": "NewsArticle",
        "headline": "Trade and Economic Policy",
        "datePublished": "2020-03-05T11:00:22Z",
        "description": "Want to receive this post in your inbox every day?
        Sign up for the Terms of Trade newsletter."
      }</script>
    </head><body><article><div class="body-copy-v2">
      <p>Substantive Bloomberg economic reporting about trade policy and
      international investment.</p>
      <p>Additional reporting explains the consequences for businesses,
      governments and consumers across several countries.</p>
    </div></article></body></html>
    """.replace(
        "every day?\\n        Sign up",
        "every day? Sign up",
    ).encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2020-03-05/"
            "trade-and-economic-policy"
        ),
    )

    assert result.description is None


def test_reuters_parser_removes_modern_read_more_recirculation():
    reporting = " ".join(["Reuters legal reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="Court considers legal dispute">
      <meta property="article:published_time"
            content="2025-01-03T00:00:00Z">
    </head><body><article><div data-testid="ArticleBody">
      <div data-testid="paragraph-0">{reporting}</div>
      <div data-testid="paragraph-12">Read more:</div>
      <div data-testid="paragraph-13">
        Music publishers sue AI company over song lyrics
      </div>
      <div data-testid="paragraph-14">
        Publishers ask court to halt use of lyrics
      </div>
      <div data-testid="paragraph-15">
        AI company responds to copyright lawsuit
      </div>
      <p data-testid="promo-box">Sign up here.</p>
      <div class="article-body-module__element">Reporting by Jane Doe</div>
    </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/legal/litigation/"
            "court-considers-legal-dispute-2025-01-03"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Reuters legal reporting sentence." in result.plain_text
    assert "Read more:" not in result.plain_text
    assert "Music publishers sue" not in result.plain_text
    assert "Sign up here" not in result.plain_text


def test_reuters_parser_removes_legacy_share_chrome_and_wire_separator():
    canonical_url = (
        "https://www.reuters.com/article/example-idUSL4N0JH0GX20131205"
    )
    html = b"""
        <html><head>
          <meta property="og:title" content="A complete Reuters report">
        </head><body>
          <div class="StandardArticleBody_body">
            <p>The first substantive paragraph contains enough report text.</p>
            <p>^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^</p>
            <p>The final substantive paragraph completes the report.</p>
            <div class="Image_expand-button"
                 aria-label="Expand Image Slideshow" role="button"></div>
            <div class="Slideshow_expand-button" role="button"></div>
            <div class="rich-share" data-testid="rich-share">
              <svg aria-label="Save to Reading list" role="button"></svg>
              <p>Share</p>
            </div>
          </div>
        </body></html>
    """

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=raw_capture("reuters", canonical_url),
    )

    assert "first substantive paragraph" in result.plain_text
    assert "final substantive paragraph" in result.plain_text
    assert "^^^" not in result.plain_text
    assert "Save to Reading list" not in result.body_html
    assert 'role="button"' not in result.body_html


def test_reuters_parser_trims_legacy_graphics_payload_tail():
    canonical_url = "https://www.reuters.com/article/m-rkte-idDEKCN26D1S8"
    html = b"""
        <html><head>
          <meta property="og:title" content="Markets recover">
        </head><body>
          <div class="StandardArticleBody_body">
            <p>The complete market report has substantive reporting here.</p>
            <p>&lt;^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^</p>
            <p>Interactive graphics and related reports</p>
            <p>{"messageType":"graphics:graphic:1","rsf":"copyright"}</p>
            <p>^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^&gt;</p>
          </div>
        </body></html>
    """

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=raw_capture("reuters", canonical_url),
    )

    assert "complete market report" in result.plain_text
    assert "Interactive graphics" not in result.plain_text
    assert "messageType" not in result.plain_text


def test_reuters_parser_removes_gallery_and_partner_button_chrome():
    canonical_url = (
        "https://www.reuters.com/world/example-gallery-2022-11-29"
    )
    html = b"""
      <html><head>
        <meta property="og:title" content="A Reuters gallery report">
      </head><body><article>
        <div data-testid="ArticleBody">
          <p>The report contains substantive text before its photographs.</p>
          <div data-testid="Carousel">
            <div class="carousel-v2__container__1ykSx"
                 role="button" tabindex="0">
              <figure><img src="https://example.com/photo.jpg"
                alt="A news photograph"></figure>
            </div>
            <div class="pagination-v2__container__1nwui"
                 role="button" tabindex="0">1 of 3</div>
          </div>
          <a aria-label="Follow partner on Google News"
             href="https://news.google.com/example"
             role="button" tabindex="0">Follow</a>
          <p>The final paragraph completes the Reuters report.</p>
        </div>
      </article></body></html>
    """

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=raw_capture("reuters", canonical_url),
    )

    assert "final paragraph" in result.plain_text
    assert "1 of 3" not in result.plain_text
    assert "Follow partner" not in result.body_html
    assert 'role="button"' not in result.body_html
    assert "photo.jpg" in result.body_html


def test_nyt_parser_removes_legacy_newsletter_embed():
    reporting = " ".join(["New York real-estate reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="New York City Park Views">
      <meta property="article:published_time"
            content="2014-10-17T00:00:00Z">
    </head><body><article class="story theme-main">
      <div class="story-body"><p>{reporting}</p>
        <figure id="Newsletter-embed-RealEstate"
                class="interactive interactive-embedded">
          <div class="interactive-graphic">
            <div id="d-promo-realestate">
              <h2>Real Estate Newsletter</h2>
              <p>Sign up for weekly updates on residential real estate
              news from The Times.</p>
            </div>
          </div>
        </figure>
        <p>Final reporting paragraph about homes beside the city park.</p>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2014/10/19/realestate/"
            "new-york-city-park-views.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "New York real-estate reporting sentence." in result.plain_text
    assert "Final reporting paragraph" in result.plain_text
    assert "Real Estate Newsletter" not in result.plain_text
    assert "Sign up for weekly updates" not in result.plain_text


def test_bloomberg_parser_merges_caption_matched_low_resolution_lead():
    caption = "Demonstrators stand near railway tracks during a protest."
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="Rail Protest">
      <meta property="og:image"
            content="https://assets.bwbx.io/images/lead/v0/1200x800.jpg">
      <meta property="article:published_time"
            content="2020-02-14T12:00:00Z">
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "Rail Protest",
        "image": {{
          "@type": "ImageObject",
          "url": "https://assets.bwbx.io/images/lead/v0/1200x800.jpg",
          "caption": "{caption}"
        }}
      }}
      </script>
    </head><body><article><div class="body-copy-v2">
      <figure>
        <img src="https://assets.bwbx.io/images/lead/v0/1200x800.jpg">
        <figcaption>{caption}</figcaption>
      </figure>
      <figure>
        <img src="https://assets.bwbx.io/images/placeholder/v0/60x-1.jpg">
        <figcaption>{caption}</figcaption>
      </figure>
      <p>{reporting}</p>
    </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2020-02-14/"
            "rail-protest"
        ),
    )

    assert len(result.images) == 1
    assert result.images[0].role.value == "lead"
    assert result.images[0].original_url.endswith("1200x800.jpg")
    assert any(
        url.endswith("60x-1.jpg")
        for url in result.images[0].candidate_urls
    )


def test_bloomberg_parser_promotes_lazy_body_image_to_best_rendition():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="Bloomberg image report">
      <meta property="article:published_time"
            content="2017-12-12T12:00:00Z">
    </head><body><article><div class="body-copy-v2">
      <figure>
        <img src="https://assets.bwbx.io/images/users/example/photo/v0/60x-1.jpg">
        <figcaption>A factory worker examines a production line.</figcaption>
      </figure>
      <p>{reporting}</p>
    </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2017-12-12/"
            "bloomberg-image-report"
        ),
    )

    assert len(result.images) == 1
    assert result.images[0].original_url.endswith("/1200x-1.jpg")
    assert result.images[0].candidate_urls == [
        "https://assets.bwbx.io/images/users/example/photo/v0/1200x-1.jpg",
        "https://assets.bwbx.io/images/users/example/photo/v0/60x-1.jpg",
    ]


def test_bloomberg_parser_separates_explicit_figure_credit():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="Bloomberg visual report">
      <meta property="article:published_time"
            content="2017-07-29T12:00:00Z">
    </head><body><article><div class="body-copy-v2">
      <figure>
        <img data-native-src="https://assets.bwbx.io/example/-1x-1.png"
             src="https://assets.bwbx.io/example/60x-1.png">
        <figcaption>
          <div class="news-figure-caption-text caption">
            Welcome to the factory floor.
          </div>
          <div class="news-figure-credit credit">Tesla</div>
        </figcaption>
      </figure>
      <p>{reporting}</p>
    </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2017-07-29/"
            "bloomberg-visual-report"
        ),
    )

    assert len(result.images) == 1
    assert result.images[0].caption == "Welcome to the factory floor."
    assert result.images[0].credit == "Tesla"
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_removes_camera_metadata_image_captions():
    html = b"""
    <html><head>
      <meta property="og:title" content="A Vintage Watch Article">
      <meta property="article:published_time"
            content="2014-12-29T12:00:00Z">
    </head><body><article>
      <div class="body-copy-v2">
        <p>Collectors examined two different photographs of the watch
        before discussing the dial and bezel in detail.</p>
        <figure class="inline-image">
          <img src="https://assets.bwbx.io/images/watch-one/v1/-1x-1.jpg"
               alt="OLYMPUS DIGITAL CAMERA">
          <figcaption>OLYMPUS DIGITAL CAMERA</figcaption>
        </figure>
        <p>The first photograph showed the original bezel.</p>
        <figure class="inline-image">
          <img src="https://assets.bwbx.io/images/watch-two/v1/-1x-1.jpg"
               alt="OLYMPUS DIGITAL CAMERA">
          <figcaption>OLYMPUS DIGITAL CAMERA</figcaption>
        </figure>
        <p>The second photograph showed the aged dial.</p>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-12-29/"
            "a-vintage-watch-article"
        ),
    )

    assert result.quality.status.value == "complete"
    assert len(result.images) == 2
    assert all(image.caption is None for image in result.images)
    assert all(image.alt is None for image in result.images)
    assert "OLYMPUS DIGITAL CAMERA" not in result.body_html


def test_bloomberg_parser_prefers_main_story_over_header_live_cards():
    html = b"""
    <html><head>
      <meta property="og:title" content="Main Bloomberg investigation">
      <meta property="article:published_time"
            content="2019-07-18T15:21:25Z">
    </head><body>
      <header>
        <article class="live-now-story">
          <p>Bloomberg Television live programming card that is not part
          of the requested archived news article.</p>
        </article>
      </header>
      <main>
        <article data-story-id="PUUBB06VDKHS01">
          <h1>Main Bloomberg investigation</h1>
          <div>
            <p>The first paragraph contains substantive reporting about
            a financial investigation and the people involved.</p>
            <p>The second paragraph provides court records, responses,
            dates and additional context for the archived story.</p>
          </div>
        </article>
      </main>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2019-07-18/example"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "first paragraph" in result.plain_text
    assert "second paragraph" in result.plain_text
    assert "Television live programming" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_rejects_explicit_teaser_body():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="A 30 Million Hampstead Steal">
      <meta property="article:published_time"
            content="2022-12-03T00:00:00Z">
    </head><body><article data-story-id="teaser">
      <div class="teaser-body__b6065d89">
        <div class="body-content teaser-content__388dc739">
          <p>An occasional glimpse inside the world of selling very
          expensive homes.</p>
          <p>Words fail me. I am blown away by the beauty of this house.</p>
        </div>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2022-12-03/"
            "a-30-million-hampstead-steal"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_trims_professional_subscription_shell():
    html = b"""
    <html><head>
      <meta property="og:title" content="An archived Bloomberg report">
      <meta property="article:published_time"
            content="1997-06-29T00:00:00Z">
    </head><body><article>
      <div class="body-copy-v2">
        <p>The first substantial paragraph explains the report and gives
        readers enough genuine editorial context to identify the story.</p>
        <p>The second substantial paragraph continues the reporting with
        additional facts, analysis, and historical background.</p>
        <p>To continue reading this article you must be a Bloomberg
        Professional Service Subscriber. Read this article on the Terminal
        Request a demo to learn more.</p>
        <p>Recommended terminal products</p>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/1997-06-29/"
            "an-archived-bloomberg-report"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "Professional Service Subscriber" not in result.plain_text
    assert "Recommended terminal products" not in result.plain_text
    assert "second substantial paragraph" in result.plain_text


def test_bloomberg_parser_ignores_hidden_professional_subscription_tout():
    html = b"""
    <html><head>
      <meta property="og:title" content="A complete archived report">
      <meta property="article:published_time"
            content="2010-07-15T00:00:00Z">
    </head><body><article>
      <div class="body-copy-v2">
        <p>The first paragraph reports the terms of the acquisition and
        identifies the companies involved in the transaction.</p>
        <p>The second paragraph explains the financing, market reaction,
        and the strategic reasons management gave for the agreement.</p>
        <p>The final paragraph supplies historical context and confirms
        the remaining timetable for completing the transaction.</p>
        <p>.</p>
      </div>
      <div class="content-cliff-tout-v2" hidden>
        <p>To continue reading this article you must be a Bloomberg
        Professional Service Subscriber.</p>
      </div>
      <aside class="content-type-footer">
        <div class="topic-list">
          <ul><li>Markets</li><li>Debt</li><li>New York</li></ul>
        </div>
      </aside>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-07-15/"
            "a-complete-archived-report"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "truncated-body" not in result.quality.warnings
    assert "final paragraph" in result.plain_text
    assert "Professional Service Subscriber" not in result.plain_text
    assert "\n.\n" not in f"\n{result.plain_text}\n"
    assert "Markets" not in result.plain_text
    assert "New York" not in result.plain_text


def test_bloomberg_parser_removes_legacy_subscription_promos():
    html = b"""
    <html><head>
      <meta property="og:title" content="Bloomberg markets report">
      <meta property="article:published_time"
            content="2017-05-01T00:00:00Z">
    </head><body><article><div class="body-copy-v2">
      <p>The opening paragraph contains substantial original reporting about
      markets, companies, and economic policy around the world.</p>
      <p>Good morning! This is Fly Charts, the daily charts-only newsletter
      from Gadfly; sign up here . From an oil sands disaster, the charts
      explain what investors need to know today.</p>
      <p>Subscribe to Game Plan on iTunes Podcasts</p>
      <p>Subscribe to Game Plan on Pocket Casts</p>
      <p>Follow @Brexit for all the latest news, and sign up to our daily
      Brexit Bulletin newsletter.</p>
      <p>A version of this column originally appeared in Bloomberg's Fully
      Charged technology newsletter. You can sign up here .</p>
    </div></article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2017-05-01/"
            "bloomberg-markets-report"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "sign up here" not in result.plain_text
    assert "Subscribe to Game Plan" not in result.plain_text
    assert "Follow @Brexit" not in result.plain_text
    assert "From an oil sands disaster" in result.plain_text


def test_bloomberg_parser_extracts_legacy_div_span_story_body():
    html = b"""
    <html><head>
      <meta property="og:title" content="Legacy Bloomberg story">
      <meta property="article:published_time"
            content="2018-09-18T15:21:25Z">
    </head><body><article>
      <div class="body-copy-v2">
        <div><span>The first legacy paragraph contains substantive reporting
        about a workplace settlement and enough context for extraction.</span></div>
        <div><span>The second legacy paragraph explains the response, timing,
        evidence and consequences in additional detail for readers.</span></div>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2018-09-18/example"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.body_characters >= 200
    assert len(result.blocks) == 2
    assert "first legacy paragraph" in result.plain_text
    assert "second legacy paragraph" in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_extracts_pre_2015_story_content_and_date():
    html = b"""
    <html><head>
      <title>Legacy Markets Story - Bloomberg</title>
    </head><body>
      <div id="disqus_title"><h1>Legacy Markets Story</h1></div>
      <div id="story_meta"><cite class="byline">By Jane Reporter -
        <span class="datestamp"><script>document.write(dateFormat(
          new Date(1272314505000)))</script>
          <noscript>Mon Apr 26 20:41:45 GMT 2010</noscript>
        </span>
      </cite></div>
      <div id="story_content" class="clearfix">
        <div id="story_social_toolbar_top_container"></div>
        <p>Shares moved sharply after the company published quarterly
        earnings that exceeded analyst expectations in New York.</p>
        <p>Executives said demand remained strong across several markets,
        while investors assessed the outlook for the rest of the year.</p>
        <p>Analysts surveyed by Bloomberg raised their estimates following
        the report and cited improving operating margins.</p>
        <p>The shares closed higher after active trading during the session.</p>
        <p>To contact the reporter on this story: Jane Reporter in New York
        at jreporter@bloomberg.net.</p>
        <ul id="story_social_toolbar_bottom"><li>Share this story</li></ul>
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-04-26/"
            "legacy-markets-story"
        ),
    )

    assert result.headline == "Legacy Markets Story"
    assert result.published_at is not None
    assert result.published_at.year == 2010
    assert result.quality.status.value == "complete"
    assert result.quality.body_characters > 300
    assert "Share this story" not in result.plain_text


def test_bloomberg_parser_scopes_legacy_body_without_right_rail():
    html = b"""
    <html><head>
      <meta property="og:title" content="A Legacy Bloomberg Report">
      <meta property="article:published_time"
            content="2018-01-31T00:00:00Z">
    </head><body><article>
      <div class="content-well">
        <div class="main-column__content">
          <div class="body-copy">
            <p>The complete Bloomberg report begins with market context,
            named sources and specific facts from the archived story.</p>
            <p>A second paragraph preserves the analysis and response
            required for a complete normalized article.</p>
          </div>
        </div>
        <aside class="right-rail">
          <div class="recirc"><ol class="recirc__list">
            <li>When Will It End? Bloodied Traders Are Seeking Clues</li>
            <li>Record Billions Flee the World's Largest ETF</li>
          </ol></div>
        </aside>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2018-01-31/"
            "a-legacy-bloomberg-report"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "complete Bloomberg report begins" in result.plain_text
    assert "Bloodied Traders" not in result.plain_text
    assert "Largest ETF" not in result.plain_text


def test_bloomberg_parser_removes_share_article_control_from_body():
    reporting = " ".join(["Bloomberg reporting sentence."] * 25)
    html = f"""
    <html><head>
      <meta property="og:title" content="Bloomberg market report">
      <meta property="article:published_time"
            content="2020-09-01T00:00:00Z">
    </head><body>
      <div class="body-copy">
        <div class="share-control"><span>SHARE THIS ARTICLE</span></div>
        <p>{reporting}</p>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2020-09-01/"
            "bloomberg-market-report"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Bloomberg reporting sentence." in result.plain_text
    assert "SHARE THIS ARTICLE" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_recovers_embedded_story_body_and_audio():
    content = [
        {
            "type": "paragraph",
            "data": {},
            "content": [
                {
                    "type": "text",
                    "value": (
                        f"Embedded Bloomberg podcast paragraph {index} "
                        "contains substantive reporting and context."
                    ),
                }
            ],
        }
        for index in range(1, 7)
    ]
    content.insert(
        1,
        {
            "type": "paragraph",
            "data": {},
            "content": [
                {
                    "type": "embed",
                    "href": "https://omny.fm/shows/example/episode",
                }
            ],
        },
    )
    payload = {
        "props": {
            "pageProps": {
                "story": {
                    "body": {"type": "document", "content": content}
                }
            }
        }
    }
    html = f"""
    <html><head>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "Embedded Bloomberg podcast",
        "datePublished": "2023-12-19T22:15:36Z"
      }}
      </script>
      <script type="application/json">{json.dumps(payload)}</script>
    </head><body>
      <article class="navigation-card"><p>Unrelated navigation card.</p></article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2023-12-19/"
            "podcast-embedded-story"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "audio"
    assert result.quality.body_characters >= 500
    embed_blocks = [
        block for block in result.blocks if block.type.value == "embed"
    ]
    assert [block.embed_url for block in embed_blocks] == [
        "https://omny.fm/shows/example/episode"
    ]
    assert "Unrelated navigation card" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_nyt_parser_joins_distributed_story_companion_columns():
    canonical_url = (
        "https://www.nytimes.com/2018/10/03/briefing/"
        "trump-taxes-kavanaugh-melania-trump.html"
    )
    html = b"""
    <html>
      <head>
        <meta property="og:title" content="Your Wednesday Evening Briefing">
        <meta name="pdate" content="20181003">
      </head>
      <body><article>
        <div class="StoryBodyCompanionColumn">
          <p>Good evening. Here is the latest national and international
          reporting selected for this briefing.</p>
        </div>
        <div class="StoryBodyCompanionColumn">
          <p>First, senators continued reviewing evidence during a closely
          watched confirmation process. The report explains the competing
          accounts and reactions from lawmakers.</p>
        </div>
        <div class="StoryBodyCompanionColumn">
          <p>Second, a tax investigation described financial transactions
          over several decades. Additional reporting supplies documentary
          context and responses from the people involved.</p>
        </div>
      </article></body>
    </html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
    )

    assert result.quality.status.value == "complete"
    assert "Good evening" in result.plain_text
    assert "senators continued" in result.plain_text
    assert "tax investigation" in result.plain_text
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_reuters_parser_removes_toolbar_licensing_ui_and_promotes_ksl_image():
    canonical_url = (
        "https://www.reuters.com/legal/litigation/"
        "airlines-oppose-facial-recognition-limits-2025-07-28"
    )
    image_base = "https://img.ksl.com/slc/3103/310340/31034099.JPG"
    structured = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": "Airlines oppose facial recognition limits",
            "datePublished": "2025-07-28T17:35:07Z",
            "image": [
                f"{image_base}?filter=ksl/100x100",
                f"{image_base}?filter=ksl/1600x900",
                f"{image_base}?filter=ksl/400x300",
            ],
        }
    )
    reporting = " ".join(["Reuters reporting sentence."] * 30)
    html = f"""
    <html><head>
      <script type="application/ld+json">{structured}</script>
      <meta property="og:title"
            content="Airlines oppose facial recognition limits">
      <meta property="article:published_time"
            content="2025-07-28T17:35:07Z">
      <meta property="og:image" content="{image_base}">
    </head><body><article>
      <div data-testid="ToolbarItemContainer">
        <ul><li>Small Text</li><li>Medium Text</li><li>Large Text</li></ul>
      </div>
      <div data-testid="ToolbarItemContainer">
        <ul><li>X</li><li>Facebook</li><li>Linkedin</li>
          <li>Email</li><li>Link</li></ul>
      </div>
      <figure>
        <img src="{image_base}?filter=ksl/100x100">
        <figcaption>Airline corporate logos.
          <a data-testid="LicenceContentButton">Purchase Licensing Rights</a>
        </figcaption>
      </figure>
      <p>{reporting}</p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
    )

    assert result.quality.status.value == "complete"
    assert "Reuters reporting sentence." in result.plain_text
    assert "Small Text" not in result.plain_text
    assert "Facebook Linkedin Email" not in result.plain_text
    assert "Purchase Licensing Rights" not in result.plain_text
    assert result.images[0].original_url == image_base
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


def test_reuters_yahoo_syndication_excludes_ai_summary_and_caption_noise():
    canonical_url = (
        "https://www.reuters.com/business/autos-transportation/"
        "boeing-justice-department-seek-judges-approval-"
        "deal-opposed-by-crash-victims-2025-07-03"
    )
    yahoo_url = (
        "https://www.yahoo.com/news/"
        "boeing-justice-department-seek-judges-035416509.html"
    )
    capture = raw_capture("reuters", canonical_url)
    capture = capture.model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=yahoo_url,
            ),
            "final_url": yahoo_url,
        }
    )
    html = b"""
    <!doctype html><html lang="en"><head>
      <meta property="og:site_name" content="Yahoo News">
      <meta property="og:image" content="https://s.yimg.com/example.jpg">
      <script type="application/ld+json">
      {
        "@type": "NewsArticle",
        "headline": "Boeing and Justice Department seek judge's approval for deal opposed by crash victims' families",
        "datePublished": "2025-07-03T03:54:16Z",
        "author": {"name": "David Shepardson"}
      }
      </script>
    </head><body><article>
      <h1>Boeing and Justice Department seek judge's approval</h1>
      <figure><figcaption>Unrelated lead-media caption noise.</figcaption></figure>
      <div class="key-takeaways">
        <p><button>AI key takeaways should never enter the article body.</button></p>
        <ul><li>Generated summary noise must be excluded.</li></ul>
      </div>
      <div class="article-content">
        <p>By David Shepardson</p>
        <p>(Reuters) - Boeing and the Justice Department asked a U.S. judge
        to approve an agreement concerning the 737 MAX case, despite
        objections from relatives of people killed in two crashes.</p>
        <p>The agreement includes compensation for victims' families and
        additional compliance obligations. Court filings explain the legal
        reasoning and provide enough reporting for a complete extraction.</p>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "Boeing and the Justice Department asked" in result.plain_text
    assert "AI key takeaways" not in result.plain_text
    assert "Generated summary noise" not in result.plain_text
    assert "Unrelated lead-media caption" not in result.plain_text
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


def test_reuters_postmedia_syndication_joins_only_reporting_paragraphs():
    canonical_url = (
        "https://www.reuters.com/markets/asia/"
        "indian-shares-rise-after-rates-steady-2021-04-07"
    )
    partner_url = "https://financialpost.com/pmn/business-pmn/example"
    capture = raw_capture("reuters", canonical_url).model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=partner_url,
            ),
            "final_url": partner_url,
        }
    )
    paragraph = (
        "Reuters reporting supplies substantive market facts, named "
        "sources, prices and policy context for readers. "
    )
    sections = "".join(
        f"""
        <section class="story-v2-content-element
                        article-content__content-group">
          <div class="story-v2-content-element-inline">
            <p>{paragraph * 2} Paragraph {index}.</p>
          </div>
        </section>
        """
        for index in range(3)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Indian shares rise">
      <meta property="article:published_time"
            content="2021-04-07T06:13:00Z">
    </head><body>
      <article class="story-v2-article-content-story">
        {sections}
        <div class="article-content__sign-in-group">
          <h2>Sign In or Create an Account</h2>
        </div>
        <ol class="list-widget__content">
          <li>Recommended headline Advertisement Story continues below</li>
        </ol>
        <div class="story-v2-footer-container">
          <p>Postmedia is committed to maintaining a lively forum.</p>
        </div>
      </article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "Paragraph 0" in result.plain_text
    assert "Paragraph 2" in result.plain_text
    assert "Sign In or Create" not in result.plain_text
    assert "Advertisement" not in result.plain_text
    assert "Postmedia is committed" not in result.plain_text
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


def test_reuters_syndication_removes_registration_and_subscription_ui():
    canonical_url = (
        "https://www.reuters.com/world/uk/"
        "licensed-partner-report-2021-04-07"
    )
    partner_url = "https://www.thestar.com.my/example"
    capture = raw_capture("reuters", canonical_url).model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=partner_url,
            ),
            "final_url": partner_url,
        }
    )
    html = b"""
    <html><head>
      <meta property="og:title" content="Licensed Reuters report">
      <meta property="article:published_time"
            content="2021-04-07T06:13:00Z">
    </head><body>
      <article class="article-content">
        <p>LONDON (Reuters) - The licensed report begins with verified
        facts and enough substantive context to preserve the story.</p>
        <p>A second paragraph records the response from named sources and
        explains the consequences for readers.</p>
        <p>Register now for FREE unlimited access to Reuters.com</p>
        <p>The company and law firm names shown above are generated
        automatically based on the text of the article. We are improving
        this feature as we continue to test and develop in beta.</p>
        <p>Reporting by Example Reporter; Editing by Example Editor</p>
        <p>Already a subscriber? <a>Log in</a></p>
        <hr>
        <h2>Get 20% OFF The Star Digital Access</h2>
        <p>Cancel anytime. Ad-free. Unlimited access with perks.</p>
        <h3>Monthly Plan</h3>
        <p>RM 13.90/month RM 11.12/month</p>
        <p>Thank you for your report!</p>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "licensed report begins" in result.plain_text
    assert "Reporting by Example Reporter" in result.plain_text
    assert "Register now" not in result.plain_text
    assert "generated automatically" not in result.plain_text
    assert "subscriber" not in result.plain_text
    assert "Monthly Plan" not in result.plain_text
    assert "Thank you for your report" not in result.plain_text
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


def test_reuters_parser_scopes_rcs_body_without_promoted_modules():
    html = b"""
    <html><head>
      <meta property="og:title" content="A Complete Legacy Reuters Report">
      <meta property="article:published_time"
            content="2017-05-12T00:00:00Z">
    </head><body><div id="rcs-articleContent">
      <div class="column1 col col-10">
        <span id="article-text">
          <p>ORWELL, Ohio (Reuters) - The complete report begins with
          detailed observations from reporters and named sources.</p>
          <p>Further paragraphs preserve the evidence, context and response
          necessary for a complete historical news article.</p>
        </span>
        <div class="info-box">
          Our Standards: The Thomson Reuters Trust Principles
        </div>
        <section><h3>Next In Autos</h3>
          <p>An unrelated recommendation must not enter the body.</p>
        </section>
        <div id="bd_article">From Around the Web Promoted by Revcontent</div>
      </div>
      <div class="column2">Trending Stories Pictures</div>
      <div class="story-content">Photos of the week</div>
    </div></body></html>
    """

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/article/autos-used/"
            "complete-report-idUSKBN1881PU"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "complete report begins" in result.plain_text
    assert "Promoted by" not in result.plain_text
    assert "Trending Stories" not in result.plain_text
    assert "unrelated recommendation" not in result.plain_text
    assert "Our Standards" not in result.plain_text


def test_reuters_parser_promotes_and_deduplicates_legacy_lazy_image():
    reporting = " ".join(["Reuters reporting sentence."] * 30)
    lead = (
        "https://s1.reutersmedia.net/resources/r/"
        "?m=02&d=20120310&t=2&i=580898814&w=1200&r=CBRE82902CK00"
    )
    lazy = (
        "https://s1.reutersmedia.net/resources/r/"
        "?m=02&d=20120310&t=2&i=580898814&r=CBRE82902CK00&w=20"
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Reuters legacy image report">
      <meta property="og:image" content="{lead}">
      <meta property="article:published_time"
            content="2012-03-10T00:00:00Z">
    </head><body><article>
      <p>{reporting}</p>
      <figure>
        <img src="{lazy}" aria-label="A detainee holds a fence.">
      </figure>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/article/"
            "us-legacy-image-idUSBRE82901O20120310"
        ),
    )

    assert result.quality.status.value == "complete"
    assert len(result.images) == 1
    assert result.images[0].role.value == "lead"
    assert result.images[0].original_url == lead
    assert lazy in result.images[0].candidate_urls
    assert result.images[0].alt == "A detainee holds a fence."
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


def test_reuters_parser_scopes_hashed_modern_body_and_removes_trust_link():
    reporting = " ".join(["Modern Reuters reporting sentence."] * 25)
    html = f"""
    <html><head>
      <meta property="og:title" content="Modern Reuters report">
      <meta property="article:published_time"
            content="2023-03-13T12:00:00Z">
    </head><body>
      <article>
        <div class="article-body__content__17Yit paywall-article">
          <p data-testid="Body">{reporting}</p>
          <p class="article-body__element__2p5pI" data-testid="Body">
            Our Standards:
            <a href="https://www.thomsonreuters.com/en/about-us/trust-principles.html">
              The Thomson Reuters Trust Principles.
            </a>
          </p>
          <div data-testid="Latest Updates"
               data-variant-id="article-latest-updates">
            <h3>Latest Updates</h3>
            <p>Unrelated recommendation headline, article with image</p>
          </div>
          <p>Capital Calls - More concise insights on global finance:</p>
          <p>Another unrelated recommended article read more</p>
        </div>
      </article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/world/example-report-2023-03-13/"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Modern Reuters reporting sentence." in result.plain_text
    assert "Trust Principles" not in result.plain_text
    assert "Unrelated recommendation" not in result.plain_text
    assert "Capital Calls" not in result.plain_text
    assert "Another unrelated" not in result.plain_text
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


def test_reuters_parser_trims_read_next_and_author_profile_tail():
    reporting = " ".join(["Verified Reuters reporting sentence."] * 25)
    html = f"""
    <html><head>
      <meta property="og:title" content="Reuters report with recirculation">
      <meta property="article:published_time"
            content="2023-10-16T12:00:00Z">
    </head><body><article>
      <div class="article-body__content__17Yit">
        <p>{reporting}</p>
        <section class="more-on">
          <h2>Recommended Stories</h2>
          <ol><li>Unrelated Capitol security story</li></ol>
        </section>
        <p>Reporting by Example Reporter; Editing by Example Editor</p>
        <div>
          <p class="article-body__element__2p5pI trust-badge">
            Our Standards: The Thomson Reuters Trust Principles.
          </p>
        </div>
        <p>Reporter profile biography must not enter the article.</p>
        <div class="article-body__element__2p5pI">
          <div class="read-next-mobile__container__10f75">
            <h2>Read Next</h2>
            <p>Unrelated recommended story headline</p>
          </div>
        </div>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/world/example-report-2023-10-16/"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Verified Reuters reporting sentence." in result.plain_text
    assert "Reporting by Example Reporter" in result.plain_text
    assert "Trust Principles" not in result.plain_text
    assert "Reporter profile biography" not in result.plain_text
    assert "Recommended Stories" not in result.plain_text
    assert "Unrelated Capitol security" not in result.plain_text
    assert "Read Next" not in result.plain_text
    assert "Unrelated recommended" not in result.plain_text


def test_reuters_parser_does_not_pad_truncated_body_with_interface_text():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Currencies Stable Ahead of Central Bank Decision">
      <meta property="article:published_time"
            content="2020-10-07T00:00:00Z">
    </head><body><article class="ArticlePage-article-body-1xN5M">
      <div class="ArticleBodyWrapper">
        <p class="Byline-byline ArticleBody-byline">
          By Anita Komuves BUDAPEST, Oct 7 (Reuters) - Central European
          currencies held.
        </p>
        <div class="ArticleBody-read-time-and-social">
          <p class="ReadTime-read-time-1s3CG">0 Min Read</p>
        </div>
        <div class="TrustBadge-trust-badge-20GM8">
          <p>Our Standards: The Thomson Reuters Trust Principles.</p>
        </div>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/article/easteurope-markets/"
            "currencies-idUSL8N2GY1OV"
        ),
    )

    assert result.quality.status.value == "partial"
    assert result.quality.warnings == ["body-too-short"]
    assert "0 Min Read" not in result.plain_text
    assert "Our Standards" not in result.plain_text


@pytest.mark.parametrize(
    ("headline", "body"),
    [
        (
            "BRIEF-Allianz SE placed hybrid 30-year bond",
            (
                "FRANKFURT, Oct 10 (Reuters) - Allianz SE says it placed "
                "a hybrid 30-year bond of 1.5 billion euros."
            ),
        ),
        (
            "标题新闻：俄经济部长称G20领导人或签署公报",
            (
                "俄罗斯经济发展部长表示领导人可能签署公报。"
                "以上为即时重要消息提示,路透中文快讯将暂不做进一步报导."
            ),
        ),
    ],
)
def test_reuters_parser_accepts_complete_short_news_records(headline, body):
    html = f"""
    <html><head>
      <script type="application/ld+json">{{
        "@type": "NewsArticle",
        "headline": {json.dumps(headline)},
        "datePublished": "2018-11-30T17:57:56Z"
      }}</script>
    </head><body>
      <div class="StandardArticleBody_body"><p>{body}</p></div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/article/"
            "brief-example-idUSL4S1Y553G"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]
    assert result.plain_text == body
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


@pytest.mark.parametrize(
    ("body_markup", "expected_text"),
    [
        (
            """
            <div class="StandardArticleBody_body">
              <p>Legacy Reuters standard body contains substantive reporting
              about markets, companies, and policy decisions.</p>
              <p>Another paragraph provides enough detail for a complete
              normalized article extraction.</p>
            </div>
            """,
            "Legacy Reuters standard body",
        ),
        (
            """
            <div class="ArticleBody_body_2ECha">
              <p>Hashed Reuters article body contains substantive reporting
              from the archived React page template.</p>
              <p>Another paragraph preserves the remainder of the original
              report for parser validation.</p>
            </div>
            """,
            "Hashed Reuters article body",
        ),
        (
            """
            <span id="articleText">
              <p>Classic Reuters article text contains substantive reporting
              from the archived pre-React page template.</p>
              <p>Another paragraph preserves the complete historical report
              for deterministic validation.</p>
            </span>
            """,
            "Classic Reuters article text",
        ),
        (
            """
            <span id="articleText">
              LONDON, Jan 8 (Reuters) - Classic Reuters reporting stored
              directly inside articleText remains available.<br><br>
              A second BR-delimited paragraph preserves market context,
              sources, and the rest of the historical report.<br><br>
              A third paragraph supplies enough substantive detail for a
              complete normalized article.
            </span>
            """,
            "BR-delimited paragraph",
        ),
        (
            """
            <div id="rcs-articleContent">
              <p>Alternate Reuters content contains substantive reporting
              from another archived pre-React page template.</p>
              <p>Another paragraph supplies the rest of the historical news
              report for complete extraction.</p>
            </div>
            """,
            "Alternate Reuters content",
        ),
    ],
)
def test_reuters_legacy_body_templates(body_markup, expected_text):
    canonical_url = "https://www.reuters.com/article/example-idUSL1N2AB123"
    html = f"""
    <!doctype html><html lang="en"><head>
      <meta property="og:title" content="Archived Reuters report">
      <meta name="analyticsAttributes.articleDate"
            content="2018-08-22T05:23:32+0000">
    </head><body>{body_markup}</body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=raw_capture("reuters", canonical_url),
    )

    assert result.quality.status.value == "complete"
    assert expected_text in result.plain_text
    assert result.published_at == datetime(
        2018,
        8,
        22,
        5,
        23,
        32,
        tzinfo=timezone.utc,
    )
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


def test_reuters_legacy_press_release_restores_nested_media_and_drops_disclaimer():
    canonical_url = (
        "https://www.reuters.com/article/"
        "idUS101591+03-Oct-2012+BW20121003"
    )
    opening = " ".join(["Opening press release reporting."] * 12)
    closing = " ".join(["Closing press release reporting."] * 12)
    html = f"""
    <!doctype html><html lang="en"><head>
      <meta property="og:title" content="Archived Business Wire report">
      <meta name="analyticsAttributes.articleDate"
            content="2012-10-03T12:00:00+0000">
    </head><body>
      <span id="articleText"><p>
        {opening}
        <div id="bwbodyimg">
          <img src="http://mms.businesswire.com/media/example.jpg"
               alt="Factory floor (Photo: Business Wire)">
          <p>Factory floor (Photo: Business Wire)</p>
        </div>
        {closing}
        <div id="div_with_disclaimer_id">
          This announcement is distributed by Thomson Reuters on behalf of
          Thomson Reuters clients. The owner of this announcement warrants
          that they are solely responsible for its content.
        </div>
      </p></span>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=raw_capture("reuters", canonical_url),
    )

    assert result.quality.status.value == "complete"
    assert [block.type.value for block in result.blocks] == [
        "paragraph",
        "image",
        "paragraph",
    ]
    assert "Opening press release reporting." in result.blocks[0].text
    assert "Closing press release reporting." in result.blocks[2].text
    assert result.images[0].caption == "Factory floor"
    assert result.images[0].credit == "Photo: Business Wire"
    assert result.images[0].should_archive
    assert "owner of this announcement" not in result.plain_text.casefold()
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


def test_bloomberg_press_release_trims_professional_service_footer():
    reporting = "\n".join(
        [
            "Bloomberg worked with leading banks to create the daily price "
            "fixing service and improve transparency in the market."
        ]
        * 5
    )
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Bloomberg Launches Daily Price Fixings" />
    </head><body>
      <div class="story_attribution">Press Release</div>
      <div id="story_content"><pre>{reporting}

The Bloomberg Professional^® service delivers reliable access to the latest
market data, financial news, and economic information critical to the
investment decision process.

For more information about Bloomberg Professional service, please contact
the sales team.

About Bloomberg

Bloomberg is the world's most trusted source of information.

Contact: press@example.com
      </pre></div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-11-16/"
            "bloomberg-launches-daily-price-fixings"
        ),
    )

    assert "improve transparency" in result.plain_text
    assert "Professional" not in result.plain_text
    assert "About Bloomberg" not in result.plain_text
    assert "press@example.com" not in result.plain_text


def test_bloomberg_sports_release_trims_promotional_about_tail():
    reporting = " ".join(
        ["Decision Maker evaluates player performance and matchup data."] * 8
    )
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Bloomberg Sports Launches Fantasy Tools" />
    </head><body><div id="story_content">
      <p>{reporting}</p>
      <p>Decision Maker 2011 is available for fantasy football players.
      The mobile apps are available for the iPhone and iPad.</p>
      <p>For more information on Bloomberg Sports, please visit
      <a href="http://www.bloombergsports.com">
      http://www.bloombergsports.com</a> and follow us on Twitter
      (@BloombergSports) and Facebook.</p>
      <h2>About Bloomberg Sports</h2>
      <p>Launched in 2010, Bloomberg Sports develops analytics products.</p>
      <h2>About Bloomberg</h2>
      <p>Bloomberg is a global information provider.</p>
      <p>Contact for Bloomberg: press@example.com</p>
    </div></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-08-30/"
            "bloomberg-sports-launches-fantasy-tools"
        ),
    )

    assert "mobile apps are available" in result.plain_text
    assert "For more information on Bloomberg Sports" not in result.plain_text
    assert "About Bloomberg Sports" not in result.plain_text
    assert "press@example.com" not in result.plain_text


def test_bloomberg_press_release_drops_regional_media_contacts():
    reporting = " ".join(
        ["Bloomberg Vault helps companies retain regulated communications."] * 8
    )
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Bloomberg Launches Cloud Archiving" />
    </head><body><div id="story_content">
      <p>{reporting}</p>
      <p>About Bloomberg
      Bloomberg, the global business and financial information and news
      leader, provides business information and technology.</p>
      <h2>Contact for Bloomberg</h2>
      <p>-Sophie Fischman, Cognito-US, +1-646-395-6300</p>
      <p>-Anne Karumo, Cognito-APAC, +65-8112-6409</p>
    </div></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-07-14/"
            "bloomberg-launches-cloud-archiving"
        ),
    )

    assert "retain regulated communications" in result.plain_text
    assert "About Bloomberg" not in result.plain_text
    assert "business information and technology" not in result.plain_text
    assert "Contact for Bloomberg" not in result.plain_text
    assert "Cognito" not in result.plain_text
    assert "+65-8112-6409" not in result.plain_text


def test_bloomberg_inline_image_drops_businessweek_subscription_caption():
    reporting = " ".join(
        ["Researchers compared climate interventions and policy risks."] * 8
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="How to Slow Climate Change" />
    </head><body><article>
      <p>{reporting}</p>
      <figure class="inline-image inline-media">
        <img src="https://assets.bwbx.io/images/example/v1/488x-1.jpg"
             alt="Featured in Bloomberg Businessweek, Nov. 30, 2015.
                  Subscribe now." />
        <figcaption class="inline-media__info">
          <div class="inline-media__caption">Featured in
          <em>Bloomberg Businessweek</em>, Nov. 30, 2015.
          <a href="https://subscribe.businessweek.com/">Subscribe now</a>.
          </div>
          <div class="inline-media__credit">Illustration: Justin Metz;
          Source: Getty Images (2)</div>
        </figcaption>
      </figure>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-11-30/"
            "how-to-slow-climate-change"
        ),
    )

    assert result.images[0].caption is None
    assert result.images[0].credit == (
        "Illustration: Justin Metz; Source: Getty Images (2)"
    )
    assert "Subscribe now" not in result.plain_text


def test_reuters_legacy_parser_uses_embedded_rcom_body():
    canonical_url = (
        "https://www.reuters.com/article/example/"
        "archived-report-idUSL1N2AB123"
    )
    embedded_body = (
        "<pre>Embedded Reuters reporting preserves substantive details "
        "from the archived article page.\n"
        "A second line contains market context and source attribution.\n"
        "A third line preserves the remainder of the original report.</pre>"
    )
    state = {
        "article_list": {
            "first_article": {
                "headline": "Archived Reuters report",
                "published": 1_503_456_789,
                "body": embedded_body,
            }
        }
    }
    html = f"""
    <!doctype html><html lang="en"><head>
      <meta property="og:title" content="Archived Reuters report">
      <meta name="sailthru.date" content="2017-08-22T05:23:32Z">
    </head><body>
      <script>window.RCOM_Data = {json.dumps(state)};</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=raw_capture("reuters", canonical_url),
    )

    assert result.quality.status.value == "complete"
    assert len(result.blocks) == 1
    assert "remainder of the original report" in result.plain_text
    assert result.published_at == datetime(
        2017,
        8,
        22,
        5,
        23,
        32,
        tzinfo=timezone.utc,
    )


def test_reuters_parser_recovers_structured_live_blog_updates():
    news_article = {
        "@type": "NewsArticle",
        "headline": "Hurricane Melissa as it happened",
        "datePublished": "2025-10-28T13:50:23Z",
    }
    live_blog = {
        "@type": "LiveBlogPosting",
        "headline": "Hurricane Melissa",
        "liveBlogUpdate": [
            {
                "@type": "BlogPosting",
                "headline": "Summary",
                "datePublished": "2025-10-28T13:50:46Z",
                "articleBody": (
                    "Hurricane Melissa roared through Cuba on Wednesday"
                    "More than 700,000 people evacuated from their homes"
                    "Four deaths were reported in Jamaica"
                    "Trouble viewing video posts? Content depends on your "
                    "cookie settings"
                ),
            },
            {
                "@type": "BlogPosting",
                "headline": "Storm sweeps towards The Bahamas",
                "datePublished": "2025-10-29T21:55:56Z",
                "articleBody": (
                    "The storm hit Cuba after unleashing devastation in "
                    "Jamaica. Authorities continued assessing damage."
                ),
            },
        ],
    }
    html = (
        "<html><head>"
        "<script type='application/ld+json'>"
        f"{json.dumps(news_article)}</script>"
        "<script type='application/ld+json'>"
        f"{json.dumps(live_blog)}</script>"
        "</head><body><main data-testid='LivePage'></main></body></html>"
    ).encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/world/"
            "hurricane-melissa-live-example"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "liveblog"
    assert result.quality.block_count == 4
    assert "Wednesday. More than 700,000" in result.plain_text
    assert "cookie settings" not in result.plain_text
    assert "Storm sweeps towards The Bahamas" in result.plain_text


def test_bloomberg_yahoo_syndication_excludes_nested_recommendations():
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2024-06-03/"
        "tories-fail-to-dent-labour-polling-lead-in-early-uk-campaign"
    )
    yahoo_url = (
        "https://www.yahoo.com/news/"
        "tories-fail-to-dent-labour-polling-lead-040000123.html"
    )
    capture = raw_capture("bloomberg", canonical_url)
    capture = capture.model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=yahoo_url,
            ),
            "final_url": yahoo_url,
        }
    )
    html = b"""
    <!doctype html><html lang="en"><head>
      <meta property="og:site_name" content="Yahoo News">
      <script type="application/ld+json">
      {
        "@type": "NewsArticle",
        "headline": "Tories Fail to Dent Labour Polling Lead in Early UK Campaign",
        "datePublished": "2024-06-03T04:00:00Z",
        "author": {"name": "Bloomberg News"}
      }
      </script>
    </head><body><article>
      <figure><figcaption>Unrelated lead-media caption.</figcaption></figure>
      <div class="key-takeaways">
        <p><button>Generated Yahoo summary must be excluded.</button></p>
      </div>
      <div class="article-content">
        <p>Bloomberg News reports that the governing party failed to narrow
        the opposition's polling lead during the opening campaign.</p>
        <p>The survey compares voter groups and includes enough substantive
        reporting for the normalized article body.</p>
      </div>
      <aside>
        <article><p>Nested recommendation text must not enter the body.</p>
        </article>
      </aside>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "failed to narrow" in result.plain_text
    assert "Generated Yahoo summary" not in result.plain_text
    assert "Unrelated lead-media caption" not in result.plain_text
    assert "Nested recommendation" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_moneyweb_syndication_strips_copyright_footer():
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2015-10-27/"
        "zuma-against-changing-south-africa-s-constitution-for-third-term"
    )
    partner_url = (
        "https://www.moneyweb.co.za/news-fast-news/"
        "zuma-against-changing-constitution-for-third-term/"
    )
    capture = raw_capture("bloomberg", canonical_url).model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=partner_url,
            ),
            "final_url": partner_url,
        }
    )
    html = """
    <!doctype html><html><head>
      <link rel="canonical"
            href="https://www.moneyweb.co.za/news-fast-news/example/">
      <meta property="og:title" content="Zuma Against Changing Constitution">
      <meta property="article:published_time"
            content="2015-10-27T04:00:00Z">
    </head><body>
      <div id="storybody">
        <p class="indent">The president discussed constitutional term limits
        and the governing party's succession process in an interview.</p>
        <p class="indent">The report compared recent democratic votes across
        several African countries and described the opposition response.</p>
        <p class="indent">©2015 Bloomberg News</p>
      </div>
      <p>Moneyweb subscription and navigation text must stay outside.</p>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "succession process" in result.plain_text
    assert "©2015 Bloomberg News" not in result.plain_text
    assert "Moneyweb subscription" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_esm_syndication_selects_nested_story_only():
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2015-07-23/"
        "sabmiller-quarterly-sales-miss-estimates-on-weakness-in-europe"
    )
    partner_url = (
        "https://www.esmmagazine.com/drinks/"
        "sabmiller-quarterly-sales-miss-estimates-on-weakness-in-europe-18047"
    )
    capture = raw_capture("bloomberg", canonical_url).model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=partner_url,
            ),
            "final_url": partner_url,
        }
    )
    html = """
    <!doctype html><html><head>
      <link rel="canonical"
            href="https://www.esmmagazine.com/drinks/example">
      <meta property="og:title" content="Brewer Quarterly Sales Miss">
      <meta property="article:published_time"
            content="2015-07-23T04:00:00Z">
    </head><body><article>
      <div class="article__content"><article>
        <p>The brewer reported quarterly sales that missed estimates amid
        difficult trading conditions across several European markets.</p>
        <p>Growth elsewhere was tempered by weaker regional volumes while
        management retained its longer-term operating outlook.</p>
        <p>News by Bloomberg, edited by ESM. To subscribe to ESM:
        The European Supermarket Magazine, click here.</p>
      </article></div>
      <div><span>Tags</span><a>SABMiller</a></div>
      <div><h3>Recommended Reading</h3></div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "quarterly sales" in result.plain_text
    assert "Recommended Reading" not in result.plain_text
    assert "Tags" not in result.plain_text
    assert "edited by ESM" not in result.plain_text
    assert "subscribe to ESM" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_plain_link_to_statement_footer():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Company Cuts Annual Forecast">
      <meta property="article:published_time"
            content="2015-10-16T04:00:00Z">
    </head><body><article>
      <p>The company reduced its annual forecast after weaker quarterly
      results in two of its largest international markets.</p>
      <p>Management also reported the latest revenue and operating earnings
      figures while describing its revised expectations.</p>
      <p>Link to Statement: Link</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-10-16/"
            "hugo-boss-shares-slide-as-china-slump-hurts-full-year-forecasts"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "operating earnings" in result.plain_text
    assert "Link to Statement" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_truncated_hedge_fund_terminal_command():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Hedge Fund Faces Regulatory Delay">
      <meta property="article:published_time"
            content="2015-04-07T04:00:00Z">
    </head><body><article>
      <p>The fund delayed its launch while the regulator reviewed the
      application and responsibilities of its senior managers.</p>
      <p>The prospective investors were informed and representatives for
      the relevant firms declined to provide additional comment.</p>
      <p>Hedge-fund rankings: {HFND &lt;GO&gt;}</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-04-07/"
            "ex-citigroup-currency-chief-s-hedge-fund-said-to-face-fca-delay"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "prospective investors" in result.plain_text
    assert "Hedge-fund rankings" not in result.plain_text
    assert "HFND" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_transcript_strips_multimedia_and_machine_markers():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Pelosi Interview Transcript">
      <meta property="article:published_time"
            content="2012-07-27T04:00:00Z">
    </head><body><article>
      <p>HOST: The interview discusses congressional policy and the
      positions taken by both parties during the current campaign.</p>
      <p>GUEST: The response explains the legislative strategy and the
      issues expected to return when lawmakers reconvene.</p>
      <p>For more Bloomberg Multimedia see {AV &lt;GO&gt;}</p>
      <p>Top Stories: TOP Top Bond Stories: TOP BON
      Bloomberg Billionaires Index: RICH</p>
      <p>#&lt;738796.1204164.3.0.2.9.25&gt;#</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-07-27/"
            "pelosi-says-republicans-use-israel-to-distract-transcript-"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "legislative strategy" in result.plain_text
    assert "Bloomberg Multimedia" not in result.plain_text
    assert "Top Bond Stories: TOP BON" not in result.plain_text
    assert "738796.1204164" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_related_tickers_terminal_metadata():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Companies Form Real Estate Fund">
      <meta property="article:published_time"
            content="2015-11-29T04:00:00Z">
    </head><body><article>
      <p>The companies formed a real estate development fund to finance
      construction work and other projects in the coastal city.</p>
      <p>The bank will finance the fund while its investment unit manages
      it and the development company carries out the projects.</p>
      <p>Related tickers: KINGDOM AB (Kingdom Holding Co)
      ALINMA AB (Alinma Bank)</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-11-29/"
            "jeddah-economic-alinma-form-8-4-billion-riyal-real-estate-fund"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "investment unit manages" in result.plain_text
    assert "Related tickers" not in result.plain_text
    assert "KINGDOM AB" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_origin_marks_final_unclosed_quote_as_truncated():
    html = """
    <!doctype html><html><head>
      <link rel="canonical"
            href="https://www.bloomberg.com/news/articles/2015-11-10/example">
      <meta property="og:url"
            content="https://www.bloomberg.com/news/articles/2015-11-10/example">
      <meta property="og:title"
            content="Oil Price Drop Threatens Climate Industries">
      <meta property="article:published_time"
            content="2015-11-10T01:01:00Z">
    </head><body><article>
      <div class="body-copy-v2">
        <p>The report describes investment in renewable power and the
        policies needed to maintain deployment across major markets.</p>
        <p>Those investments help limit the projected global temperature
        increase while expanding energy efficiency regulation.</p>
        <p>“The coverage of mandatory energy efficiency regulation
        worldwide expanded to more than a quarter of global consumption.</p>
        <div class="terminal-tout">Before it's here, it's on the
        Bloomberg Terminal.</div>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-11-10/"
            "oil-price-drop-threatens-industries-that-help-cut-global-warming"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "Before it's here" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_related_story_commands_and_inline_osch():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Market Methodology and Policy">
      <meta property="article:published_time"
            content="2012-03-02T04:00:00Z">
    </head><body><article>
      <p>The report lists the largest changes in implied volatility from
      the previous trading day for actively traded contracts.</p>
      <p>This {OSCH &lt;GO&gt;} search was limited to options that are more
      than ten days from expiration and met the stated volume threshold.</p>
      <p>Stories related to the ECB: {NI ECB &lt;GO&gt;}
      Euro-region economic stories: {TNI ECO EUROP &lt;GO&gt;}
      Europe Crisis: {CRISIS &lt;GO&gt;}
      Top Europe stories: {TOP EUR &lt;GO&gt;}</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-03-02/"
            "u-s-stock-options-with-biggest-changes-in-implied-volatility"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "This search was limited to options" in result.plain_text
    assert "Stories related to the ECB" not in result.plain_text
    assert "OSCH" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_letter_and_author_social_prompts():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Policy Commentary">
      <meta property="article:published_time"
            content="2010-08-01T04:00:00Z">
    </head><body><article>
      <p>The commentary examines the fiscal policy choices facing lawmakers
      and the likely consequences for households and businesses.</p>
      <p>(The author is a columnist at Bloomberg News. The opinions
      expressed are his own. Read more here.)</p>
      <p>Nic Screws is the style director at Bloomberg. Follow her on
      Instagram and Twitter.</p>
      <p>Click on {LETT &lt;GO&gt;} to send a letter to the editor.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-08-01/"
            "democrats-relish-fight-over-big-income-tax-cuts-commentary"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "fiscal policy choices" in result.plain_text
    assert "The author is a columnist" in result.plain_text
    assert "Nic Screws is the style director" in result.plain_text
    assert "Read more here" not in result.plain_text
    assert "Follow her on" not in result.plain_text
    assert "letter to the editor" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_related_information_terminal_footer():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Policy receives public support">
      <meta property="article:published_time"
            content="2010-08-12T04:00:00Z">
    </head><body><article>
      <p>Residents said they wanted a place to live and work while their
      children continued attending local schools.</p>
      <p>Officials said the municipality would review the policy after
      consulting community representatives.</p>
      <p>For Related Information: Top French News
      {TOP FR &lt;GO&gt;} On religion in France:
      {TNI FRA RLG &lt;GO&gt;} For Top Stories: {TOP &lt;GO&gt;}</p>
      <p>To contact the reporter on this story:
      reporter@bloomberg.net</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-08-12/"
            "policy-receives-public-support"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "municipality would review" in result.plain_text
    assert "For Related Information" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert "@bloomberg.net" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_keeps_partner_author_credit_without_social_provenance():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Conservation priorities">
      <meta property="article:published_time"
            content="2013-08-27T04:00:00Z">
    </head><body><article>
      <p>Conservation requires difficult choices about how limited
      funding can protect the widest range of habitats.</p>
      <p>Researchers said the available money should support diverse
      ecosystems rather than a single species.</p>
      <p><em>Timothy Lavin is an editorial board member at Bloomberg
      View. <a href="https://twitter.com/example">Follow him</a> on
      Twitter. This post originally appeared
      <a href="https://www.bloomberg.com/example">here</a>.</em></p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-08-27/"
            "conservation-priorities"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Timothy Lavin is an editorial board member" in result.plain_text
    assert "Follow him on Twitter" not in result.plain_text
    assert "originally appeared here" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_column_subscription_from_correction_note():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Law firms expand practices">
      <meta property="article:published_time"
            content="2013-05-21T04:00:00Z">
    </head><body><article>
      <p>(Corrects lawyer's title in headline of an item in the Moves
      section of the column published yesterday. To be sent this column
      daily, click SALT LAWBIZ &lt;GO&gt;.)</p>
      <p>Several firms hired partners to expand their insurance recovery
      and corporate practices in major markets.</p>
      <p>The lawyers will lead new offices and advise clients on complex
      commercial disputes.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-05-21/"
            "law-firms-expand-practices"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Corrects lawyer's title" in result.plain_text
    assert "column published yesterday.)" in result.plain_text
    assert "To be sent this column" not in result.plain_text
    assert "SALT LAWBIZ" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_wallstreetpit_mirror_drops_affiliate_disclaimer():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Princeton brews trouble">
      <meta property="article:published_time"
            content="2011-12-29T04:00:00Z">
    </head><body><article>
      <div class="entry-content">
      <p>The university debate illustrates a broader disagreement about
      economic policy and the distribution of income in the United States.</p>
      <p>The final reporting paragraph explains why the dispute matters to
      investors and policymakers considering the available evidence.</p>
      <ul><li><a class="external" rel="nofollow" href="https://ads.example/">
      <strong>The 15-Second Scalping Strategy That Works</strong></a></li></ul>
      <div class="adblock"><p><i>Disclaimer: This page contains
      <a href="/privacy-policy">affiliate links</a>. If you choose to make a
      purchase after clicking a link, we may receive a commission at no
      additional cost to you. Thank you for your support!</i></p></div>
      </div>
      <div class="entry-tags"><ul><li><a rel="tag">Finance</a></li></ul></div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-12-29/"
            "princeton-brews-trouble-for-us-1-percenters-commentary"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "final reporting paragraph" in result.plain_text
    assert "affiliate links" not in result.plain_text
    assert "receive a commission" not in result.plain_text
    assert "Scalping Strategy" not in result.plain_text
    assert "Finance" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_business_times_drops_no_print_promos_and_recirculation():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Airbnb raises new funds">
      <meta property="article:published_time"
            content="2015-06-18T04:00:00Z">
    </head><body><article>
      <div class="typo-article-body">
        <p>The company is in talks to raise capital at a higher valuation,
        according to people familiar with the financing discussions.</p>
        <p>A spokeswoman did not return a request for comment.</p>
        <div class="container no-print">
          <h3>Asean Intelligence</h3>
          <p>Get insights into businesses across South-east Asia</p>
        </div>
      </div>
      <div class="no-print">
        <p>Share with us your feedback on BT's products and services</p>
        <p>Singapore banks' battle for wealth talent goes beyond bankers</p>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-06-18/"
            "airbnb-said-in-talks-to-raise-funds"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "spokeswoman did not return" in result.plain_text
    assert "Asean Intelligence" not in result.plain_text
    assert "Get insights into businesses" not in result.plain_text
    assert "Share with us your feedback" not in result.plain_text
    assert "Singapore banks' battle" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_origin_marks_mid_sentence_for_more_capture_truncated():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Law firm revenue increases">
      <meta property="og:url"
            content="https://www.bloomberg.com/news/2010-09-24/law-firms.html">
      <meta property="article:published_time"
            content="2010-09-24T04:00:00Z">
    </head><body><article>
      <p>Britain's largest law firms increased quarterly revenue amid
      continued uncertainty about the economy, according to a survey.</p>
      <p>The largest firms reported smaller gains than the wider group.</p>
      <p>Clifford Chance regained its spot as the highest-grossing</p>
      <p>For more, click here.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-09-24/"
            "law-firms"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "For more, click here" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_origin_marks_trailing_section_heading_truncated():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Reinsurer erases gains">
      <meta property="og:url"
            content="https://www.bloomberg.com/news/articles/2015-12-09/reinsurer">
      <meta property="article:published_time"
            content="2015-12-09T04:00:00Z">
    </head><body><article>
      <section class="article-body">
        <div class="article-body__content">
          <p>The reinsurer erased most of its gains after investment and
          underwriting losses reduced its reported book value.</p>
          <p>The ratings firm lowered its outlook to negative from stable,
          citing risks in the investment portfolio.</p>
          <h2>Patience</h2>
          <div class="terminal-tout">Before it's here, it's on the terminal.</div>
        </div>
      </section>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-12-09/"
            "reinsurer-erases-gains"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "ratings firm lowered its outlook" in result.plain_text
    assert "Patience" not in result.plain_text
    assert "Before it's here" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_find_column_terminal_suffix_from_correction():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Company enters courtroom phase">
      <meta property="article:published_time"
            content="2014-10-28T04:00:00Z">
    </head><body><article>
      <p>(Corrects reference to Ypres in third section. Find this column
      daily at NI OPENLINE &lt;GO&gt;.)</p>
      <p>The company entered the courtroom phase of a dispute after
      months of negotiations between the parties.</p>
      <p>The judge scheduled arguments on the remaining claims for the
      following week.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-10-28/"
            "company-enters-courtroom-phase"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Corrects reference to Ypres in third section.)" in result.plain_text
    assert "Find this column daily" not in result.plain_text
    assert "NI OPENLINE" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_more_view_columns_footer():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Social Networks and Marriage">
      <meta property="article:published_time"
            content="2011-07-14T04:00:00Z">
    </head><body><article>
      <p>The column examines research about social networks and changing
      patterns in personal relationships and communication.</p>
      <p>The author discusses the limits of the available evidence and the
      need for more careful study before drawing broad conclusions.</p>
      <p>For more Bloomberg View columns.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-07-14/"
            "facebook-might-be-to-blame-for-your-divorce-sheril-kirshenbaum"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "available evidence" in result.plain_text
    assert "For more Bloomberg View columns" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_decodes_latin1_label_as_windows_1252():
    html = b"""
    <!doctype html><html><head>
      <meta http-equiv="Content-Type"
            content="text/html; charset=iso-8859-1">
      <meta property="og:title" content="Legacy Bloomberg punctuation">
      <meta property="article:published_time"
            content="2010-04-12T04:00:00Z">
      <title>Legacy Bloomberg punctuation</title>
    </head><body><article>
      <p>The company said the search isn\x92t public and the manager called
      it \x93a useful test\x94 for investors following the market.</p>
      <p>A second reporting paragraph supplies enough substantive text to
      classify this archived Bloomberg story as complete.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-04-12/"
            "goldman-courts-endowment-heads"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "isn’t public" in result.plain_text
    assert "“a useful test”" in result.plain_text
    assert not re.search(r"[\x80-\x9f]", result.plain_text)
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_repairs_utf8_encoded_windows_1252_control():
    html = b"""
    <!doctype html><html><head>
      <meta charset="utf-8">
      <meta property="og:title" content="Contract shortlist">
      <meta property="article:published_time"
            content="2010-11-01T04:00:00Z">
    </head><body><article>
      <p>Several companies were shortlisted for the project, according
      to people familiar with the decision.</p>
      <p>The people declined to be identified because the decision
      hasn\xc2\x92t been publicly announced.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-11-01/"
            "contract-shortlist"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "hasn’t been publicly announced" in result.plain_text
    assert not re.search(r"[\x80-\x9f]", result.plain_text)
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_deduplicates_dateline_prefixed_video_summary():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Water Polo Team Publicity">
      <meta property="article:published_time"
            content="2010-10-13T04:00:00Z">
    </head><body><article>
      <p>Oct. 13 (Bloomberg) -- Bloomberg's Michele Steele reports on the
      U.S. Women's Water Polo Team and its recent cover for ESPN The
      Magazine. (Source: Bloomberg)</p>
      <p>Bloomberg's Michele Steele reports on the U.S. Women's Water Polo
      Team and its recent cover for ESPN The Magazine. (Source: Bloomberg)</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-10-13/"
            "u-s-women-s-water-polo-team-video"
        ),
    )

    summaries = [
        block.text
        for block in result.blocks
        if block.text and "Michele Steele reports" in block.text
    ]
    assert result.quality.status.value == "complete"
    assert len(summaries) == 1
    assert summaries[0].startswith("Oct. 13 (Bloomberg) --")
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_deduplicates_split_dateline_video_summary():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Fama Discusses Regulation">
      <meta property="article:published_time"
            content="2010-11-12T04:00:00Z">
    </head><body><article>
      <p>Nov. 12 (Bloomberg) -- Eugene Fama discusses financial regulation
      and capital requirements for banks. Fama speaks on Bloomberg
      Television's InsideTrack. (Source: Bloomberg)</p>
      <p>Eugene Fama discusses financial regulation and capital requirements
      for banks.</p>
      <p>Fama speaks on Bloomberg Television's InsideTrack.
      (Source: Bloomberg)</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-11-12/"
            "fama-says-too-big-to-fail-video"
        ),
    )

    paragraphs = [
        block.text
        for block in result.blocks
        if block.type.value == "paragraph"
    ]
    assert result.quality.status.value == "complete"
    assert len(paragraphs) == 1
    assert paragraphs[0].startswith("Nov. 12 (Bloomberg) --")
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_promotes_legacy_thumbnail_caption_to_figure():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="SEC Enforcement Vote">
      <meta property="article:published_time"
            content="2010-04-19T04:00:00Z">
    </head><body><article>
      <p>The commission approved an enforcement case after reviewing the
      disclosures and hearing arguments from both sides.</p>
      <p>A second reporting paragraph explains the divided vote and the
      expected next steps in the litigation.</p>
      <div class="image thumbnail item_container">
        <div class="thumbnail_container">
          <img src="https://assets.bwbx.io/sec-building.jpg"
               alt="The SEC headquarters">
        </div>
        <p class="caption">A file photo of the SEC in Washington.
        Photographer: Andrew Harrer/Bloomberg</p>
      </div>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-04-19/"
            "sec-enforcement-vote"
        ),
    )

    assert len(result.images) == 1
    assert result.images[0].caption == "A file photo of the SEC in Washington."
    assert result.images[0].credit == "Photographer: Andrew Harrer/Bloomberg"
    assert result.plain_text.count("A file photo of the SEC") == 1
    assert result.blocks[-1].type.value == "image"
    assert result.blocks[-1].caption == result.images[0].caption
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_standalone_wire_credit_and_author_follow_tail():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Oil Projects Face New Pressure">
      <meta property="article:published_time"
            content="2014-12-18T05:00:00Z">
    </head><body><article>
      <p>Lower prices are forcing producers to reconsider projects that
      require years of investment before returning cash.</p>
      <p>The analysis compares planned spending with several possible price
      paths and explains the risks for future production.</p>
      <p>Follow @tsrandall on Twitter for more zombie analysis</p>
      <p>* Bloomberg</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-12-18/"
            "bankers-see-investments-stranded"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "forcing producers" in result.plain_text
    assert "Follow @tsrandall" not in result.plain_text
    assert "* Bloomberg" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_more_stories_by_recirculation_tail():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Summer Is Hardly Relaxing">
      <meta property="article:published_time"
            content="2014-07-31T05:00:00Z">
    </head><body><article>
      <p>For many people, summer arrives with an undercurrent of frustration
      because work and family obligations do not slow down.</p>
      <p>They would be better off lowering expectations and enjoying any
      peace and quiet that happens to come along.</p>
      <p>More stories by Ben Steverman:</p>
      <ul>
        <li><a href="/news/articles/related-one">Vacation-Phobic Americans</a></li>
        <li><a href="/news/articles/related-two">The New Card Sharks</a></li>
        <li><a href="/news/articles/related-three">You Want a Raise?</a></li>
      </ul>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-07-31/"
            "summer-s-a-time-to-relax"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "lowering expectations" in result.plain_text
    assert "More stories by" not in result.plain_text
    assert "Vacation-Phobic Americans" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_more_stories_from_blog_recirculation_tail():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Hidden Cash in Divorce Proceedings">
      <meta property="article:published_time"
            content="2013-06-03T05:00:00Z">
    </head><body><article>
      <p>Forensic accountants can identify signs of hidden assets when a
      divorce case involves closely held businesses, complex compensation,
      tax filings, property records, and spending patterns that do not match
      a party's disclosed income.</p>
      <p><em><strong>More stories from the </strong></em>
      <strong><em><a href="/blogs/personal_finance/ventured-gained/">
      Ventured&amp;Gained</a> blog:</em></strong></p>
      <ul>
        <li><a href="/news/articles/related-one">A Man, A Van, A Debt-Defying Plan</a></li>
        <li><a href="/news/articles/related-two">A Millionaire's Recovery</a></li>
      </ul>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-06-03/"
            "hunting-for-hidden-cash-in-divorce-proceedings"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Forensic accountants" in result.plain_text
    assert "More stories from" not in result.plain_text
    assert "A Man, A Van" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_inline_author_social_prompt_before_disclaimer():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="A Political Speech">
      <meta property="article:published_time"
            content="2013-04-11T05:00:00Z">
    </head><body><article>
      <p>The speech offered a broad account of democratic institutions and
      the economic challenges facing the country.</p>
      <p>A second reporting paragraph examines the argument and its
      implications for the approaching election.</p>
      <p>(Chandrahas Choudhury, a novelist, is the New Delhi correspondent
      for World View. Follow him on Twitter @Hashestweets. The opinions
      expressed are his own.)</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-04-11/"
            "a-political-speech"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "New Delhi correspondent for World View" in result.plain_text
    assert "The opinions expressed are his own" in result.plain_text
    assert "Follow him on Twitter" not in result.plain_text
    assert "@Hashestweets" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_strips_author_tumblr_prompt_from_bio_tail():
    html = b"""
    <!doctype html><html><head>
      <meta property="og:title" content="Lafayette Restaurant Review">
      <meta property="article:published_time"
            content="2013-06-19T05:00:00Z">
    </head><body><article>
      <p>The restaurant offers a focused menu with carefully prepared
      dishes and a quieter dining room than its downtown rivals.</p>
      <p>The critic explains which dishes succeed and which should be
      skipped during a visit.</p>
      <p>(Ryan Sutton writes about New York City restaurants for Muse.
      The opinions expressed are his own. Follow him on Tumblr at
      www.thepricehike.com or www.thebaddeal.com.)</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-06-19/"
            "lafayette-restaurant-review"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Ryan Sutton writes about New York City restaurants" in result.plain_text
    assert "The opinions expressed are his own" in result.plain_text
    assert "Follow him on Tumblr" not in result.plain_text
    assert "thepricehike.com" not in result.plain_text
    assert "thebaddeal.com" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_yahoo_syndication_removes_most_read_list():
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2022-07-14/"
        "central-bank-policy-example"
    )
    yahoo_url = (
        "https://www.yahoo.com/news/"
        "central-bank-policy-example-040000123.html"
    )
    capture = raw_capture("bloomberg", canonical_url)
    capture = capture.model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=yahoo_url,
            ),
            "final_url": yahoo_url,
        }
    )
    opening = " ".join(["Opening Bloomberg reporting sentence."] * 12)
    closing = " ".join(["Closing Bloomberg reporting sentence."] * 12)
    html = f"""
    <!doctype html><html lang="en"><head>
      <meta property="og:site_name" content="Yahoo News">
      <meta property="og:title" content="Central Bank Policy Report">
      <meta property="article:published_time"
            content="2022-07-14T04:00:00Z">
    </head><body><article>
      <div class="article-content">
        <p>{opening}</p>
        <p>Most Read from Bloomberg</p>
        <ul>
          <li><p><a href="https://www.bloomberg.com/news/articles/other-1">
            Unrelated most-read headline one</a></p></li>
          <li><p><a href="https://www.bloomberg.com/news/articles/other-2">
            Unrelated most-read headline two</a></p></li>
        </ul>
        <p>{closing}</p>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "Opening Bloomberg reporting sentence." in result.plain_text
    assert "Closing Bloomberg reporting sentence." in result.plain_text
    assert "Most Read from Bloomberg" not in result.plain_text
    assert "Unrelated most-read headline" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_nyt_parser_trims_access_shell_after_complete_article():
    reporting = " ".join(["New York Times reporting sentence."] * 35)
    html = f"""
    <html><head>
      <meta property="og:title" content="Recovered Times Article">
      <meta property="article:published_time"
            content="2024-02-08T12:00:00Z">
    </head><body><article>
      <section name="articleBody">
        <p>{reporting}</p>
        <div class="css-access-shell">
          <p>Thank you for your patience while we verify access.</p>
          <div data-testid="optimistic-truncator-message">
            <p>Already a subscriber? Log in.</p>
            <p>Want all of The Times? Subscribe.</p>
          </div>
        </div>
      </section>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2024/02/08/world/"
            "recovered-times-article.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "New York Times reporting sentence." in result.plain_text
    assert "verify access" not in result.plain_text
    assert "Already a subscriber" not in result.plain_text


def test_nyt_parser_removes_sponsorship_subscription_and_opinion_footer_ui():
    reporting = " ".join(["New York Times reporting sentence."] * 35)
    html = f"""
    <html><head>
      <meta property="og:title" content="Times Report">
      <meta property="article:published_time"
            content="2020-12-23T12:00:00Z">
    </head><body><article>
      <section name="articleBody">
        <p>Supported by</p>
        <p>{reporting}</p>
        <p>The reporting is supported by extensive documentary evidence.</p>
        <p>Subscriber support helps make Times journalism possible.
        If you’re not already a subscriber, please consider becoming one
        today.</p>
        <p>The Times is committed to publishing a diversity of letters to
        the editor. We’d like to hear what you think.</p>
        <p>Follow The New York Times Opinion section on Facebook,
        Instagram and X.</p>
        <p>Share full article</p>
      </section>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2020/12/23/briefing/times-report.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "New York Times reporting sentence." in result.plain_text
    assert "supported by extensive documentary evidence" in result.plain_text
    assert "Subscriber support" not in result.plain_text
    assert "diversity of letters" not in result.plain_text
    assert "Opinion section on Facebook" not in result.plain_text
    assert "Share full article" not in result.plain_text
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_removes_california_today_subscription_ctas():
    reporting = " ".join(["California reporting sentence."] * 35)
    html = f"""
    <html><head>
      <meta property="og:title" content="California briefing">
      <meta property="article:published_time" content="2020-12-09T12:00:00Z">
    </head><body><article><section name="articleBody">
      <p>{reporting}</p>
      <p>(This article is part of the California Today newsletter. Sign up to
      get it delivered to your inbox.)</p>
      <p>California Today goes live at 6:30 a.m. Pacific time weekdays. Were
      you forwarded this email? Sign up for California Today here and read
      every edition online here.</p>
      <p>Sign up for weekly updates on learning from The Times.</p>
    </section></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url="https://www.nytimes.com/2020/12/09/us/example.html",
    )

    assert result.quality.status.value == "complete"
    assert "California reporting sentence." in result.plain_text
    assert "This article is part of" not in result.plain_text
    assert "Were you forwarded this email" not in result.plain_text
    assert "weekly updates on learning" not in result.plain_text
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_uses_article_summary_when_archived_live_headline_is_empty():
    reporting = " ".join(["New York Times live reporting sentence."] * 35)
    html = f"""
    <html><head>
      <title>The New York Times</title>
      <meta property="article:published_time"
            content="2020-10-02T22:58:07-04:00">
    </head><body><article>
      <header>
        <h1 itemprop="headline"></h1>
        <p id="article-summary">Positive tests inch up in New York City.</p>
      </header>
      <section name="articleBody"><p>{reporting}</p></section>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2020/10/02/world/"
            "02virus-briefing-deblasio.html"
        ),
    )

    assert result.headline == "Positive tests inch up in New York City."
    assert result.quality.status.value == "complete"
    assert "missing-headline" not in result.quality.warnings
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_removes_related_coverage_and_newsletter_modules():
    reporting = " ".join(["New York Times analysis sentence."] * 35)
    html = f"""
    <html><head>
      <meta property="og:title" content="Times Analysis">
      <meta property="article:published_time"
            content="2018-06-04T12:00:00Z">
    </head><body><article>
      <section name="articleBody">
        <p>{reporting}</p>
        <div class="RelatedCoverage-relatedcoverage--LmkKX">
          <h3>Related Coverage</h3>
          <p>Unrelated recommended opinion article</p>
        </div>
        <div id="newsletter-module" class="Newsletter-wrap--1WbCb">
          <h2>Sign up for The Upshot Newsletter</h2>
          <p>Manage Email Preferences and read our Privacy Policy</p>
        </div>
      </section>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2018/06/04/opinion/"
            "times-analysis.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "New York Times analysis sentence." in result.plain_text
    assert "Related Coverage" not in result.plain_text
    assert "Upshot Newsletter" not in result.plain_text
    assert "Privacy Policy" not in result.plain_text


def test_nyt_generic_syndication_extracts_local_newspaper_copy():
    canonical_url = (
        "https://www.nytimes.com/2026/04/15/us/"
        "floods-michigan-cheboygan-dams-evacuation.html"
    )
    syndicated_url = (
        "https://example.net/nation-world-news/"
        "dam-failure-could-imperil-thousands/"
    )
    capture = raw_capture("nyt", canonical_url)
    capture = capture.model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=syndicated_url,
            ),
            "final_url": syndicated_url,
        }
    )
    paragraphs = "".join(
        (
            "<p>New York Times reporting paragraph "
            f"{index} contains substantive details about emergency crews, "
            "evacuation warnings, rising water and the condition of several "
            "dams across northern Michigan.</p>"
        )
        for index in range(1, 9)
    )
    html = f"""
    <!doctype html><html><head>
      <meta property="og:title"
            content="Dam Failure Could Imperil Thousands in Northern Michigan">
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "Dam Failure Could Imperil Thousands in Northern Michigan",
        "datePublished": "2026-04-16T00:05:00Z",
        "author": {{"name": "New York Times"}}
      }}
      </script>
    </head><body>
      <div class="article-content">
        <aside><p>Related article text must not enter the body.</p></aside>
        <div class="post-content">{paragraphs}</div>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert result.quality.body_characters >= 1_000
    assert "paragraph 8" in result.plain_text
    assert "Related article" not in result.plain_text
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_reuters_generic_syndication_removes_benzinga_recirculation_tail():
    canonical_url = (
        "https://www.reuters.com/markets/us/"
        "goldman-sachs-expects-us-fed-deliver-three-rate-cuts-2025"
    )
    syndicated_url = "https://www.benzinga.com/markets/example"
    capture = raw_capture("reuters", canonical_url).model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=syndicated_url,
            ),
            "final_url": syndicated_url,
        }
    )
    reporting = " ".join(
        ["Reuters reporting explains the revised rate forecast."] * 10
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Goldman Expects Three Rate Cuts">
      <meta property="og:url" content="{syndicated_url}">
    </head><body><article>
      <p>{reporting}</p>
      <p>The outlook reflects labor-market weakness and muted tariffs.</p>
      <p>See Also: An unrelated administration story</p>
      <p>Read Next:</p>
      <ul><li>Unrelated technology recommendation</li></ul>
      <p>Disclaimer: This content was produced with AI tools.</p>
      <p>© 2026 Benzinga.com. All rights reserved.</p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "revised rate forecast" in result.plain_text
    assert "See Also" not in result.plain_text
    assert "Read Next" not in result.plain_text
    assert "Benzinga.com" not in result.plain_text


def test_reuters_generic_syndication_removes_partner_widgets():
    canonical_url = (
        "https://www.reuters.com/world/asia-pacific/"
        "japans-nikkei-falls-2026-07-24"
    )
    syndicated_url = (
        "https://www.channelnewsasia.com/business/"
        "japans-nikkei-falls-6275061"
    )
    capture = raw_capture("reuters", canonical_url).model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=syndicated_url,
            ),
            "final_url": syndicated_url,
        }
    )
    reporting = "".join(
        f"<p>Reuters market report paragraph {index} explains the Nikkei "
        "decline, technology shares and investor concerns in detail.</p>"
        for index in range(1, 5)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Japan's Nikkei Falls">
      <meta property="og:url" content="{syndicated_url}">
    </head><body><div class="article-content">
      {reporting}
      <figure><img src="https://dam.mediacorp.sg/editorial.jpg"></figure>
      <section class="block-type--subscription_cta_block">
        <p>Sign up for our newsletters</p>
        <img src="/images/inbox-large.png">
      </section>
      <div class="get-app"><p>Get the CNA app</p>
        <img src="/images/get-app-news.png"></div>
      <div class="whatsapp-group"><p>Get WhatsApp alerts</p>
        <img src="/images/whatsapp-news-logo.png"></div>
    </div></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "paragraph 4" in result.plain_text
    assert "Sign up for our newsletters" not in result.plain_text
    assert "Get the CNA app" not in result.plain_text
    assert len(result.images) == 1
    assert result.images[0].original_url.endswith("editorial.jpg")

    bnn_url = "https://www.bnnbloomberg.ca/business/example"
    bnn_capture = capture.model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=bnn_url,
            ),
            "final_url": bnn_url,
        }
    )
    bnn_html = f"""
    <html><head>
      <meta property="og:title" content="AmEx Raises Forecast">
      <meta property="og:url" content="{bnn_url}">
    </head><body><article>
      {reporting}
      <ul><li>Latest updates on company news here</li></ul>
      <p>Expenses increased while the profit outlook was unchanged.</p>
    </article></body></html>
    """.encode()
    bnn_result = parse_article(
        bnn_html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=bnn_capture,
    )

    assert bnn_result.quality.status.value == "complete"
    assert "Latest updates" not in bnn_result.plain_text
    assert "profit outlook was unchanged" in bnn_result.plain_text


def test_reuters_marketscreener_syndication_scopes_body_and_joins_punctuation():
    canonical_url = (
        "https://www.reuters.com/markets/deals/"
        "infosys-ai-deal-terminated-2023-12-26"
    )
    syndicated_url = (
        "https://www.marketscreener.com/quote/stock/"
        "INFOSYS-LIMITED-9743342/news/example"
    )
    capture = raw_capture("reuters", canonical_url).model_copy(
        update={
            "selected_candidate": CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=syndicated_url,
            ),
            "final_url": syndicated_url,
        }
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Infosys AI Deal Terminated">
      <meta property="og:url" content="{syndicated_url}">
      <meta property="og:image"
            content="https://www.marketscreener.com/images/twitter_MS_fdnoir.png">
    </head><body><article>
      <img src="https://cdn.zonebourse.com/images/membre/chart3.png"
           class="chart" width="16" height="16">
      <div class="txt-s4 article-text">
        <p>Reuters reporting paragraph explains why the global company
        ended its artificial-intelligence agreement with Infosys, how the
        decision affects planned digital services, why technology companies
        face uncertainty, and what investors expect during the next quarter.
        The report also describes the original contract, its duration and
        the artificial-intelligence platforms involved in the agreement.</p>
        <p>The former chief financial officer</p>
        <p>resigned</p>
        <p>.</p>
        <p>Shares had gained during the quarter before the announcement,
        according to market data cited in the report.</p>
      </div>
      <img src="https://www.reuters.com/images/reuters.jpg">
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=canonical_url,
        raw_capture=capture,
    )

    assert result.quality.status.value == "complete"
    assert "resigned." in result.plain_text
    assert "\n\n." not in result.plain_text
    assert "chart3.png" not in result.body_html
    assert "reuters.jpg" not in result.body_html
    assert all(not image.should_archive for image in result.images)
    assert result.extraction.parser_version == "reuters-parser/0.7.25"


def test_nyt_parser_normalizes_legacy_interactive_quiz():
    questions = "".join(
        f"""
        <div class="multiple-choice-question">
          <figure><img src="https://static01.nyt.com/quiz-{index}.jpg">
            <figcaption>Quiz photograph {index}</figcaption>
          </figure>
          <div class="question-text">{index + 1} of 3 Question {index}
            asks readers about a reported health finding.</div>
          <div class="question-answers">
            <div class="answer-text">First possible answer {index}</div>
            <div class="answer-text">Second possible answer {index}</div>
            <div class="answer-text">Third possible answer {index}</div>
          </div>
        </div>
        """
        for index in range(3)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="The Weekly Health Quiz">
      <meta property="article:published_time"
            content="2017-04-21T09:00:00Z">
    </head><body>
      <div class="interactive-graphic">{questions}</div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2017/04/21/"
            "well/live/health-quiz.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "interactive"
    assert len(
        [block for block in result.blocks if block.type.value == "heading"]
    ) == 3
    assert len(
        [block for block in result.blocks if block.type.value == "list"]
    ) == 3
    assert "Third possible answer 2" in result.plain_text
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_prefers_substantive_interactive_story_over_image_metadata():
    paragraphs = "".join(
        f"<p>Interactive narrative paragraph {index} preserves substantive "
        "reporting, participant testimony and historical context.</p>"
        for index in range(1, 9)
    )
    html = f"""
    <html><head>
      <script type="application/ld+json">{{
        "@type": "NewsArticle",
        "headline": "Coming Out in a Pandemic",
        "datePublished": "2021-06-26T09:00:00Z",
        "image": [
          "https://static01.nyt.com/interactive-lead.jpg",
          "https://static01.nyt.com/interactive-second.jpg"
        ]
      }}</script>
    </head><body>
      <article class="interactive">
        <div class="g-story g-freebird">{paragraphs}</div>
      </article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2021/06/26/"
            "opinion/coming-out.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "opinion"
    assert "paragraph 8" in result.plain_text
    assert result.quality.body_characters >= 800
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_recovers_gallery_from_preloaded_data_before_js_config():
    image_blocks = [
        {
            "__typename": "ImageBlock",
            "media": {
                "__typename": "Image",
                "caption": {"text": f"Illustration panel {index}"},
                "crops": [
                    {
                        "renditions": [
                            {
                                "__typename": "ImageRendition",
                                "url": (
                                    "https://static01.nyt.com/images/"
                                    f"panel-{index}-superJumbo.jpg"
                                ),
                                "width": 2048,
                                "height": 1536,
                            }
                        ]
                    }
                ],
            },
        }
        for index in range(3)
    ]
    initial_data = {
        "data": {
            "article": {
                "sprinkledBody": {
                    "__typename": "DocumentBlock",
                    "content": image_blocks,
                }
            }
        }
    }
    html = f"""
    <html><head>
      <meta property="og:title" content="An Illustrated Television Essay">
      <meta property="article:published_time"
            content="2025-08-23T09:00:00Z">
    </head><body><article></article>
      <script>
        window.__preloadedData = {{
          "initialData": {json.dumps(initial_data)},
          "config": {{"gate": function(value) {{ return value; }}}}
        }};
      </script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2025/08/23/arts/"
            "television/illustrated-essay.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "gallery"
    assert len(result.images) == 3
    assert "Illustration panel 2" in result.plain_text


def test_nyt_parser_recovers_lazy_itemprop_visual_essay():
    figures = "".join(
        f"""
        <figure itemprop="associatedMedia"
                itemtype="http://schema.org/ImageObject"
                itemid="https://static01.nyt.com/images/drawing-{index}.jpg">
          <figcaption>Drawing panel {index}</figcaption>
        </figure>
        """
        for index in range(4)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="An Actor's Face in Drawings">
      <meta property="article:published_time"
            content="2020-01-02T15:00:29Z">
    </head><body><main><article>
      <header><p>An illustrated examination of an actor's career.</p></header>
      {figures}
    </article></main></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2020/01/02/"
            "movies/actor-face.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "gallery"
    assert len(result.images) == 4
    assert "Drawing panel 3" in result.plain_text


def test_nyt_parser_recovers_legacy_single_image_op_art():
    html = b"""
    <html><head>
      <title>Judge This Book by Its Cover - Op-Art - NYTimes.com</title>
      <meta property="og:title" content="Judge This Book by Its Cover">
      <meta property="article:published_time"
            content="2012-08-18T09:00:00Z">
    </head><body>
      <div class="ledeStory">
        <div class="storyHeader">Op-Art | Chip Kidd</div>
        <div class="storySummary">
          A graphic artist redesigns a novel for the election season.
        </div>
        <img src="http://graphics8.nytimes.com/images/op-art-custom.jpg">
      </div>
      <div class="interactiveFooter"><div class="module">
        Illustration by Chip Kidd.
      </div></div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2012/08/19/"
            "opinion/sunday/opart.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "gallery"
    assert len(result.images) == 1
    assert "graphic artist redesigns" in result.plain_text


def test_nyt_parser_accepts_complete_associated_press_sports_brief():
    body = (
        "Kohei Uchimura of Japan breezed to his record sixth world "
        "gymnastics championship title in Glasgow."
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Japanese Sets Title Record">
      <meta property="article:published_time"
            content="2015-10-31T09:00:00Z">
    </head><body><main>
      <div>Sports | Sports Briefing | Gymnastics</div>
      <div>By THE ASSOCIATED PRESS</div>
      <div class="story-body"><p>{body}</p></div>
    </main></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2015/10/31/"
            "sports/japanese-sets-title-record.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]
    assert result.plain_text == body


def test_parser_falls_back_to_catalog_publication_time():
    canonical_url = "https://apnews.com/article/catalog-date"
    body = " ".join(["Substantive article sentence."] * 30)
    html = f"""
    <html>
      <head><meta property="og:title" content="Catalog dated story"></head>
      <body><div data-key="article"><p>{body}</p></div></body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=canonical_url,
        raw_capture=raw_capture("ap", canonical_url),
    )

    assert result.published_at == datetime(
        2020,
        1,
        1,
        tzinfo=timezone.utc,
    )
    assert "missing-published-at" not in result.quality.warnings


def test_parser_keeps_legitimate_short_brief_and_removes_legacy_nyt_noise():
    canonical_url = (
        "https://www.nytimes.com/2016/01/07/sports/example.html"
    )
    html = b"""
    <html>
      <head>
        <meta name="pdate" content="20160107">
        <meta property="og:title" content="A short news brief">
      </head>
      <body>
        <article>
          <div class="story-body">
            <p>Advertisement</p>
            <p itemprop="articleBody">Seven former players pleaded not guilty
            to charges related to home invasions and an assault. Another
            defendant was arraigned.</p>
            <p class="story-print-citation">A version of this brief appears in
            print. Order Reprints | Today's Paper | Subscribe</p>
          </div>
        </article>
      </body>
    </html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
    )

    assert result.quality.status.value == "complete"
    assert "Advertisement" not in result.plain_text
    assert "Order Reprints" not in result.plain_text
    assert "Seven former players" in result.plain_text


def test_parser_extracts_bloomberg_timeline_feature():
    canonical_url = (
        "https://www.bloomberg.com/features/2016/example.html"
    )
    timeline_items = "".join(
        f"""
        <article class="event">
          <div class="copy">
            <div class="dates">{2000 + index}</div>
            <div class="text">Career milestone number {index} includes
            substantive biographical reporting and context.</div>
          </div>
        </article>
        """
        for index in range(8)
    )
    html = f"""
    <html>
      <head>
        <meta property="og:title" content="How Did I Get Here? Example">
        <meta name="pdate" content="20160109">
      </head>
      <body>
        <div id="main">
          <div class="timeline_header">
            <h1>Example Person</h1>
            <div id="current-title">Chief executive officer</div>
          </div>
          <div class="timeline">
            <article class="event title">
              <div class="text">Work Experience</div>
            </article>
            {timeline_items}
          </div>
        </div>
      </body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=canonical_url,
    )

    assert result.quality.status.value == "complete"
    assert "Chief executive officer" in result.plain_text
    assert "Career milestone number 7" in result.plain_text
    assert any(
        block.type.value == "heading" and block.text == "Work Experience"
        for block in result.blocks
    )


def test_parser_deduplicates_responsive_text_and_image_blocks():
    canonical_url = "https://apnews.com/article/responsive-duplicates"
    repeated = (
        "This reporting paragraph is repeated in responsive page variants "
        "and must appear only once in normalized article content."
    )
    html = f"""
    <html>
      <head>
        <meta property="og:title" content="Responsive duplicate test">
        <meta name="pub_date" content="20200101">
      </head>
      <body>
        <div data-key="article">
          <p>{repeated}</p>
          <p>{repeated}</p>
          <p>Another substantive paragraph supplies additional context and
          ensures this remains a complete article extraction.</p>
          <img src="https://dims.apnews.com/example.jpg">
          <img src="https://dims.apnews.com/example.jpg">
        </div>
      </body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=canonical_url,
    )

    assert result.quality.status.value == "complete"
    assert result.plain_text.count(repeated) == 1
    assert len(
        [
            block
            for block in result.blocks
            if block.type.value == "image"
        ]
    ) == 1
    assert [block.position for block in result.blocks] == list(
        range(len(result.blocks))
    )


def test_ft_parser_accepts_image_led_data_story_without_prose():
    images = "\n".join(
        (
            "<figure><img "
            f"src='https://www.ft.com/__origami/service/image/{index}.png'>"
            "</figure>"
        )
        for index in range(7)
    )
    html = f"""
    <html>
      <head>
        <meta property="og:title" content="City statistics">
        <meta property="article:published_time"
              content="2018-10-26T10:00:35Z">
      </head>
      <body>
        <article>
          <div class="article__content-body">
            {images}
          </div>
        </article>
      </body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/image-led-story",
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 7
    assert "body-too-short" not in result.quality.warnings


def test_ft_parser_merges_structured_copy_with_dom_media():
    article_body = (
        "City data comparing population, housing, transport and household "
        "income across two metropolitan areas, with explanatory context."
    )
    images = "\n".join(
        (
            "<figure><img "
            f"src='https://www.ft.com/__origami/service/image/{index}.png'>"
            "</figure>"
        )
        for index in range(3)
    )
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">
          {{
            "@type": "NewsArticle",
            "headline": "City statistics",
            "datePublished": "2018-10-26T10:00:35Z",
            "articleBody": {json.dumps(article_body)}
          }}
        </script>
      </head>
      <body><article><div class="article__content-body">
        {images}
      </div></article></body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/structured-media-story",
    )

    assert result.quality.status.value == "complete"
    assert article_body in result.plain_text
    assert result.quality.images_selected == 3
    assert len(
        [block for block in result.blocks if block.type.value == "image"]
    ) == 3


def test_ft_parser_preserves_crossword_pdf_and_removes_branding_noise():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="FT Weekend Magazine Crossword Number 449">
      <meta property="article:published_time"
            content="2019-08-09T09:00:28Z">
      <meta property="og:image"
            content="http://im.ft-static.com/m/img/social/og-ft-logo-large.png">
    </head><body>
      <div class="article__content-body">
        <p>
          <a href="http://prod-upp-image-read.ft.com/crossword-asset">
            Download crossword PDF
          </a>
        </p>
        <p>FT.com also brings you the crossword from Monday to Saturday.</p>
        <p>Copyright The Financial Times Limited. All rights reserved.
          Please don't cut articles from FT.com and redistribute by email
          or post to the web.</p>
      </div>
    </body></html>
    """

    article = parse_article(
        html,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "36cdff7a-a8ed-11e9-b6ee-3cdf3174eb89"
        ),
    )

    assert article.content_type.value == "interactive"
    assert article.quality.status.value == "complete"
    assert article.quality.images_selected == 0
    assert "Copyright The Financial Times" not in article.plain_text
    assert [
        block.embed_url for block in article.blocks if block.embed_url
    ] == [
        "http://prod-upp-image-read.ft.com/crossword-asset"
    ]
    assert article.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_removes_flattened_newsletter_cards():
    reporting_before = " ".join(["FT reporting before module."] * 20)
    reporting_after = " ".join(["FT reporting after module."] * 20)
    html = f"""
    <html><head>
      <meta property="og:title" content="FT company report">
      <meta property="article:published_time"
            content="2019-10-11T00:00:00Z">
    </head><body><article>
      <p>{reporting_before}</p>
      <div class="n-content-layout">
        <div class="n-content-layout__container">
          <h2>Daily newsletter</h2>
          <div class="n-content-layout__slot">
            <figure><img src="https://www.ft.com/newsletter-card.jpg"></figure>
          </div>
        </div>
      </div>
      <p>DAILY NEWSLETTER — Are you interested in the latest company
      news? Every morning our City reporter delivers it to your inbox.</p>
      <p>Sign up here with one click</p>
      <p>{reporting_after}</p>
      <p>Sign up for the newsletter by clicking here.</p>
      <p>Lex recommends the FT’s Due Diligence newsletter, a curated
      briefing. Click here to sign up.</p>
      <p>Do you want to receive Lex in your inbox? Sign up for the
      weekly Best of Lex email at www.ft.com/newsletters.</p>
      <p>Follow @FTMag on Twitter to find out about our latest stories
      first. Subscribe to our podcast.</p>
      <p>The FT is free to read today. You can share this article using
      the buttons at the top.</p>
      <p>The Financial Times is making key coronavirus coverage free to
      read to help everyone stay informed. Find the latest here.</p>
      <p>If you are a subscriber and would like to receive alerts when
      Lex articles are published, just click the button “Add to myFT”.</p>
      <p>Follow Philip Stephens with myFT and on Twitter.</p>
      <p>Sign up to our Due Diligence newsletter to keep up to date
      with the top M&amp;A stories and sharp analysis.</p>
      <p>For more, sign up for our House &amp; Home Unlocked weekly
      newsletter.</p>
      <p>FT premium subscribers can sign up here. Please send feedback
      to due.diligence@ft.com.</p>
      <p>Lex publishes two popular newsletters for premium subscribers.
      Please sign up at ft.com/newsletters.</p>
      <p>HOUSE &amp; HOME UNLOCKED FT subscribers can sign up for our
      weekly email newsletter. Sign up here with one click.</p>
      <p>FT subscribers can sign up for the email version here and
      non-subscribers here.</p>
      <p>Coronavirus Business Update Sign up here for our newsletter
      chronicling the epidemic’s impact on markets and global business.</p>
      <h2>Related stories</h2>
      <ul><li>Unrelated recirculated story</li></ul>
      <p>Tuesday's parliamentary schedule follows.</p>
    </article></body></html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/example-newsletter",
    )

    assert article.quality.status.value == "complete"
    assert "FT reporting before module." in article.plain_text
    assert "FT reporting after module." in article.plain_text
    assert "Tuesday's parliamentary schedule" in article.plain_text
    assert "DAILY NEWSLETTER" not in article.plain_text
    assert not any(
        "newsletter-card.jpg" in image.original_url
        for image in article.images
    )
    assert "Sign up here" not in article.plain_text
    assert "Sign up for the newsletter" not in article.plain_text
    assert "Lex recommends" not in article.plain_text
    assert "Follow @FTMag" not in article.plain_text
    assert "The FT is free to read today" not in article.plain_text
    assert "key coronavirus coverage free" not in article.plain_text
    assert "Add to myFT" not in article.plain_text
    assert "Follow Philip Stephens" not in article.plain_text
    assert "Sign up to our Due Diligence" not in article.plain_text
    assert "House & Home Unlocked weekly" not in article.plain_text
    assert "FT premium subscribers" not in article.plain_text
    assert "Lex publishes two popular newsletters" not in article.plain_text
    assert "HOUSE & HOME UNLOCKED" not in article.plain_text
    assert "email version here" not in article.plain_text
    assert "Coronavirus Business Update" not in article.plain_text
    assert "Related stories" not in article.plain_text
    assert "Unrelated recirculated story" not in article.plain_text
    assert "Do you want to receive Lex" not in article.plain_text
    assert article.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_strips_attached_syndication_copyright_suffix():
    reporting = " ".join(["Syndicated FT reporting sentence."] * 20)
    html = f"""
    <html><head>
      <meta property="og:title" content="Syndicated FT report">
      <meta property="article:published_time"
            content="2025-03-11T12:00:00Z">
    </head><body>
      <article class="article-body-wrapper">
        <p>{reporting}</p>
        <p>
          “That is good for them,” said Chris.
          – Copyright The Financial Times Limited 2025. All rights reserved.
        </p>
      </article>
    </body></html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "2164f23c-9602-4bcd-9c39-f310861dfd48"
        ),
    )

    assert article.quality.status.value == "complete"
    assert "That is good for them" in article.plain_text
    assert "Copyright The Financial Times" not in article.plain_text
    assert article.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_strips_standalone_syndication_copyright_footer():
    reporting = " ".join(["Syndicated FT reporting sentence."] * 20)
    html = f"""
    <html><head>
      <meta property="og:title" content="Syndicated FT report">
      <meta property="article:published_time"
            content="2019-09-30T12:00:00Z">
    </head><body>
      <article class="article-body-wrapper">
        <p>{reporting}</p>
        <p>Copyright The Financial Times Limited 2019. All rights reserved.</p>
        <p>.</p>
      </article>
    </body></html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "a263d2c6-e13e-11e9-9743-db5a370481bc"
        ),
    )

    assert article.quality.status.value == "complete"
    assert "Syndicated FT reporting sentence." in article.plain_text
    assert "Copyright The Financial Times" not in article.plain_text
    assert "." not in [block.text for block in article.blocks]
    assert article.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_classifies_uuid_podcast_and_preserves_audio_source():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Barclays under fire on climate">
      <meta property="article:published_time"
            content="2020-02-04T12:00:00Z">
    </head><body><div class="article__content-body">
      <audio controls data-o-component="o-audio"
             data-audio-subtype="podcast"
             data-content-id="example">
        <source src="https://media.acast.com/ft-banking-weekly/example/media.mp3"
                type="audio/mpeg">
      </audio>
      <p>Your browser does not support playing this file but you can still
      download the MP3.</p>
      <p>The banking team discusses climate policy and provides sufficient
      context for this archived podcast episode.</p>
      <p>A transcript for this podcast is currently unavailable.</p>
    </div></body></html>
    """

    article = parse_article(
        html,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "0e20fd1c-3a01-4fbe-85bd-8b5b0c99a460"
        ),
    )

    assert article.content_type.value == "audio"
    assert article.quality.status.value == "complete"
    assert [
        block.embed_url for block in article.blocks if block.embed_url
    ] == [
        "https://media.acast.com/ft-banking-weekly/example/media.mp3"
    ]


def test_ft_parser_preserves_legacy_brightcove_video_embeds():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Election report with video analysis">
      <meta property="article:published_time"
            content="2015-05-08T00:00:00Z">
    </head><body><div class="article__content-body">
      <p>The election report contains complete written analysis and
      explains the result with enough historical context.</p>
      <amp-brightcove data-account="47628783001"
                      data-player="default"
                      data-embed="default"
                      data-video-id="4224995535001">
      </amp-brightcove>
      <p>A second paragraph records the response and preserves the rest of
      the original Financial Times report.</p>
    </div></body></html>
    """

    article = parse_article(
        html,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "f350d9ac-f4fa-11e4-8a42-00144feab7de"
        ),
    )

    assert article.content_type.value == "article"
    assert article.quality.status.value == "complete"
    assert [
        block.embed_url for block in article.blocks if block.embed_url
    ] == [
        "https://players.brightcove.net/47628783001/"
        "default_default/index.html?videoId=4224995535001"
    ]


def test_ft_parser_converts_structured_image_placeholder_to_body_block():
    asset_id = "66874fc6-23f5-11e6-9d4d-c11776a5124d"
    placeholder_url = (
        "http://com.ft.imagepublish.upp-prod-eu.s3.amazonaws.com/"
        + asset_id
    )
    lead_url = "http://prod-upp-image-read.ft.com/" + asset_id
    body = (
        f"[{placeholder_url}]\n"
        "Left, a subject pictured during the reported event. "
        "The complete article begins with substantive reporting and "
        "explains the dispute using named sources and documentary context."
        "\n\nA second paragraph preserves the response and the remainder "
        "of the original Financial Times report."
    )
    structured = {
        "@type": "NewsArticle",
        "headline": "A report with a structured image placeholder",
        "datePublished": "2016-05-27T23:00:26Z",
        "articleBody": body,
        "image": {"@type": "ImageObject", "url": lead_url},
    }
    html = f"""
    <html><head>
      <script type="application/ld+json">{json.dumps(structured)}</script>
    </head><body><main class="subscription-barrier"></main></body></html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/structured-image",
    )

    assert article.quality.status.value == "complete"
    assert article.quality.images_selected == 1
    assert article.blocks[0].type.value == "image"
    assert article.blocks[0].asset_id == article.images[0].asset_id
    assert set(article.images[0].candidate_urls) == {
        placeholder_url,
        lead_url,
    }
    assert placeholder_url not in article.plain_text
    assert "complete article begins" in article.plain_text


def test_ft_parser_uses_json_ld_article_body_when_dom_is_paywalled():
    article_body = "\n\n".join(
        [
            (
                f"Paragraph {index} contains substantive financial reporting "
                "and enough context for a complete archived story."
            )
            for index in range(1, 7)
        ]
    )
    html = f"""
    <html>
      <head>
        <title>Subscribe to read | Financial Times</title>
        <script type="application/ld+json">
          {{
            "@type": "NewsArticle",
            "headline": "Structured FT article",
            "datePublished": "2022-09-07T17:00:26Z",
            "articleBody": {json.dumps(article_body)}
          }}
        </script>
      </head>
      <body><main class="subscription-barrier"></main></body>
    </html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/example",
    )

    assert article.quality.status.value == "complete"
    assert len(article.blocks) == 6
    assert "Paragraph 1" in article.plain_text
    assert article.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_recovers_images_flattened_into_json_ld_article_body():
    first_image = (
        "http://com.ft.imagepublish.upp-prod-eu.s3.amazonaws.com/"
        "first-image"
    )
    second_image = (
        "http://com.ft.imagepublish.upp-prod-eu.s3.amazonaws.com/"
        "second-image"
    )
    article_body = "\n\n".join(
        [
            (
                "Agency description that is not article prose "
                f"[{first_image}]\n"
                "The lead image caption © Reuters"
                "The complete report begins with substantive reporting and "
                "enough context to identify the real body."
            ),
            (
                "The complete article begins with substantive reporting and "
                "enough context to identify the real body"
            ),
            (
                "Second agency description that is not article prose "
                f"[{second_image}]\n"
                "The second image caption © AP"
                "The reporter repeats that the complete article begins with "
                "substantive reporting and enough context to identify the "
                "real body before explaining the development."
            ),
        ]
    )
    html = f"""
    <html>
      <head>
        <title>Subscribe to read | Financial Times</title>
        <script type="application/ld+json">
          {{
            "@type": "NewsArticle",
            "headline": "Structured FT article with embedded images",
            "datePublished": "2017-07-18T17:00:02Z",
            "articleBody": {json.dumps(article_body)}
          }}
        </script>
      </head>
      <body><main class="subscription-barrier"></main></body>
    </html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/embedded-images",
    )

    assert article.quality.status.value == "complete"
    assert [image.original_url for image in article.images] == [
        first_image,
        second_image,
    ]
    assert [image.caption for image in article.images] == [
        "The lead image caption",
        "The second image caption",
    ]
    assert [image.credit for image in article.images] == [
        "Photo: Reuters",
        "Photo: AP",
    ]
    assert "Agency description" not in article.plain_text
    assert "[http" not in article.plain_text
    assert article.plain_text.casefold().count(
        "the complete article begins"
    ) == 1
    assert "The reporter repeats" in article.plain_text


def test_ft_parser_uses_photo_hint_to_split_unknown_credit_from_body():
    first_image = "https://images.example.com/nippon-paint.jpg"
    second_image = "https://images.example.com/theatre.jpg"
    article_body = "\n\n".join(
        [
            (
                "Agency description (Photo by Thomas White/Reuters) "
                f"[{first_image}]\n"
                "Nippon Paint headquarters © Thomas White/Reuters"
                "Nippon Paint has agreed a major transaction with its "
                "largest shareholder and provided detailed terms."
            ),
            (
                "The transaction creates a regional coatings group and "
                "requires regulatory approval in several markets."
            ),
            (
                "Stage production (Photo by Ahron R. Foster) "
                f"[{second_image}]\n"
                "The leading actors on stage © Ahron R. Foster "
                "Like the families in the original novel, the characters "
                "are unhappy in different ways throughout the production."
            ),
        ]
    )
    html = f"""
    <html><head>
      <script type="application/ld+json">
        {{
          "@type": "NewsArticle",
          "headline": "FT report with flattened photo credits",
          "datePublished": "2020-08-21T12:00:00Z",
          "articleBody": {json.dumps(article_body)}
        }}
      </script>
    </head><body><main class="subscription-barrier"></main></body></html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/photo-credit-boundaries",
    )

    assert article.quality.status.value == "complete"
    assert [image.caption for image in article.images] == [
        "Nippon Paint headquarters",
        "The leading actors on stage",
    ]
    assert [image.credit for image in article.images] == [
        "Photo: Thomas White/Reuters",
        "Photo: Ahron R. Foster",
    ]
    assert "Nippon Paint has agreed" in article.plain_text
    assert "Like the families in the original novel" in article.plain_text
    assert all(len(image.credit or "") < 100 for image in article.images)
    assert article.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_rejects_ft_chinese_percentage_preview():
    html = """
    <html><head>
      <meta property="og:title"
            content="AbbVie buys Apogee for $10.9bn">
      <meta property="article:published_time"
            content="2026-06-22T20:39:26Z">
    </head><body>
      <div class="article__content-body">
        <p>AbbVie is buying biotech Apogee Therapeutics in a $10.9bn
        deal, strengthening its pipeline for immunology medicines.</p>
        <p>Shareholders will receive cash at a premium under the deal.</p>
        <p>The two companies commented on the proposed acquisition.</p>
        <div class="clearfloat"><strong>
          您已阅读12%（426字），剩余88%（3217字）包含更多重要信息，
        </strong>订阅以继续探索完整内容，并享受更多专属服务。</div>
      </div>
    </body></html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/percentage-preview",
    )

    assert article.quality.status.value == "partial"
    assert "truncated-body" in article.quality.warnings
    assert article.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_extracts_legacy_story_content():
    paragraphs = "".join(
        f"<p>Legacy Financial Times reporting paragraph {index} contains "
        "substantive archived details and historical context.</p>"
        for index in range(1, 7)
    )
    html = f"""
    <html>
      <head>
        <title>Legacy FT report - FT.com</title>
        <meta property="og:title" content="Legacy FT report - FT.com">
        <meta name="description" content="An archived Financial Times report.">
      </head>
      <body>
        <div class="fullstoryBody">
          <span class="time">May 28, 2011 12:44 am</span>
          <div id="storyContent">{paragraphs}</div>
        </div>
      </body>
    </html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "3786ab78-8886-11e0-afe1-00144feabdc0"
        ),
    )

    assert article.quality.status.value == "complete"
    assert len(article.blocks) == 6
    assert "Legacy Financial Times reporting paragraph 1" in article.plain_text
    assert article.published_at == datetime(
        2011, 5, 28, 0, 44, tzinfo=timezone.utc
    )
    assert article.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_accepts_image_led_cartoon_and_deduplicates_origami_urls():
    raw_image = (
        "https://d1e00ek4ebabms.cloudfront.net/production/"
        "image-led-cartoon.jpg?source=next-article"
    )
    wrapped_once = (
        "https://www.ft.com/__origami/service/image/v2/images/raw/"
        + quote(raw_image, safe="")
        + "?width=1200"
    )
    wrapped_twice = (
        "https://www.ft.com/__origami/service/image/v2/images/raw/"
        + quote(wrapped_once, safe="")
        + "?width=700"
    )
    article_body = "A weekly cartoon about markets and working life."
    structured = {
        "@type": "NewsArticle",
        "headline": "Imposter syndrome — a cartoon",
        "datePublished": "2024-08-24T04:00:00Z",
        "wordCount": 9,
        "articleBody": article_body,
        "image": {
            "@type": "ImageObject",
            "url": wrapped_once,
            "width": 2048,
            "height": 1152,
        },
    }
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">{json.dumps(structured)}</script>
        <meta property="og:image" content="{wrapped_twice}">
      </head>
      <body>
        <article><div class="article__content-body">
          <p>{article_body}</p>
          <img src="{raw_image}">
        </div></article>
      </body>
    </html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/image-led-cartoon",
    )

    assert article.quality.status.value == "complete"
    assert article.content_type.value == "gallery"
    assert article.quality.images_selected == 1
    assert len(article.images) == 1
    assert article.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_promotes_origami_images_and_deduplicates_raw_lead():
    lead_raw = (
        "http://prod-upp-image-read.ft.com/"
        "3b1050e6-326a-11e9-bb0c-42459962a812"
    )
    lead_wrapped = (
        "https://www.ft.com/__origami/service/image/v2/images/raw/"
        + quote(lead_raw, safe="")
        + "?fit=scale-down&source=next&width=900"
    )
    body_raw = (
        "http://prod-upp-image-read.ft.com/"
        "9c98486e-3288-11e9-bd3a-8b2a211d90d5"
    )
    body_wrapped = (
        "https://www.ft.com/__origami/service/image/v2/images/raw/"
        + quote(body_raw, safe="")
        + "?fit=scale-down&source=next&width=700"
    )
    reporting = " ".join(["FT image reporting sentence."] * 30)
    structured = {
        "@type": "NewsArticle",
        "headline": "FT image report",
        "datePublished": "2019-02-18T00:00:00Z",
        "articleBody": reporting,
        "image": lead_raw,
    }
    html = f"""
    <html><head>
      <script type="application/ld+json">{json.dumps(structured)}</script>
      <meta property="og:image" content="{lead_wrapped}">
    </head><body><article>
      <p>{reporting}</p>
      <figure><img src="{body_wrapped}">
        <figcaption>Distinct body photograph.</figcaption>
      </figure>
    </article></body></html>
    """.encode()

    article = parse_article(
        html,
        publisher="ft",
        canonical_url="https://www.ft.com/content/image-report",
    )

    assert len(article.images) == 2
    lead = next(image for image in article.images if image.role.value == "lead")
    body = next(image for image in article.images if image.role.value == "body")
    assert lead.original_url == lead_raw
    assert lead_wrapped.replace("width=900", "width=1200") in (
        lead.candidate_urls
    )
    assert body.original_url.endswith("source=next&width=1200")
    assert body_wrapped in body.candidate_urls


def test_ap_parser_removes_legacy_newsletter_promo_and_separator():
    reporting = " ".join(["AP reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="AP regional report">
      <meta property="article:published_time"
            content="2018-04-12T12:00:00Z">
    </head><body><article>
      <p>{reporting}</p>
      <div data-ap-readmore>
        <button class="ap-readmore-btn">Read More</button>
      </div>
      <button class="ReadMore-more-button">Read More</button>
      <p>___</p>
      <p>More AP college football: https://apnews.com/college-football.
      Sign up for the AP’s weekly newsletter showcasing our best
      reporting: http://apne.ws/example</p>
      <p>•••</p>
      <p>Sign up for “Politics in Focus,” a weekly newsletter showcasing
      the AP’s best political reporting: http://apne.ws/example</p>
      <p>For more lottery results, go to Jackpot.com |
      Order Lottery Tickets</p>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/article/example",
    )

    assert result.quality.status.value == "complete"
    assert "AP reporting sentence." in result.plain_text
    assert "weekly newsletter" not in result.plain_text
    assert "Politics in Focus" not in result.plain_text
    assert "•••" not in result.plain_text
    assert "Jackpot.com" not in result.plain_text
    assert "___" not in result.plain_text
    assert "<button" not in result.body_html
    assert "data-ap-readmore" not in result.body_html
    assert result.extraction.parser_version == "ap-parser/0.6.17"


def test_ap_parser_extracts_story_html_from_embedded_state():
    story_html = "".join(
        f"<p>Embedded AP reporting paragraph {index} provides substantive "
        "details about the archived story.</p>"
        for index in range(1, 7)
    )
    state = {
        "content": {
            "data": {
                "urn:publicid:ap.org:example": {
                    "storyHTML": story_html,
                }
            }
        }
    }
    html = f"""
    <html>
      <head>
        <meta property="og:title" content="Embedded AP article">
        <meta property="article:published_time"
              content="2021-05-28T07:24:28Z">
      </head>
      <body>
        <script>
          window['titanium-config'] = {{"env": "prod"}};
          window['titanium-state'] = {json.dumps(state)};
        </script>
      </body>
    </html>
    """.encode()

    article = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/article/example",
    )

    assert article.quality.status.value == "complete"
    assert len(article.blocks) == 6
    assert "paragraph 6" in article.plain_text
    assert article.extraction.parser_version == "ap-parser/0.6.17"


def test_ap_parser_accepts_complete_ranked_archive_record():
    html = b"""
    <html><head>
      <script type="application/ld+json">{
        "@type": "NewsArticle",
        "headline": "#4. Grande Sausage Breakfast Burrito - Jack in the Box",
        "datePublished": "2017-05-01T22:07:21Z",
        "keywords": ["Archive"],
        "image": [{
          "url": "https://dims.apnews.com/resize/?url=https%3A%2F%2Fassets.apnews.com%2Fdefaultshareimage-copy.png"
        }]
      }</script>
    </head><body>
      <div class="RichTextStoryBody">
        <p>Calories: 1,044 Fat (g): 90 Sodium (mg): 2,131 Sugar (g): 5</p>
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=(
            "https://apnews.com/article/"
            "33ae8525434145048dfd4ed6823f7788"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]
    assert result.images == []
    assert result.extraction.parser_version == "ap-parser/0.6.17"


def test_ap_parser_classifies_metadata_only_box_score_as_data_content():
    html = b"""
    <html><head><script type="application/ld+json">{
      "@type": "NewsArticle",
      "name": "AP News",
      "datePublished": "2019-10-18T01:30:14Z",
      "keywords": ["BKN--Heat-Magic Box", "Basketball"]
    }</script></head><body><main></main></body></html>
    """

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=(
            "https://apnews.com/5c0273821ea1fb1161129680fb7ebd5e"
        ),
    )

    assert result.headline == "BKN--Heat-Magic Box"
    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]


def test_ap_parser_accepts_short_spanish_news_alert():
    description = (
        "PARIS (AP) — Fiscal en París: Célula terrorista neutralizada "
        "estaba lista para actuar."
    )
    payload = json.dumps(
        {
            "@type": "NewsArticle",
            "headline": "Alerta Noticioso de AP",
            "datePublished": "2015-11-18T18:28:23Z",
            "description": description,
            "keywords": ["General news", "APAlertaNoticioso"],
        }
    )
    html = (
        "<html><head><script type='application/ld+json'>"
        f"{payload}</script></head><body>"
        f"<div class='RichTextStoryBody'><p>{description}</p></div>"
        "</body></html>"
    ).encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/general-news-example",
    )

    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]
    assert result.plain_text == description


def test_ap_parser_classifies_metadata_only_nomination_result():
    html = b"""
    <html><head><script type="application/ld+json">{
      "@type": "NewsArticle",
      "headline": "NY-House-6-nominated",
      "datePublished": "2020-06-24T03:04:19Z",
      "keywords": ["NY-House-6-nominated"]
    }</script></head><body><main></main></body></html>
    """

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=(
            "https://apnews.com/"
            "ny-house-6-nominated-a93b2abc2ae34d3382594f588f48af9f"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]
    assert result.plain_text == "NY-House-6-nominated"


def test_ap_parser_classifies_metadata_only_lottery_result():
    html = b"""
    <html><head><script type="application/ld+json">{
      "@type": "NewsArticle",
      "name": "AP News",
      "datePublished": "2019-07-18T03:10:00Z",
      "keywords": ["Lotteries", "General news", "Classic Lotto"]
    }</script></head><body><main></main></body></html>
    """

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=(
            "https://apnews.com/lotteries-general-news-example"
        ),
    )

    assert result.headline == "Classic Lotto"
    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]


def test_ap_parser_classifies_metadata_only_race_call():
    html = b"""
    <html><head><script type="application/ld+json">{
      "@type": "NewsArticle",
      "headline": "AP Race Call: Republican Scott Perry wins reelection",
      "datePublished": "2024-11-06T06:22:00Z",
      "keywords": ["2024 Race Call", "Pennsylvania"]
    }</script></head><body><main></main></body></html>
    """

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=(
            "https://apnews.com/article/race-call-perry-wins-example"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]


def test_ap_parser_classifies_metadata_only_state_winners():
    html = b"""
    <html><head><script type="application/ld+json">{
      "@type": "NewsArticle",
      "headline": "HI-Winners",
      "datePublished": "2018-08-12T14:48:46Z",
      "keywords": ["HI-Winners"]
    }</script></head><body><main></main></body></html>
    """

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=(
            "https://apnews.com/hi-winners-example"
        ),
    )

    assert result.headline == "HI-Winners"
    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]
    assert result.plain_text == "HI-Winners"


def test_ap_parser_recovers_metadata_only_county_election_result():
    html = b"""
    <html><head><script type="application/ld+json">{
      "@type": "NewsArticle",
      "headline": "LA-CAmend-2-UnanimousJury-Cnty",
      "datePublished": "2018-11-07T04:00:00Z",
      "keywords": ["LA-CAmend-2-UnanimousJury-Cnty"]
    }</script></head><body><main></main></body></html>
    """

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=(
            "https://apnews.com/"
            "la-camend-2-unanimousjury-cnty-example"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.plain_text == "LA-CAmend-2-UnanimousJury-Cnty"


def test_ap_parser_recovers_self_contained_structured_description():
    description = (
        "Sweden's central bank says it will cut its key interest rate to "
        "a record low of minus 0.50 percent, saying the measure is necessary "
        "to safeguard its target of increasing inflation. The bank said its "
        "expansionary monetary policy had strengthened the economy and that "
        "it hopes the cut will increase inflation to its target in 2017."
    )
    payload = json.dumps(
        {
            "@type": "NewsArticle",
            "headline": "Sweden cuts key interest rate",
            "datePublished": "2016-02-11T10:00:00Z",
            "description": description,
            "keywords": ["General news", "International News"],
        }
    )
    html = (
        "<html><head><script type='application/ld+json'>"
        f"{payload}</script></head><body><main></main></body></html>"
    ).encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/general-news-example",
    )

    assert result.quality.status.value == "complete"
    assert result.headline == "Sweden cuts key interest rate"
    assert result.plain_text == description


def test_ap_parser_prefers_full_dom_story_over_truncated_description():
    html = b"""
    <html><head>
      <script type="application/ld+json">
      {
        "@type": "NewsArticle",
        "headline": "AP Interview: Rose says he finally gets it",
        "datePublished": "2010-10-19T21:03:00Z",
        "description": "So, he's 'fessing up."
      }
      </script>
    </head><body>
      <div class="RichTextStoryBody">
        <p>CINCINNATI (AP) - Pete Rose says he finally understands what the
        former baseball commissioner meant when he asked him to reconfigure
        his life after receiving a lifetime ban.</p>
        <p>Baseball's hits king told The Associated Press that the realization
        took him many years and that he is now ready to acknowledge it.</p>
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=(
            "https://apnews.com/article/"
            "mlb-sports-43db9ba6c12f42eb8c496302bc337b43"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.plain_text.startswith("CINCINNATI (AP)")
    assert "Baseball's hits king" in result.plain_text


def test_ap_parser_uses_descriptive_wire_slug_for_generic_headline():
    description = (
        "Police say a man entered a Houston auto shop where he used to "
        "work and fatally shot two people before killing himself. "
        "Investigators said the motive for the shooting was not clear."
    )
    payload = json.dumps(
        {
            "@type": "NewsArticle",
            "headline": "AP News",
            "datePublished": "2017-06-30T22:00:00Z",
            "description": description,
            "keywords": [
                "General news",
                "US-Auto-Shop-Shooting-Houston",
                "Houston",
            ],
        }
    )
    html = (
        "<html><head><script type='application/ld+json'>"
        f"{payload}</script></head><body><main></main></body></html>"
    ).encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/general-news-example",
    )

    assert result.quality.status.value == "complete"
    assert result.headline == "US-Auto-Shop-Shooting-Houston"
    assert result.plain_text == description


def test_ap_parser_accepts_self_contained_archive_brief():
    description = (
        "The percentage of the workforce living in the county remains "
        "below the goal."
    )
    payload = json.dumps(
        {
            "@type": "NewsArticle",
            "headline": "20170329 a indicator report",
            "datePublished": "2017-03-29T12:00:00Z",
            "description": description,
            "keywords": ["Archive"],
        }
    )
    html = (
        "<html><head><script type='application/ld+json'>"
        f"{payload}</script></head><body><main></main></body></html>"
    ).encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/article/archive-brief",
    )

    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]
    assert result.plain_text == description


def test_ap_parser_recovers_structured_score_bulletin_description():
    description = "GIRLS HOCKEY Herb Brooks Holiday Classic (Bronze Division)"
    payload = json.dumps(
        {
            "@type": "NewsArticle",
            "headline": "Wednesday's Scores",
            "datePublished": "2020-12-30T23:00:00Z",
            "description": description,
            "keywords": ["MN-HKO--Prep Scores", "Hockey", "Sports"],
        }
    )
    html = (
        "<html><head><script type='application/ld+json'>"
        f"{payload}</script></head><body><main></main></body></html>"
    ).encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/wednesdays-scores-example",
    )

    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["structured-short-record"]
    assert result.plain_text == description


def test_ap_parser_rejects_promotional_description_shell():
    description = "Visit ProFootballWeekly.com | View Latest E-Edition"
    payload = json.dumps(
        {
            "@type": "NewsArticle",
            "headline": "2017 fantasy football fresh start",
            "datePublished": "2017-08-01T00:00:00Z",
            "description": description,
            "keywords": ["Archive", "Football", "Sports"],
        }
    )
    html = (
        "<html><head><script type='application/ld+json'>"
        f"{payload}</script></head><body><main></main></body></html>"
    ).encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/article/sports-football-example",
    )

    assert result.quality.status.value == "unsupported"
    assert result.plain_text == ""


def test_ap_parser_restores_race_call_from_structured_description():
    description = (
        "Former President Donald Trump won Pennsylvania on Wednesday, "
        "defeating his opponent in the critical battleground state. "
        "The Associated Press declared Trump the winner after its analysis "
        "determined there was no path for the trailing candidate."
    )
    payload = json.dumps(
        {
            "@type": "NewsArticle",
            "headline": "AP Race Call: Donald Trump wins Pennsylvania",
            "datePublished": "2024-11-06T07:24:56Z",
            "description": description,
            "keywords": ["2024 Race Call", "Pennsylvania"],
        }
    )
    html = (
        "<html><head><script type='application/ld+json'>"
        f"{payload}</script></head><body><main></main></body></html>"
    ).encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url=(
            "https://apnews.com/article/"
            "race-call-trump-wins-pennsylvania-president"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.plain_text == description
    assert result.images == []


def test_ap_parser_extracts_lazy_loaded_carousel_gallery():
    slides = "".join(
        f"""
        <div class="Carousel-slide">
          <img
            src="data:image/svg+xml;base64,placeholder"
            data-flickity-lazyload="https://dims.apnews.com/photo-{index}.jpg"
            data-flickity-lazyload-srcset="
              https://dims.apnews.com/photo-{index}.jpg 1x,
              https://dims.apnews.com/photo-{index}-2x.jpg 2x"
            alt="Editorial caption for photograph {index}.">
        </div>
        """
        for index in range(4)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="AP photos of the week">
      <meta property="article:published_time"
            content="2025-05-08T19:32:18Z">
    </head><body>
      <main class="Page-main">
        <bsp-carousel class="Carousel">
          <div class="Carousel-slides">{slides}</div>
        </bsp-carousel>
      </main>
    </body></html>
    """.encode()

    article = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/article/photo-collection-example",
    )

    assert article.content_type.value == "gallery"
    assert article.quality.status.value == "complete"
    assert len(article.blocks) == 4
    assert len(article.images) == 4
    assert article.images[2].caption == "Editorial caption for photograph 2."
    assert article.images[2].original_url.endswith("photo-2.jpg")


def test_parser_includes_gallery_captions_in_plain_text():
    canonical_url = (
        "https://www.nytimes.com/2023/09/20/t-magazine/example.html"
    )
    caption = (
        "Clockwise from top left: a bag, a pair of shoes, a coat and another "
        "bag, with complete product details and photography credit."
    )
    html = f"""
    <html>
      <head>
        <meta property="og:title" content="Cozy Accessories">
        <meta property="og:description" content="A visual fashion report.">
        <meta name="pub_date" content="20230920">
      </head>
      <body>
        <section name="articleBody">
          <figure>
            <img src="https://static01.nyt.com/gallery.jpg">
            <figcaption>{caption}</figcaption>
          </figure>
          <p>Set design and photography production credits.</p>
        </section>
      </body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "gallery"
    assert caption in result.plain_text
    assert "production credits" in result.plain_text


def test_parser_classifies_interactive_urls():
    canonical_url = (
        "https://www.nytimes.com/interactive/2020/05/12/example.html"
    )
    body = " ".join(["Interactive election reporting."] * 20)
    html = f"""
    <html>
      <head>
        <meta property="og:title" content="Election results">
        <meta name="pub_date" content="20200512">
      </head>
      <body>
        <section name="articleBody"><p>{body}</p></section>
      </body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
    )

    assert result.content_type.value == "interactive"


def test_nyt_parser_extracts_interactive_roundup_body():
    canonical_url = (
        "https://www.nytimes.com/interactive/2023/01/20/"
        "briefing/the-weekender.html"
    )
    html = b"""
    <html>
      <head>
        <meta property="og:title" content="The Weekender">
        <meta property="article:published_time"
              content="2023-01-20T21:32:37Z">
      </head>
      <body>
        <div class="interactive-body">
          <h2>Times editors have handpicked stories for you to enjoy.</h2>
          <h2>A substantive story selected for this edition</h2>
          <p>The summary provides enough reporting context to make this
          interactive roundup useful as normalized article content.</p>
        </div>
      </body>
    </html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "interactive"
    assert "handpicked stories" in result.plain_text
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_extracts_birdkit_attendee_sheet():
    canonical_url = (
        "https://www.nytimes.com/interactive/2025/04/26/world/"
        "pope-funeral.html"
    )
    attendees = ",".join(
        (
            f'{{name:"Attendee {index}",'
            f'caption:"Public role number {index}"}}'
        )
        for index in range(30)
    )
    html = f"""
    <html>
      <head>
        <meta property="og:title" content="Funeral attendees">
        <meta property="article:published_time"
              content="2025-04-26T12:00:00Z">
      </head>
      <body>
        <article>
          <div class="interactive-body">
            <p>Explore the photo.</p>
            <script>
              const data = {{sheets:{{attendees:[{attendees}]}}}};
            </script>
          </div>
        </article>
      </body>
    </html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
    )

    assert result.quality.status.value == "complete"
    assert result.quality.block_count == 30
    assert "Attendee 0 — Public role number 0" in result.plain_text
    assert "Attendee 29 — Public role number 29" in result.plain_text


def test_nyt_parser_extracts_preloaded_graphql_image_gallery():
    state = {}
    for index in range(3):
        media_id = f"Image:gallery-{index}"
        crop_id = f"{media_id}.crop"
        rendition_id = f"ImageRendition:gallery-{index}"
        state[f"$Article.sprinkledBody.content.{index}"] = {
            "__typename": "ImageBlock",
            "media": {"id": media_id},
        }
        state[media_id] = {
            "__typename": "Image",
            "credit": f"Artist {index}",
            "crops": [{"id": crop_id}],
        }
        state[crop_id] = {
            "__typename": "ImageCrop",
            "renditions": [{"id": rendition_id}],
        }
        state[rendition_id] = {
            "__typename": "ImageRendition",
            "url": f"https://static01.nyt.com/gallery-{index}.jpg",
            "width": 1600,
            "height": 1200,
        }
    payload = json.dumps({"initialState": state})
    html = f"""
    <html><head>
      <meta property="og:title" content="A year in editorial cartoons">
      <meta property="article:published_time" content="2018-12-06T11:00:00Z">
    </head><body>
      <script>window.__preloadedData = {payload};</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url="https://www.nytimes.com/2018/12/06/opinion/cartoons.html",
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert [image.original_url for image in result.images] == [
        f"https://static01.nyt.com/gallery-{index}.jpg"
        for index in range(3)
    ]
    assert len(result.blocks) == 3


def test_nyt_parser_extracts_ordered_diptych_visual_story():
    body_id = "$Article:round-bags.sprinkledBody"
    header_id = f"{body_id}.content@filterEmpty.0"
    diptych_id = f"{body_id}.content@filterEmpty.1"
    state = {
        body_id: {
            "__typename": "DocumentBlock",
            "content@filterEmpty": [
                {"id": header_id},
                {"id": diptych_id},
            ],
        },
        header_id: {
            "__typename": "HeaderFullBleedVerticalBlock",
            "ledeMedia": {"id": f"{header_id}.ledeMedia"},
        },
        f"{header_id}.ledeMedia": {
            "__typename": "ImageBlock",
            "media": {"id": "Image:bag-0"},
        },
        diptych_id: {
            "__typename": "DiptychBlock",
            "imageOne": {"id": "Image:bag-1"},
            "imageTwo": {"id": "Image:bag-2"},
        },
    }
    for index in range(3):
        image_id = f"Image:bag-{index}"
        crop_id = f"{image_id}.crop"
        rendition_id = f"ImageRendition:bag-{index}"
        legacy_caption = (
            "Customers at a Sprint store. Sprint wants to block a "
            "T-Mobile/AT&amp;T deal."
            if index == 0
            else f"<strong>Bag {index}</strong>, $100."
        )
        state[image_id] = {
            "__typename": "Image",
            "legacyHtmlCaption": legacy_caption,
            "credit": "Studio Photographer",
            "crops": [{"id": crop_id}],
        }
        state[crop_id] = {
            "__typename": "ImageCrop",
            "renditions": [{"id": rendition_id}],
        }
        state[rendition_id] = {
            "__typename": "ImageRendition",
            "url": f"https://static01.nyt.com/bag-{index}.jpg",
            "width": 1200,
            "height": 1500,
        }
    payload = json.dumps({"initialState": state})
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Cylindrical, Oval and Bucket Bags">
      <meta property="article:published_time"
            content="2022-03-03T13:00:00Z">
    </head><body>
      <script>window.__preloadedData = {payload};</script>
    </body></html>
    """.encode()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = parse_article(
            html,
            publisher="nyt",
            canonical_url=(
                "https://www.nytimes.com/2022/03/03/t-magazine/"
                "round-bags-spring-fashion.html"
            ),
        )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert len(result.blocks) == 3
    assert [image.caption for image in result.images] == [
        "Customers at a Sprint store. Sprint wants to block a "
        "T-Mobile/AT&T deal.",
        "Bag 1 , $100.",
        "Bag 2 , $100.",
    ]
    assert caught == []
    assert [image.credit for image in result.images] == [
        "Credit: Studio Photographer"
        for _ in range(3)
    ]


def test_nyt_parser_recovers_legacy_interactive_graphic():
    summary = (
        "With well-connected sales representatives and relationships with "
        "influential doctors, a small company became the main supplier of "
        "heart devices to a large hospital virtually overnight."
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Quick Change in Heart Devices">
      <meta property="article:published_time"
            content="2011-04-02T00:00:00Z">
    </head><body id="interactiveABC">
      <div id="interactiveShell">
        <div class="storySummary"><span class="summary">{summary}</span></div>
        <div id="interactiveFreeFormMain">
          <img src="http://graphics8.nytimes.com/packages/images/
                     newsgraphics/2011/implant-web.jpg">
        </div>
        <div id="interactiveFooter">
          <p class="credit">Sources: Company documents; Medical Center</p>
        </div>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2011/04/02/business/"
            "a-quick-change-in-heart-devices.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "interactive"
    assert result.plain_text == (
        f"{summary}\n\nSources: Company documents; Medical Center"
    )
    assert len(result.images) == 1
    assert result.quality.images_selected == 1


def test_nyt_parser_recovers_embedded_interactive_lede_tables():
    html = b"""
    <html><head>
      <meta property="og:title" content="A Dual Review of What's New">
      <meta property="article:published_time"
            content="2014-09-19T00:00:00Z">
    </head><body>
      <div id="story-body" class="story-body">
        <div class="lede-container">
          <figure class="interactive interactive-embedded lede">
            <div class="interactive-graphic">
              <p class="summary">Two critics assess a group of new products.</p>
              <table><tr>
                <td><span class="summary">The first critic explains why
                the rotating tray is useful and thoughtfully designed.</span></td>
                <td><img src="https://static01.nyt.com/tray.jpg">
                    <span class="caption">A rotating tray, $525.</span></td>
                <td><span class="summary">The second critic explains why
                reaching across the table is easier.</span></td>
              </tr></table>
            </div>
          </figure>
        </div>
        <p>A short fallback description.</p>
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2014/09/19/t-magazine/"
            "a-dual-review.html"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert "first critic explains" in result.plain_text
    assert "second critic explains" in result.plain_text
    assert result.quality.images_selected == 1


def test_nyt_parser_does_not_replace_article_with_short_interactive_nav():
    article_text = (
        "The nominations included expected contenders, but the academy also "
        "recognized several historic achievements and overlooked other films. "
    ) * 8
    html = f"""
    <html><head>
      <meta property="og:title" content="The Snubs and Surprises">
      <meta property="article:published_time"
            content="2018-01-23T00:00:00Z">
    </head><body>
      <article id="story" class="story theme-main">
        <div class="story-body">
          <div class="story-content"><p>{article_text}</p></div>
          <figure class="interactive interactive-embedded lede">
            <div class="interactive-graphic">
              <p>Oscars 2018</p>
              <p>Catch Up Snubs and Surprises Ballot Stream Nominees</p>
              <img src="https://static01.nyt.com/oscars-navigation.jpg">
            </div>
          </figure>
        </div>
      </article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2018/01/23/movies/"
            "oscars-snubs-surprises.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "historic achievements" in result.plain_text
    assert len(result.plain_text) > 500


def test_nyt_parser_treats_interpreted_by_cartoon_as_complete_gallery():
    html = b"""
    <html><head>
      <meta property="og:title" content="North Korea's Rocket Launch">
      <meta name="description" content="As interpreted by Heng.">
      <meta property="article:published_time"
            content="2012-12-16T00:00:00Z">
      <meta property="og:image"
            content="https://static01.nyt.com/cartoon-superJumbo.jpg">
    </head><body>
      <div id="story-body" class="story-body">
        <p>As interpreted by Heng.</p>
        <img src="https://static01.nyt.com/cartoon-large.jpg">
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2012/12/16/opinion/global/"
            "north-koreas-rocket-launch.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert result.quality.images_selected >= 1


def test_nyt_parser_treats_legacy_heng_opinion_art_as_complete_gallery():
    html = b"""
    <html><head>
      <meta property="og:title" content="Nationalism in Japan">
      <meta name="description"
            content="Was the shrine visit a sign of things to come in 2014?">
      <meta property="article:published_time"
            content="2013-12-29T00:00:00Z">
      <meta property="og:image"
            content="http://graphics8.nytimes.com/images/2013/opinion/29-iht-edhengart-videoSixteenByNine1050.jpg">
    </head><body>
      <div id="articleBody">
        <p>Was the shrine visit a sign of things to come in 2014?</p>
        <img src="http://graphics8.nytimes.com/images/2013/opinion/29-iht-edhengart-articleLarge.jpg">
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2013/12/29/opinion/"
            "heng-nationalism-in-japan.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert result.quality.images_selected >= 1


def test_nyt_parser_treats_short_transcript_with_document_embed_as_complete():
    html = b"""
    <html><head>
      <meta property="og:title" content="F.B.I. Transcript">
      <meta property="article:published_time"
            content="2018-02-23T00:00:00Z">
    </head><body>
      <article class="story">
        <p>From the F.B.I. tip line.</p>
        <script>
          DV.flexLoad(
            "//www.documentcloud.org/documents/4386532-document022318.js"
          );
        </script>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2018/02/23/us/"
            "fbi-transcript.html"
        ),
    )

    assert result.content_type.value == "transcript"
    assert result.quality.status.value == "complete"
    assert any(block.type.value == "embed" for block in result.blocks)


def test_nyt_parser_preserves_legacy_interactive_documents():
    html = b"""
    <html><head>
      <meta property="og:title" content="Poll Results">
      <meta property="article:published_time"
            content="2012-07-18T00:00:00Z">
    </head><body>
      <div id="interactiveShell">
        <div id="interactiveFreeFormMain">
          <a href="http://s3.documentcloud.org/poll.pdf">Poll (PDF)</a>
          <a href="http://s3.documentcloud.org/poll.txt">Poll (Text)</a>
          <script>
            DV.load("//www.documentcloud.org/documents/402362-poll.js", {
              container: "#viewer"
            });
          </script>
        </div>
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2012/07/19/us/"
            "poll-results.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "interactive"
    assert [block.embed_url for block in result.blocks] == [
        "http://s3.documentcloud.org/poll.pdf",
        "http://s3.documentcloud.org/poll.txt",
        "https://www.documentcloud.org/documents/402362-poll",
    ]


def test_nyt_parser_recovers_div_only_interactive_text():
    sections = "".join(
        f"""
        <div class="g-section">
          <div class="g-source">({index}) Source rule {index} with details.</div>
          <div class="g-translation">
            Translation: explanation {index} gives readers useful context.
          </div>
        </div>
        """
        for index in range(1, 8)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Areas of Autonomy">
      <meta property="article:published_time" content="2014-08-06T00:00:00Z">
    </head><body>
      <div class="interactive-graphic">
        <div class="g-intro">
          An introduction explaining what the proposed governance rules mean.
        </div>
        {sections}
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2014/08/06/sports/"
            "ncaa-autonomy-translation.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Translation: explanation 7" in result.plain_text
    assert len(result.blocks) == 15


def test_nyt_parser_preserves_documents_from_modern_legacy_shell():
    html = b"""
    <html><head>
      <meta property="og:title" content="S&amp;P Capital IQ Report">
      <meta property="article:published_time" content="2014-05-01T00:00:00Z">
    </head><body>
      <main><article class="story theme-interactive">
        <a href="http://s3.documentcloud.org/report.pdf">Report (PDF)</a>
        <a href="http://s3.documentcloud.org/report.txt">Report (Text)</a>
        <script>
          DV.flexLoad("//www.documentcloud.org/documents/2956923-report.js");
        </script>
      </article></main>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2014/05/01/upshot/"
            "report-docviewer.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert [block.embed_url for block in result.blocks] == [
        "http://s3.documentcloud.org/report.pdf",
        "http://s3.documentcloud.org/report.txt",
        "https://www.documentcloud.org/documents/2956923-report",
    ]


def test_nyt_parser_recovers_legacy_flex_magazine_payload():
    payload = {
        "data": {
            "gobig": (
                "http://www.nytimes.com/slideshow/2013/09/08/"
                "magazine/look-bond.slideshow.jsonp"
            ),
            "col2": {
                "text": (
                    "Ian Fleming took the name James Bond from the author of "
                    "a book he found in Jamaica. The photographs examine the "
                    "recurring characters, vehicles and objects in the films."
                )
            },
            "col3": {
                "video": {
                    "promo": (
                        "http://graphics8.nytimes.com/images/"
                        "video-bondgirl-custom1.jpg"
                    ),
                    "title": "Bond Girl",
                    "caption": "An actor reads lines dubbed for Dr. No.",
                    "credit": "Taryn Simon",
                },
                "stats": [
                    {"key": "James Bond books", "value": 14},
                    {"key": "Women photographed", "value": 65},
                ],
            },
        }
    }
    html = f"""
    <html><head>
      <meta property="og:title" content="The Bond Market">
      <meta property="article:published_time" content="2013-09-06T00:00:00Z">
    </head><body>
      <div id="interactiveShell"><div id="interactiveFreeFormMain">
        <script>
          function getFlexData() {{ return {json.dumps(payload)}; }}
        </script>
      </div></div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2013/09/08/magazine/"
            "look-bond-market.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Ian Fleming took the name James Bond" in result.plain_text
    assert "Women photographed: 65" in result.plain_text
    assert result.blocks[1].embed_url.endswith("look-bond.slideshow.jsonp")
    assert len(result.images) == 1


def test_nyt_parser_recovers_legacy_flex_story_columns():
    payload = {
        "data": {
            "items": [
                {
                    "story": [
                        {
                            "headline": "How to Have Grace",
                            "byline": "Robert Battle<br>Artistic Director",
                            "text": (
                                "<p>Grace is achieved through vulnerability, "
                                "forgiveness and the choice to keep moving.</p>"
                            ),
                            "thumb": (
                                "http://graphics8.nytimes.com/packages/"
                                "images/magazine/grace.png"
                            ),
                            "photo": "",
                            "bottom": "",
                            "pcred": "The New York Times",
                        },
                        {
                            "headline": "The Big Profile",
                            "byline": "Dave Itzkoff",
                            "text": (
                                "<p>The musician describes family life and "
                                "a newly released record in this profile.</p>"
                            ),
                            "photo": (
                                "http://graphics8.nytimes.com/packages/"
                                "images/magazine/profile.png"
                            ),
                            "thumb": "",
                            "bottom": "",
                            "pcred": "",
                        },
                    ]
                }
            ]
        }
    }
    html = f"""
    <html><head>
      <meta property="og:title" content="What's Hillary Doing?">
      <meta property="article:published_time" content="2013-08-23T00:00:00Z">
    </head><body>
      <div id="interactiveShell"><div id="interactiveFreeFormMain">
        <script>
          function getFlexData() {{ return {json.dumps(payload)}; }}
        </script>
      </div></div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2013/08/25/magazine/"
            "one-page-magazine25.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Grace is achieved through vulnerability" in result.plain_text
    assert "The musician describes family life" in result.plain_text
    assert len(result.images) == 2


def test_nyt_parser_recovers_legacy_flex_audio_track():
    payload = {
        "data": {
            "lede": {
                "description": "Cantor David Rosenberg chanting in Hebrew."
            },
            "tracks": {
                "track": {
                    "source": (
                        "http://graphics8.nytimes.com/packages/audio/"
                        "nyregion/davidrosenberg.mp3"
                    ),
                    "duration": 56,
                }
            },
        }
    }
    html = f"""
    <html><head>
      <meta property="og:title" content="A Prayer Sung by a Long-Lost Son">
      <meta property="article:published_time" content="2015-07-09T00:00:00Z">
    </head><body><article class="story theme-interactive theme-main">
      <div class="interactive-graphic">
        <script>
          function getFlexData() {{ return {json.dumps(payload)}; }}
        </script>
      </div>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2015/07/12/nyregion/"
            "12rosenberg-audio.html"
        ),
    )

    assert result.content_type.value == "audio"
    assert result.quality.status.value == "complete"
    assert "Cantor David Rosenberg" in result.plain_text
    assert any(
        block.embed_url and block.embed_url.endswith("davidrosenberg.mp3")
        for block in result.blocks
    )


def test_nyt_parser_recovers_legacy_newsgraphic_nodes_outside_article():
    paragraphs = "".join(
        f"""
        <div class="story g-text">
          <p class="g-body">
            Victim profile {index}: this paragraph preserves detailed
            reporting about a person and the attack that affected the family.
          </p>
        </div>
        """
        for index in range(1, 9)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="The Human Toll of Terror Attacks">
      <meta property="article:published_time" content="2016-07-26T00:00:00Z">
    </head><body>
      <article class="story"><div class="interactive-graphic"></div></article>
      {paragraphs}
      <div class="story g-image"><div class="g-item-image">
        <img src="https://static01.nyt.com/newsgraphics/victim.jpg"
             alt="Victim portrait">
      </div></div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2016/07/27/world/"
            "human-toll-of-terror-attacks.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Victim profile 8" in result.plain_text
    assert len(result.images) == 1


def test_nyt_parser_does_not_treat_regular_story_g_body_as_newsgraphic():
    noise = "".join(
        f"""
        <div class="g-body">
          <img src="https://static01.nyt.com/noise-{index}.jpg">
          Unrelated generated module {index} with enough repeated text to
          resemble an old graphics payload but no interactive root.
        </div>
        """
        for index in range(8)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Regular News Article">
      <meta property="article:published_time" content="2016-03-01T00:00:00Z">
    </head><body>
      <article>
        <p>The first reported paragraph contains the actual article text and
        establishes the facts readers need to understand the story.</p>
        <p>The second paragraph adds interviews, context and further details
        from the newspaper's reporting.</p>
      </article>
      {noise}
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2016/03/01/movies/"
            "a-regular-news-article.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.plain_text.startswith("The first reported paragraph")
    assert "Unrelated generated module" not in result.plain_text
    assert len(result.images) == 0


def test_nyt_parser_recovers_2016_story_content_body():
    paragraphs = "".join(
        f"""
        <p class="story-body-text story-content" itemprop="articleBody">
          Legacy story paragraph {index} contains reporting, interviews and
          historical context from the original newspaper article.
        </p>
        """
        for index in range(1, 7)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Legacy Story">
      <meta property="og:description" content="A short article summary.">
      <meta property="article:published_time" content="2016-01-15T00:00:00Z">
    </head><body>
      <article id="story" class="story theme-main">
        <div class="story-body">{paragraphs}
          <p class="story-content">
            <a href="https://example.org/supporting-study.pdf">
              Supporting study
            </a>
          </p>
        </div>
      </article>
      <article class="story theme-summary">
        <div class="story-body"><p>Unrelated recommendation.</p></div>
      </article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2016/01/15/obituaries/"
            "a-legacy-story.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.plain_text.startswith("Legacy story paragraph 1")
    assert "Legacy story paragraph 6" in result.plain_text
    assert "Unrelated recommendation" not in result.plain_text


def test_nyt_parser_recovers_2010_id_based_article_body():
    html = b"""
    <html><head>
      <meta property="og:title" content="An Early Times Article">
      <meta property="article:published_time" content="2010-02-09T00:00:00Z">
    </head><body>
      <div id="articleBody">
        <p>The first paragraph reports the company's quarterly results and
        explains the most important change from the prior year.</p>
        <p>The second paragraph gives revenue figures, analyst expectations
        and additional context for readers.</p>
      </div>
      <div class="articleBody"><div class="articleBody">
        <p>A nested duplicate must not be extracted twice.</p>
      </div></div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2010/02/09/business/"
            "an-early-times-article.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.plain_text.startswith("The first paragraph")
    assert result.plain_text.count("nested duplicate") == 1


def test_nyt_parser_classifies_short_legacy_visual_story_as_gallery():
    html = b"""
    <html><head>
      <meta property="og:title" content="A Visual Fashion Feature">
      <meta property="article:published_time" content="2015-03-25T00:00:00Z">
      <meta property="og:image"
            content="https://static01.nyt.com/images/visual-feature.jpg">
    </head><body>
      <article class="story theme-main">
        <div class="story-body">
          <p class="story-body-text story-content">
            A brief introduction presents the featured design.
          </p>
          <figure itemprop="associatedMedia">
            <img src="https://static01.nyt.com/images/visual-feature.jpg">
            <figcaption>Details of the products shown in the photograph.</figcaption>
          </figure>
        </div>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2015/03/25/t-magazine/"
            "a-visual-fashion-feature.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert len(result.images) == 1


def test_nyt_parser_recovers_hidden_plain_text_timeline_fallback():
    timeline = " ".join(
        f"Year {1900 + index}: students organized a documented campus action."
        for index in range(12)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Campus Activism Timeline">
      <meta property="article:published_time" content="2016-02-07T00:00:00Z">
    </head><body>
      <article class="story">
        <div class="interactive-graphic">
          <script>window.TIMELINE_JSON_PATH = "/timeline.json";</script>
          <div id="timeline_plain_text" style="color:white">{timeline}</div>
        </div>
      </article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2016/02/07/"
            "education/campus-activism-timeline.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.plain_text.startswith("Year 1900")
    assert "Year 1911" in result.plain_text


def test_nyt_parser_preserves_description_for_javascript_only_interactive():
    html = b"""
    <html><head>
      <meta property="og:title" content="Precinct Results">
      <meta property="og:description"
            content="Detailed results in the race for president.">
      <meta property="article:published_time" content="2016-02-01T00:00:00Z">
    </head><body>
      <article class="story theme-interactive">
        <header>
          <h2>Politics</h2>
          <p><time datetime="2016-02-01">FEB. 1, 2016</time></p>
        </header>
        <div class="interactive-graphic"></div>
      </article>
      <script>
        window.resultsAssets =
          "https://int.nyt.com/newsgraphics/2016/results/";
      </script>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2016/02/01/us/"
            "precinct-results.html"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "partial"
    assert result.plain_text == "Detailed results in the race for president."


def test_nyt_parser_recovers_inline_script_image_sequence():
    html = b"""
    <html><head>
      <meta property="og:title" content="The One-Page Magazine">
      <meta property="article:published_time" content="2014-03-21T00:00:00Z">
    </head><body>
      <article class="story"><h2>Magazine</h2></article>
      <div class="interactive-graphic">
        <script>
          window.slides = [
            "http:\\/\\/graphics8.nytimes.com\\/packages\\/one.png",
            "http:\\/\\/graphics8.nytimes.com\\/packages\\/two.jpg",
            "http:\\/\\/graphics8.nytimes.com\\/packages\\/three.png"
          ];
        </script>
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2014/03/23/magazine/"
            "23-one-page-magazine.html"
        ),
    )

    assert result.content_type.value == "interactive"
    assert len(result.images) == 3
    assert result.quality.images_selected == 3


def test_nyt_parser_classifies_interactive_live_path_as_interactive():
    html = b"""
    <html><head>
      <meta property="og:title" content="Weekly Health Quiz">
      <meta property="article:published_time" content="2016-10-14T00:00:00Z">
    </head><body>
      <div class="interactive-graphic"><div id="quiz"></div></div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2016/10/14/well/live/"
            "healthquiz.html"
        ),
    )

    assert result.content_type.value == "interactive"


def test_nyt_parser_recovers_article_path_map_and_deduplicates_sizes():
    responsive_maps = "".join(
        (
            "<img data-src='https://int.nyt.com/newsgraphics/hurricanes/"
            f"maps/img/ian-tracker_{width}_v29.png'>"
        )
        for width in (945, 800, 720, 480, 300)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Keep track of Hurricane Ian.">
      <meta property="article:published_time"
            content="2022-09-27T12:00:00Z">
      <meta property="og:image"
            content="https://static01.nyt.com/vi-assets/images/share/1200x675_nameplate.png">
      <meta name="twitter:image"
            content="https://static01.nyt.com/vi-assets/images/share/1200x900_t.png">
    </head><body>
      <section name="articleBody">
        <h2 class="interactive-headline">
          The Forecast Path of Hurricane Ian
        </h2>
        {responsive_maps}
      </section>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2022/09/27/us/"
            "keep-track-of-hurricane-ian.html"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 1
    assert len(
        [block for block in result.blocks if block.type.value == "image"]
    ) == 1
    assert any(image.role.value == "logo" for image in result.images)
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_classifies_image_only_opinion_cartoon_as_gallery():
    html = b"""
    <html><head>
      <meta property="og:title" content="Wake-up call">
      <meta property="article:published_time" content="2016-02-15T00:00:00Z">
      <meta property="og:image"
            content="https://static01.nyt.com/images/cartoon.jpg">
    </head><body><main><article>
      <img src="https://static01.nyt.com/images/cartoon.jpg">
    </article></main></body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2016/02/15/opinion/"
            "cartoon-heng-on-north-koreas-rocket-launch.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"


def test_nyt_parser_preserves_blank_interactive_promo_destination():
    html = b"""
    <html><head>
      <meta property="og:title" content="Updating: New York Fashion Week">
      <meta name="description"
            content="Editors share highlights and reports from the shows.">
      <meta property="article:published_time" content="2015-09-16T00:00:00Z">
    </head><body>
      <script>
        var destUrl = " https://www.nytimes.com/interactive/projects/fashion ";
      </script>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2015/fashion/"
            "inside-fashion-week-collections-promo.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.content_type.value == "interactive"
    assert "Editors share highlights" in result.plain_text
    assert result.blocks[-1].embed_url == (
        "https://www.nytimes.com/interactive/projects/fashion"
    )


def test_nyt_parser_preserves_page_url_redirect_destination():
    html = b"""
    <html><head>
      <meta property="og:title" content="Court reform">
      <meta name="description" content="A short redirect shell.">
      <meta property="article:published_time" content="2020-10-27T00:00:00Z">
    </head><body>
      <section class="interactive-body interactive-blank"></section>
      <script>
        var page_url = 'https://www.nytimes.com/interactive/2020/10/27/opinion/supreme-court-reform.html' + para;
      </script>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2020/10/27/opinion/"
            "supreme-court-packing.html"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.blocks[-1].embed_url == (
        "https://www.nytimes.com/interactive/2020/10/27/opinion/"
        "supreme-court-reform.html"
    )


def test_nyt_parser_extracts_preloaded_legacy_slideshow():
    state = {
        "$Article.body.header.ledeMedia": {
            "__typename": "SlideshowBlock",
            "media": {"id": "Slideshow:week"},
        },
        "Slideshow:week": {
            "__typename": "Slideshow",
            "slides": [
                {"id": f"Slideshow:week.slides.{index}"}
                for index in range(3)
            ],
        },
    }
    for index in range(3):
        image_id = f"Image:week-{index}"
        rendition_id = f"ImageRendition:week-{index}"
        state[f"Slideshow:week.slides.{index}"] = {
            "__typename": "SlideshowSlide",
            "legacyHtmlCaption": (
                f"<p>Backstage photograph number {index}.</p>"
            ),
            "image": {"id": image_id},
        }
        state[image_id] = {
            "__typename": "Image",
            "credit": "NYT Photographer",
            "crops": [{"id": f"{image_id}.crop"}],
        }
        state[f"{image_id}.crop"] = {
            "__typename": "ImageCrop",
            "renditions": [{"id": rendition_id}],
        }
        state[rendition_id] = {
            "__typename": "ImageRendition",
            "url": f"https://static01.nyt.com/week-{index}.jpg",
            "width": 1600,
            "height": 1200,
        }
    payload = json.dumps({"initialState": state})
    html = f"""
    <html><head>
      <meta property="og:title" content="Off the Runway: Day Five">
      <meta property="article:published_time"
            content="2014-09-09T00:58:07Z">
      <meta property="og:image"
            content="https://static01.nyt.com/newsgraphics/images/icons/defaultPromoCrop.png">
    </head><body>
      <script>window.__preloadedData = {payload};</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2014/09/09/fashion/"
            "off-the-runway-day-five.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert len(result.blocks) == 3
    assert len(result.images) == 3
    assert all("defaultPromoCrop" not in image.original_url
               for image in result.images)


def test_nyt_parser_classifies_preloaded_video_page():
    payload = json.dumps(
        {
            "initialState": {
                "$Article.sprinkledBody.content.0": {
                    "__typename": "VideoBlock"
                }
            }
        }
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Ask the columnist">
      <meta property="article:published_time" content="2018-11-02T11:00:00Z">
      <meta property="og:image" content="https://static01.nyt.com/still.jpg">
    </head><body>
      <script>window.__preloadedData = {payload};</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url="https://www.nytimes.com/2018/11/02/opinion/questions.html",
    )

    assert result.content_type.value == "video"
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_nyt_parser_classifies_legacy_weekly_comic_strip():
    html = b"""
    <html><head>
      <meta property="og:title" content="The Strip">
      <meta name="description"
            content="A weekly comic strip featured in the Sunday Review.">
      <meta property="article:published_time"
            content="2016-01-17T00:00:00Z">
      <meta property="og:image"
            content="https://static01.nyt.com/the-strip-facebookJumbo.png">
    </head><body>
      <article><div id="story-body" class="story-body">
        <figure>
          <img src="https://static01.nyt.com/the-strip-master675.png">
          <figcaption>January 17, 2016 - By Brian McFadden</figcaption>
        </figure>
        <p>A weekly comic strip featured in the Sunday Review.</p>
      </div></article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2016/01/17/opinion/sunday/"
            "the-strip-brian-mcfadden-comics.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert len(result.images) == 2


def test_nyt_parser_extracts_legacy_watching_app_post_body():
    paragraphs = "".join(
        f"<p>Documentary recommendation {index} includes detailed historical "
        "context, availability information and critical analysis.</p>"
        for index in range(8)
    )
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Where to Stream Essential Documentaries">
      <meta name="description"
            content="A guide to important documentary films.">
    </head><body>
      <main>
        <nav><p>Search and watchlist navigation.</p></nav>
        <div class="Post__bodySection">
          <div class="Post__body">
            <button class="SaveToWatchlistButton__saveToWatchlistButton">
              Save to Watch
            </button>
            <button class="LikeButton__likeButton">Like</button>
            {paragraphs}
          </div>
        </div>
      </main>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2017/09/12/watching/"
            "documentaries-where-to-watch.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.body_characters > 500
    assert "Documentary recommendation 7" in result.plain_text
    assert "Search and watchlist navigation" not in result.plain_text
    assert "Save to Watch" not in result.plain_text
    assert "<button" not in result.body_html


def test_nyt_parser_extracts_watching_v2_post_and_visible_date():
    paragraphs = "".join(
        f"<p>Recommendation {index} contains detailed viewing guidance, "
        "critical context and availability information for this week.</p>"
        for index in range(6)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Four Shows to Watch This Week">
      <meta name="description" content="A weekly television guide.">
    </head><body><main>
      <div class="PostV2__postHeader">
        <div class="PostV2__datePublished">March 5, 2018</div>
      </div>
      <div class="PostV2__postBody">
        <h4>I Need an Odd-Ball Comedy</h4>
        {paragraphs}
      </div>
    </main></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2018/03/05/watching/"
            "what-to-watch-this-week-tv.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.published_at.isoformat() == "2018-03-05T00:00:00+00:00"
    assert "Recommendation 5" in result.plain_text
    assert result.quality.block_count == 7


def test_nyt_parser_treats_editorial_cartoon_as_complete_gallery():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="Opinion | Leaders Prepare to Meet">
      <meta property="article:published_time"
            content="2018-04-20T00:00:00Z">
      <meta property="og:image"
            content="https://static01.nyt.com/cartoon.jpg">
    </head><body><article>
      <p>How are the two leaders preparing for their meeting?</p>
      <img src="https://static01.nyt.com/cartoon.jpg">
      <p class="author-bio">The author is an editorial cartoonist.</p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2018/04/20/opinion/"
            "leaders-prepare-to-meet.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert "body-too-short" not in result.quality.warnings


def test_nyt_parser_accepts_explicit_publisher_error_notice():
    html = b"""
    <html><head>
      <meta property="og:title"
            content="This article was published in error.">
      <meta property="article:published_time"
            content="2021-06-08T00:00:00Z">
    </head><body><article><p>
      A mock article intended for a testing system was inadvertently
      published on this page earlier.
    </p></article></body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2021/06/08/admin/"
            "this-article-was-published-in-error.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["publisher-notice"]


def test_nyt_parser_accepts_copyright_removal_notice():
    html = b"""
    <html><head>
      <meta property="og:title" content="An Archived Feature">
      <meta property="article:published_time"
            content="2019-03-06T00:00:00Z">
    </head><body><article>
      <p itemprop="articleBody">
        Editors' Note: This feature has been removed because of a
        copyright dispute.
      </p>
    </article></body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2019/03/06/admin/"
            "an-archived-feature.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.warnings == ["publisher-notice"]


def test_nyt_parser_extracts_denormalized_product_gallery():
    images = []
    for index in range(3):
        images.append(
            {
                "__typename": "ImageBlock",
                "media": {
                    "__typename": "Image",
                    "credit": "Studio",
                    "caption": {"text": f"Shoe {index}"},
                    "crops": [
                        {
                            "renditions": [
                                {
                                    "__typename": "ImageRendition",
                                    "url": (
                                        "https://static01.nyt.com/"
                                        f"shoe-{index}.jpg"
                                    ),
                                    "width": 1200,
                                    "height": 1500,
                                }
                            ]
                        }
                    ],
                },
            }
        )
    payload = json.dumps(
        {
            "initialData": {
                "data": {
                    "article": {
                        "sprinkledBody": {"content": images}
                    }
                }
            },
            "initialState": {},
        }
    ).replace('"initialState": {}', '"config": {"meter": undefined}, '
              '"initialState": {}')
    html = f"""
    <html><head>
      <meta property="og:title" content="Classic Pumps Stage a Comeback">
      <meta property="article:published_time"
            content="2022-08-10T00:00:00Z">
    </head><body>
      <script>window.__preloadedData = {payload};</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2022/08/10/t-magazine/"
            "classic-pumps-heels-shoes.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert len(result.blocks) == 3


def test_nyt_parser_does_not_replace_substantive_article_with_gallery():
    images = [
        {
            "__typename": "ImageBlock",
            "media": {
                "__typename": "Image",
                "caption": {"text": f"Scene {index}"},
                "crops": [{
                    "renditions": [{
                        "__typename": "ImageRendition",
                        "url": f"https://static01.nyt.com/scene-{index}.jpg",
                        "width": 1200,
                        "height": 800,
                    }]
                }],
            },
        }
        for index in range(3)
    ]
    payload = json.dumps({
        "initialData": {
            "data": {
                "article": {"sprinkledBody": {"content": images}}
            }
        },
        "initialState": {},
    })
    paragraphs = "".join(
        "<p>This is substantive reporting paragraph "
        f"{index}, with enough original context and detail to prove that "
        "the prose article must remain the selected body even when its "
        "preloaded data contains several photographs.</p>"
        for index in range(4)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Report With Several Photos">
      <meta property="article:published_time"
            content="2019-06-23T00:00:00Z">
    </head><body>
      <article><section name="articleBody">{paragraphs}</section></article>
      <script>window.__preloadedData = {payload};</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2019/06/23/nyregion/"
            "a-report-with-several-photos.html"
        ),
    )

    assert result.content_type.value == "article"
    assert result.quality.status.value == "complete"
    assert result.quality.body_characters > 500
    assert "substantive reporting paragraph 3" in result.plain_text


def test_nyt_parser_preserves_all_stories_in_interactive_anthology():
    stories = "".join(
        f"""
        <div class="rad-article" id="story-{index}">
          <p class="rad-summary">Summary for contributor {index}.</p>
          <div class="rad-story-body">
            <p class="paragraph">Contributor {index} explains the history
            of this online campaign with independently reported evidence,
            concrete examples and enough detail to form a complete essay.</p>
            <p class="paragraph">A second paragraph records the lasting
            consequences for institutions, communities and public debate.</p>
          </div>
        </div>
        """
        for index in range(3)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Multi-Author Interactive">
      <meta property="article:published_time"
            content="2019-08-15T00:00:00Z">
    </head><body><main>
      <article class="story theme-interactive theme-minimal">
        <div class="interactive-graphic">{stories}</div>
      </article>
    </main></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2019/08/15/opinion/"
            "multi-author-interactive.html"
        ),
    )

    assert result.content_type.value == "opinion"
    assert result.quality.status.value == "complete"
    assert result.quality.body_characters > 600
    for index in range(3):
        assert f"Contributor {index} explains" in result.plain_text


def test_wsj_parser_accepts_complete_short_editorial_letter():
    canonical_url = (
        "https://www.wsj.com/articles/"
        "dad-joke-humor-eye-roll-son-11656628677"
    )
    letter = (
        "Regarding “Life Is More Pun With Dad Jokes”: I’m no stranger "
        "to eye rolls. I think my 12-year-old son has been doing them "
        "for about six years now. I know he’ll eventually grow to "
        "appreciate good humor—probably when he’s a dad. "
        "Warren Tunwall, Iowa City."
    )
    html = f"""
    <html><head>
      <meta property="og:title"
            content="The Dad Joke Meets the Eye Roll">
      <meta name="article.type" content="Letters">
      <meta name="article.type.display" content="Letters">
      <meta name="article.page" content="Letters">
      <meta name="article:word_count" content="57">
    </head><body>
      <article><p data-type="paragraph">{letter}</p></article>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=canonical_url,
        raw_capture=raw_capture("wsj", canonical_url),
    )

    assert result.quality.status.value == "complete"
    assert "body-too-short" not in result.quality.warnings
    assert "Warren Tunwall" in result.plain_text
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_nyt_parser_preserves_image_led_legacy_interactive():
    html = b"""
    <html><head>
      <meta property="og:title" content="Evening Hours">
      <meta property="article:published_time"
            content="2016-06-03T00:00:00Z">
    </head><body><main>
      <article class="story theme-interactive theme-main">
        <div class="interactive-graphic">
          <h2>Fashion &amp; Style | Evening Hours</h2>
          <p>By BILL CUNNINGHAM JUNE 3, 2016</p>
          <p><img src="https://static01.nyt.com/images/party-popup.jpg"
                  width="970" height="1103"></p>
        </div>
      </article>
    </main></body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2016/04/24/fashion/"
            "bill-cunningham-evening-hours.html"
        ),
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 1
    assert any(block.type.value == "image" for block in result.blocks)


def test_nyt_parser_preserves_rendered_legacy_interactive_tables():
    rows = "".join(
        f"<tr><th>Critic {index}</th><td>Film choice {index} with a "
        "detailed explanation of the performance and direction.</td></tr>"
        for index in range(6)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="The Critics Make Their Oscar Choices">
      <meta property="article:published_time" content="2012-12-30T00:00:00Z">
    </head><body>
      <div id="interactiveShell">
        <div class="storySummary">The critics make their Oscar choices.</div>
        <div id="interactiveFreeFormMain">
          <h2>Best Picture</h2>
          <p>Each critic explains the films most deserving of recognition
          after a year of unusually ambitious American cinema.</p>
          <table><tbody>{rows}</tbody></table>
        </div>
      </div>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2012/12/30/movies/"
            "awardsseason/20121230-oscar-picks-feature.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.body_characters > 300
    assert "Critic 5" in result.plain_text
    assert "Film choice 5" in result.plain_text


def test_nyt_parser_recovers_archived_adventure_quiz_resource():
    resource_url = (
        "https://int.nyt.com/assets/adventure/js/"
        "southern-novels-adventure-production.js"
    )
    entities = {
        "quiz": {
            "id": "quiz",
            "type": "quiz",
            "entities": ["question-1", "question-2", "question-3"],
            "data": {},
        }
    }
    for number in range(1, 4):
        entities[f"question-{number}"] = {
            "id": f"question-{number}",
            "type": "multiple_choice_question",
            "data": {},
            "entities": [
                f"prompt-{number}",
                f"answer-{number}-a",
                f"answer-{number}-b",
                f"response-{number}",
            ],
        }
        entities[f"prompt-{number}"] = {
            "id": f"prompt-{number}",
            "type": "text",
            "data": {
                "content": (
                    f"Question {number} asks which Southern state provides "
                    "the setting for this novel and why that geography "
                    "matters to its characters?"
                )
            },
            "entities": [],
        }
        for suffix, state, correct in (
            ("a", "Mississippi", True),
            ("b", "Georgia", False),
        ):
            entities[f"answer-{number}-{suffix}"] = {
                "id": f"answer-{number}-{suffix}",
                "type": "answer",
                "data": {"correct": correct},
                "entities": [f"answer-text-{number}-{suffix}"],
            }
            entities[f"answer-text-{number}-{suffix}"] = {
                "id": f"answer-text-{number}-{suffix}",
                "type": "text",
                "data": {"content": state},
                "entities": [],
            }
        entities[f"response-{number}"] = {
            "id": f"response-{number}",
            "type": "response",
            "data": {"when": "all"},
            "entities": [f"explanation-{number}"],
        }
        entities[f"explanation-{number}"] = {
            "id": f"explanation-{number}",
            "type": "text",
            "data": {
                "content": (
                    "The Book Review explains how the author uses landscape, "
                    "family history and regional memory to shape the story "
                    "in a detailed critical discussion."
                )
            },
            "entities": [],
        }
    serialized = json.dumps(
        {"entitiesById": entities, "root": "quiz"},
        separators=(",", ":"),
    )
    javascript = (
        "module.exports=JSON.parse('"
        + serialized.replace("\\", "\\\\").replace("'", "\\'")
        + "');"
    ).encode()
    html = f"""
    <html><head>
      <meta property="og:title" content="How Well Do You Know These Novels?">
      <meta property="article:published_time" content="2022-10-07T00:00:00Z">
      <meta name="description" content="A literary geography quiz.">
    </head><body><main><article>
      <section class="interactive-content">
        <div id="adventure-project-container">
          <script src="{resource_url}"></script>
        </div>
      </section>
    </article></main></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2022/10/07/books/"
            "review/southern-novels-quiz.html"
        ),
        dependent_resources={resource_url: javascript},
    )

    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.quality.body_characters > 500
    assert "Question 3 asks" in result.plain_text
    # Identical answer labels are intentionally de-duplicated as text blocks.
    assert "Correct answer: Mississippi" in result.plain_text


def test_nyt_parser_preserves_balloteer_quiz_data_endpoint():
    html = b"""
    <html><head>
      <meta property="og:title" content="Weekly Health Quiz">
      <meta name="description" content="Test your knowledge of this week's health news.">
      <meta property="article:published_time" content="2014-04-04T00:00:00Z">
    </head><body>
      <article class="story">
        <div class="interactive-graphic">
          <div id="int-chad-ballot-wrapper-20140404healthquiz"></div>
          <script>
            Chad.embed_init({
              "ballot_slug":"20140404healthquiz",
              "target_id":"#int-chad-ballot-wrapper-20140404healthquiz",
              "question_type":"well_prediction"
            });
          </script>
        </div>
      </article>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2014/04/04/health/"
            "20140404_healthquiz.html"
        ),
    )

    assert result.content_type.value == "interactive"
    embeds = [block for block in result.blocks if block.type.value == "embed"]
    assert [block.embed_url for block in embeds] == [
        "https://www.nytimes.com/svc/int/balloteer/ballot/"
        "20140404healthquiz"
    ]
    assert result.quality.status.value == "complete"


def test_nyt_parser_extracts_exact_liveblog_post_from_preloaded_state():
    canonical_url = (
        "https://www.nytimes.com/live/2022/01/13/business/"
        "markets/osha-vaccine-mandate-businesses"
    )
    article_id = "Article:target"
    body_id = "$Article:target.body"
    headline_id = "$Article:target.headline"
    state = {
        article_id: {
            "__typename": "Article",
            "url": canonical_url,
            "headline": {"id": headline_id},
            "summary": "Companies must decide how to proceed.",
            "firstPublished": "2022-01-13T22:27:01Z",
            "body": {"id": body_id},
        },
        headline_id: {
            "__typename": "CreativeWorkHeadline",
            "default": "Businesses react to the vaccine ruling.",
        },
        body_id: {
            "__typename": "DocumentBlock",
            "content@filterEmpty": [
                {"id": f"$target.paragraph.{index}"}
                for index in range(3)
            ],
        },
    }
    for index in range(3):
        state[f"$target.paragraph.{index}"] = {
            "__typename": "ParagraphBlock",
            "content": [{"id": f"$target.text.{index}"}],
        }
        state[f"$target.text.{index}"] = {
            "__typename": "TextInline",
            "text": (
                f"Paragraph {index} contains the complete archived update "
                "and enough specific reporting detail for validation."
            ),
        }
    payload = json.dumps({"initialState": state})
    html = f"""
    <html><head>
      <meta property="og:title"
            content="Stock Market and Business News: Live Updates">
      <meta property="article:published_time"
            content="2022-01-13T00:00:00Z">
    </head><body><main><p>Market widget</p></main>
      <script>window.__preloadedData = {payload};</script>
    </body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="nyt",
        canonical_url=canonical_url,
    )

    assert result.headline == "Businesses react to the vaccine ruling."
    assert result.quality.status.value == "complete"
    assert result.quality.block_count == 3
    assert result.published_at.isoformat() == "2022-01-13T22:27:01+00:00"


def test_reuters_parser_extracts_numbered_div_paragraphs():
    paragraphs = "".join(
        f"""
        <div data-testid="paragraph-{index}"
             class="article-body__paragraph__2-BtD">
          Paragraph {index} contains substantive Reuters reporting about
          commodity markets, company decisions and the economic context
          needed to understand the development in full.
        </div>
        """
        for index in range(6)
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="Commodity export outlook changes">
      <meta property="article:published_time"
            content="2025-06-30T12:00:00Z">
    </head><body><main><article>
      <div class="article-body__content__17Yit">
        {paragraphs}
        <p data-testid="promo-box">Sign up here.</p>
        <div class="article-body__element__2p5pI">
          <p>Reporting by Example Reporter; Editing by Example Editor</p>
        </div>
      </div>
    </article></main></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/markets/commodities/"
            "commodity-export-outlook-changes-2025-06-30"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.quality.body_characters > 700
    assert "Paragraph 0 contains" in result.plain_text
    assert "Paragraph 5 contains" in result.plain_text
    assert "Sign up here" not in result.plain_text


def test_wsj_parser_recovers_webui_slideshow_state():
    slides = [
        {
            "caption": f"Photograph {index} documents the complete trip.",
            "credit": "Example Photographer for The Wall Street Journal",
            "imageSrc": (
                "https://si.wsj.net/public/resources/images/"
                f"BN-SLIDE-{index}_M.jpg"
            ),
        }
        for index in range(4)
    ]
    state = json.dumps(
        {
            "data": {},
            "id": "webuislideshow",
            "context": {"slides": slides},
        },
        separators=(",", ":"),
    )
    html = f"""
    <html><head>
      <meta property="og:title" content="A Long Weekend in Kauai">
      <meta property="article:published_time"
            content="2018-03-21T12:05:00Z">
      <meta name="article.type" content="Infogrfx Slide Show">
    </head><body><article>
      <p>Where to soak up the scene and scenery.</p>
      <script>
        window.WEBUI_SLIDESHOWS = window.WEBUI_SLIDESHOWS || [];
        window.WEBUI_SLIDESHOWS.push({{
          id: 'example',
          state: {state},
          layout: "full"
        }});
      </script>
    </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "a-long-weekend-in-kauai-packed-with-nature-1521648328"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 4
    assert "Photograph 3 documents" in result.plain_text


def test_bloomberg_parser_recovers_embedded_tax_quiz():
    sections = []
    for number in range(1, 4):
        sections.append(
            f"""
            <section class="question" id="Q{number}">
              <h2>Tax question {number} asks about federal filing rules?</h2>
              <div class="quiz-question">
                <img src="https://assets.bwbx.io/tax-{number}.jpg">
                <p class="captionline">Tax form illustration {number}.</p>
                <p class="creditline">Photographer: Bloomberg</p>
              </div>
              <ol class="quiz-answers">
                <li>First possible answer</li>
                <li>Second possible answer</li>
                <li>Third possible answer</li>
              </ol>
              <div class="navbuttons">And the answer is</div>
            </section>
            <section class="answer" id="A{number}">
              <h2>Tax question {number} asks about federal filing rules?</h2>
              <div class="thisresult">You were right!</div>
              <div>The answer is the second choice. This explanation gives
              detailed context about the federal tax rule, its practical
              effect and the filing deadline taxpayers need to understand.</div>
              <div class="navbuttons">Next</div>
            </section>
            """
        )
    html = (
        "<html><head><title></title></head><body>"
        '<div id="quiz-container">'
        + "".join(sections)
        + "</div></body></html>"
    ).encode()

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/features/2017-tax-quiz/index.html"
        ),
    )

    assert result.headline == "Bloomberg Tax Quiz"
    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 3
    assert "Tax question 3 asks" in result.plain_text
    assert "You were right" not in result.plain_text


def test_bloomberg_parser_uses_first_question_for_untitled_quiz():
    html = b"""
    <html><head><title></title></head><body>
      <div id="quiz-container">
        <section class="question" id="Q1">
          <h2>Which companies make up Buffett's Powerhouse Five?</h2>
          <ol class="quiz-answers">
            <li>Rail, energy and manufacturing companies</li>
            <li>Five large technology companies</li>
          </ol>
        </section>
        <section class="answer" id="A1">
          <h2>Which companies make up Buffett's Powerhouse Five?</h2>
          <p>The answer includes Berkshire's largest non-insurance
          businesses. This explanation provides enough historical and
          financial context for a complete standalone quiz result.</p>
        </section>
      </div>
    </body></html>
    """

    result = parse_article(
        html,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/features/"
            "2016-buffett-quiz/index.html"
        ),
    )

    assert result.headline == (
        "Which companies make up Buffett's Powerhouse Five?"
    )
    assert result.content_type.value == "interactive"
    assert "missing-headline" not in result.quality.warnings
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_nyt_parser_accepts_intentionally_short_corrections_notice():
    result = parse_article(
        b"""
        <html><head>
          <meta name="description"
            content="No corrections appeared in print on Monday, October 07, 2019.">
        </head><body><article>
          <h1>Corrections: October 07, 2019</h1>
          <section name="articleBody"><p>
            No corrections appeared in print on Monday, October 07, 2019.
          </p></section>
        </article></body></html>
        """,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2019/10/07/pageoneplus/"
            "corrections-october-07-2019.html"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "structured-short-record" in result.quality.warnings
    assert "body-too-short" not in result.quality.warnings


def test_nyt_parser_preserves_single_image_comics_review():
    result = parse_article(
        b"""
        <html><head>
          <meta name="description" content="A review in comics format helps
            inquisitive young citizens learn how elections work.">
        </head><body><article>
          <h1>A Review Drawn as a Comic</h1>
          <figure><img itemprop="url"
            src="https://static01.nyt.com/images/comic-superJumbo.jpg"></figure>
          <section name="articleBody"><div></div></section>
        </article></body></html>
        """,
        publisher="nyt",
        canonical_url="https://www.nytimes.com/2020/10/25/books/review/comic.html",
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert result.quality.images_selected == 1
    assert "review in comics format" in result.plain_text


def test_nyt_parser_extracts_prose_inside_nonimage_interactive_figure():
    result = parse_article(
        b"""
        <html><head><meta name="description" content="Mapping damage."></head>
        <body><article><h1>Mapping the Damage</h1>
          <div class="interactive-body">
            <figure><custom-scroller>
              <p>First explanatory paragraph with enough detail to describe
              what the map shows and how the analysis was performed.</p>
              <p>Second explanatory paragraph preserves the findings instead
              of treating this layout-only figure as an image.</p>
            </custom-scroller></figure>
          </div>
        </article></body></html>
        """,
        publisher="nyt",
        canonical_url="https://www.nytimes.com/interactive/2023/map.html",
    )

    assert result.quality.status.value == "complete"
    assert result.quality.block_count == 2
    assert "Second explanatory paragraph" in result.plain_text


def test_nyt_parser_preserves_article_document_card():
    result = parse_article(
        b"""
        <html><head><meta name="description" content="The resignation paves
          the way for the election case to continue against Donald Trump.">
        </head><body><article><h1>Read the Resignation</h1>
          <section name="articleBody"><div>
            <a class="thumbnail-link"
              href="/interactive/2024/03/15/us/resignation-letter.html">
              <img src="https://static01.nyt.com/newsgraphics/documenttools/x.png">
            </a>
            <div><h2>Read the Resignation Letter</h2>
              <a href="/interactive/2024/03/15/us/resignation-letter.html">
                <strong>Read Document</strong> 3 pages
              </a>
            </div>
          </div></section>
        </article></body></html>
        """,
        publisher="nyt",
        canonical_url="https://www.nytimes.com/2024/03/15/us/resignation.html",
    )

    assert result.quality.status.value == "complete"
    assert result.blocks[-1].type.value == "embed"
    assert result.blocks[-1].embed_url.endswith("resignation-letter.html")


def test_nyt_parser_accepts_legacy_short_editorial_cartoon():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Clashes in the Philippines">
          <meta property="article:published_time"
            content="2013-09-22T00:00:00Z">
          <meta property="og:image"
            content="https://static01.nyt.com/cartoon-large.jpg">
          <meta name="description"
            content="For the Philippines, America is the heavy lifter.">
        </head><body><main>
          <h1>Clashes in the Philippines</h1>
          <div class="articleSpanImage">
            <span itemprop="associatedMedia" itemscope
              itemtype="http://schema.org/ImageObject">
              <img itemprop="url"
                src="https://static01.nyt.com/cartoon-large.jpg"
                width="600" height="431">
            </span>
          </div>
          <div class="articleBody"><nyt_text>
            <p><!--shortarticle--></p>
            <p itemprop="articleBody">For the Philippines, America is the
            heavy lifter.</p>
          </nyt_text></div>
        </main></body></html>
        """,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/2013/09/22/opinion/global/"
            "clashes-in-the-philippines.html"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "complete"
    assert result.images
    assert result.extraction.parser_version == "nyt-parser/0.8.54"


def test_wsj_parser_preserves_legacy_video_description():
    result = parse_article(
        b"""
        <html><head><title>Economic Growth Video</title>
          <meta name="description" content="The Treasury secretary explains
          why tax reform alone is not enough to produce economic growth.">
        </head><body><h1>Economic Growth Video</h1>
          <script>var AT_VARS={articleType:'Video - WSJ',
            publicationDate:'2011-11-16'};</script>
          <div id="masterVideoCenter"><div class="js_videoPlayer">
            <div id="videoPlayer"></div>
          </div></div>
        </body></html>
        """,
        publisher="wsj",
        canonical_url="https://www.wsj.com/article/video-id.html",
    )

    assert result.content_type.value == "video"
    assert result.quality.status.value == "complete"
    assert result.published_at is not None
    assert result.blocks[-1].type.value == "embed"


def test_wsj_parser_removes_standalone_preview_ellipsis():
    result = parse_article(
        b"""
        <html><head><title>P&amp;G Earnings: What to Watch</title>
          <meta property="article:published_time"
                content="2017-10-19T14:48:00Z">
        </head><body><article>
          <p>Procter &amp; Gamble is scheduled to report earnings Friday.
          Here is what investors need to know about the quarter.</p>
          <p>Analysts expect the company to report core earnings and
          improved organic sales compared with a year earlier.</p>
          <p>...</p>
        </article></body></html>
        """,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "p-g-earnings-what-to-watch-1508424534"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "..." not in result.plain_text


def test_wsj_parser_removes_short_dangling_preview_fragment():
    result = parse_article(
        b"""
        <html><head><title>Retail Banking History</title></head>
        <body><article>
          <p>The retail banking industry is undergoing another major shift,
          as large banks introduce technology and modern offices.</p>
          <p>This video explains the history of retail banking over the...</p>
        </article></body></html>
        """,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "a-brief-history-of-retail-banking-1505758481"
        ),
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings
    assert "This video explains" not in result.plain_text
    assert "industry is undergoing" in result.plain_text


def test_wsj_parser_recovers_legacy_video_headline_from_at_vars():
    result = parse_article(
        b"""
        <html><head>
          <title>Humor: My Nike Ad (With Apologies to Tiger) - WSJ.com</title>
          <meta name="description" content="A short archived video
          description.">
        </head><body>
          <script>var AT_VARS={
            articleHeadline:'Humor: My Nike Ad (With Apologies to Tiger)',
            clickTitle:'WSJ.com - Humor: My Nike Ad',
            articleType:'Video - WSJ',
            publicationDate:'2010-04-09'};</script>
          <div id="masterVideoCenter"><div id="videoPlayer"></div></div>
        </body></html>
        """,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/article/"
            "83852A75-620A-47C7-9B3C-045FAE5960AB.html"
        ),
    )

    assert result.headline == (
        "Humor: My Nike Ad (With Apologies to Tiger)"
    )
    assert result.content_type.value == "video"
    assert result.quality.status.value == "complete"
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_wsj_parser_preserves_legacy_video_transcript():
    result = parse_article(
        b"""
        <html><head><title>Documentary Video</title></head><body>
          <h1>Documentary Video</h1>
          <script>var AT_VARS={articleType:'Video - WSJ',
            publicationDate:'2012-12-22'};</script>
          <div class="vcrPlayerArea"><div id="videoPlayer"></div></div>
          <div id="videoPlayerDescription"><div id="currentVideoInfo">
            <p><span itemprop="description">A documentary description with
            background on the subject and the reporting.</span></p>
          </div></div>
          <div class="vcrTranscript"><div class="vcrTranscriptContent">
            This is the complete archived transcript. It contains the
            narration, interviews, evidence and conclusions presented in
            the documentary, preserving content that was otherwise hidden
            behind the retired video player.
          </div></div>
        </body></html>
        """,
        publisher="wsj",
        canonical_url="https://www.wsj.com/article/documentary-id.html",
    )

    assert result.content_type.value == "video"
    assert result.quality.status.value == "complete"
    assert "complete archived transcript" in result.plain_text


def test_reuters_parser_trims_legal_read_more_recirculation():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Court rejects damages award">
          <meta property="article:published_time"
            content="2023-07-18T12:00:00Z">
        </head><body><article>
          <div data-testid="ArticleBody">
            <p>The court rejected the damages award after finding that the
            evidence presented at trial was insufficient to support it.</p>
            <p>The underlying liability verdict remains in place while the
            parties consider further appeals and related proceedings.</p>
            <p>For the plaintiff: Counsel at Example LLP</p>
            <p>Read more:</p>
            <p><a href="/legal/related-one">Earlier related court ruling</a></p>
            <p>Another related lawsuit involving the same companies</p>
            <p>Background coverage of the original complaint</p>
          </div>
        </article></body></html>
        """,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/legal/litigation/"
            "court-rejects-damages-award-2023-07-18"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "For the plaintiff" in result.plain_text
    assert "Read more:" not in result.plain_text
    assert "Earlier related court ruling" not in result.plain_text


def test_reuters_parser_strips_licensed_wire_copyright_footers():
    market_wire = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Company announces expansion">
          <meta property="article:published_time"
            content="2010-04-29T12:00:00Z">
        </head><body><article><div class="article-body">
          <p>The company announced a substantial international expansion
          after reporting stronger demand from customers in several markets.
          Management said the new offices would support sales and service
          teams and create additional jobs over the coming year.</p>
          <p>Media Contact Jane Example (415) 555-0100
          Copyright 2010, Market Wire, All rights reserved. -0-</p>
          <p>Additional licensed reporting remains part of the article.
          Copyright 2010. Example Corporation. All rights reserved.
          Example Corporation Media Relations media@example.com</p>
          <button class="SocialTools__facebook">Share</button>
          <button aria-label="image"><img
            src="https://s4.reutersmedia.net/example.jpg"></button>
        </div></article></body></html>
        """,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/article/"
            "idUS249732+29-Apr-2010+MW20100429"
        ),
    )
    business_wire = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Technology service launches">
          <meta property="article:published_time"
            content="2010-05-19T12:00:00Z">
        </head><body><article><div class="article-body">
          <p>A technology provider launched a new service for financial
          institutions, with automated processing and reporting tools for
          customers operating across international markets.</p>
          <p>Investor Contact John Example +44 20 7551 3224
          Copyright 2010, Business Wire. All rights reserved. -0-</p>
        </div></article></body></html>
        """,
        publisher="reuters",
        canonical_url=(
            "https://www.reuters.com/article/"
            "idUS47641+19-May-2010+BW20100519"
        ),
    )

    assert "Market Wire" not in market_wire.plain_text
    assert "Media Contact Jane Example" in market_wire.plain_text
    assert "Example Corporation Media Relations" not in market_wire.plain_text
    assert "SocialTools" not in market_wire.body_html
    assert "<button" not in market_wire.body_html
    assert "example.jpg" in market_wire.body_html
    assert "Business Wire" not in business_wire.plain_text
    assert "Investor Contact John Example" in business_wire.plain_text
    assert (
        market_wire.extraction.parser_version
        == "reuters-parser/0.7.25"
    )


def test_ft_parser_removes_share_recommendation_and_follow_topic_chrome():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="A complete FT report">
          <meta property="article:published_time"
            content="2020-04-20T12:00:00Z">
        </head><body><article>
          <div class="article__content-body">
            <ul data-toolbar="share">
              <li>Share on Twitter (opens new window)</li>
              <li>Share on Facebook (opens new window)</li>
            </ul>
            <div class="ftlabsaudioplayerholder">
              <h2>Listen to this article</h2>
              <button class="control control__play">
                Play audio for this article
              </button>
              <div class="js-feedback feedback">
                <p>Report a mispronounced word</p>
                <button class="js-feedback__responder">Submit</button>
              </div>
            </div>
            <button class="component-share__button">
              Share this graphic
            </button>
            <button class="n-myft-ui__button"
                    data-trackable="save-for-later">
              Save to myFT
            </button>
            <p class="article-info__byline">Example Reporter</p>
            <p>The opening paragraph contains substantial reporting about
            the policy decision and explains why it matters to readers.</p>
            <p>RECOMMENDED * An unrelated story promoted in the middle</p>
            <p>The second paragraph must remain because the recommendation
            module was inserted between genuine article paragraphs.</p>
            <p>Additional reporting by Another Reporter</p>
            <p class="instant-alert-cta__text">Get alerts on Markets when a
            new story is published</p>
            <h2 class="h2-promoted-content">Promoted Content</h2>
            <h2 class="concept-list__title">Follow the topics in this article</h2>
            <ul><li>Markets</li><li>World</li></ul>
          </div>
        </article></body></html>
        """,
        publisher="ft",
        canonical_url="https://www.ft.com/content/complete-report",
    )

    assert result.quality.status.value == "complete"
    assert "opening paragraph" in result.plain_text
    assert "second paragraph must remain" in result.plain_text
    assert "RECOMMENDED" not in result.plain_text
    assert "Listen to this article" not in result.plain_text
    assert "Report a mispronounced word" not in result.plain_text
    assert "Share this graphic" not in result.plain_text
    assert "Save to myFT" not in result.plain_text
    assert "<button" not in result.body_html
    assert "Promoted Content" not in result.plain_text
    assert "Follow the topics" not in result.plain_text


def test_ft_parser_recovers_legacy_flash_interactive():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title"
            content="China's railway development - FT.com">
          <meta property="article:published_time"
            content="2010-09-22T12:46:33Z">
          <meta name="description" content="This interactive graphic
            explores China's ambitious rail expansion plans.">
        </head><body><article><div id="storyContent">
          <p>This interactive graphic explores China's ambitious rail
          expansion plans, compared with its existing network.</p>
          <div class="insideArticleShare">
            <div class="story-package" data-track-comp-name="moreOn">
              <h3>More</h3><h4>IN Rail</h4>
              <ul><li>An unrelated rail story</li></ul>
            </div>
          </div>
          <p>Use the slider to superimpose its proposed new network,
          shown in red.</p>
          <div class="flashcomponent">
            <div class="flasherrors hidden">
              <h2>Content description</h2>
              <p class="flashdescription">China's railways</p>
              <div class="needflash">
                <p>This content requires an Adobe Flash plugin for your
                browser.</p>
                <p>Your plugin is either missing or out of date. Please
                install the latest plugin by clicking below.</p>
                <img alt="Install Flash Player"
                  src="https://im.ft-static.com/m/img/logo/get_flash.png">
              </div>
            </div>
            <div class="flashcontent hidden">
              <a class="flashlink popuplink"
                href="https://media.ft.com/cms/railways.swf?width=825">
                China's railways
              </a>
            </div>
          </div>
        </div></article></body></html>
        """,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "a09641c2-3117-11df-8e6f-00144feabdc0"
        ),
    )

    assert result.headline == "China's railway development"
    assert result.content_type.value == "interactive"
    assert result.quality.status.value == "complete"
    assert result.blocks[-1].type.value == "embed"
    assert result.blocks[-1].embed_url.startswith(
        "https://media.ft.com/cms/railways.swf"
    )
    assert "An unrelated rail story" not in result.plain_text
    assert "Adobe Flash plugin" not in result.plain_text
    assert all(
        "get_flash.png" not in image.original_url
        for image in result.images
    )
    assert result.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_marks_migrated_caption_without_visual_partial():
    result = parse_article(
        b"""
        <html><head>
          <title>Awkward greetings at Apec</title>
        </head><body><article class="article">
          <header class="article-header">
            <h2 class="primary-theme">World</h2>
            <h1 itemprop="headline">Awkward greetings at Apec</h1>
            <time itemprop="datePublished"
              datetime="2014-11-10T11:30:15Z"></time>
          </header>
          <div class="article-body" itemprop="articleBody">
            <p>Japan's Prime Minister Shinzo Abe (L) shakes hands with
            China's President Xi Jinping (R), during their meeting at the
            Great Hall of the People on the sidelines of Apec.</p>
            <p class="article__copyright-notice">
              Copyright The Financial Times Limited.
            </p>
          </div>
          <section class="more-ons">
            <amp-img src="https://www.ft.com/unrelated-story.jpg"></amp-img>
          </section>
        </article></body></html>
        """,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "012b92b0-76fe-3865-9584-5f3522517eba"
        ),
    )

    assert result.content_type.value == "gallery"
    assert result.quality.status.value == "partial"
    assert "incomplete-gallery" in result.quality.warnings
    assert result.plain_text.startswith("Japan's Prime Minister")
    assert "World" not in result.plain_text
    assert result.images == []
    assert result.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_removes_fashion_and_podcast_subscription_tails():
    fashion = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="A fashion business profile">
          <meta property="article:published_time"
            content="2019-12-13T05:00:42Z">
        </head><body><article><div class="article-body"
          itemprop="articleBody">
          <p>The profile explains how the founder built the company,
          expanded its product range and attracted a global audience.</p>
          <p>A second paragraph provides substantial reporting about the
          strategy, finances and competitive market facing the brand.</p>
          <p><em>Follow @financialtimesfashion on Instagram to find out
          about our latest stories first. Listen and subscribe to Culture
          Call at ft.com/culture-call or on Apple Podcasts</em></p>
        </div></article></body></html>
        """,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "f98447aa-1b6f-11ea-9186-7348c2f183af"
        ),
    )
    podcast = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="A Working It episode">
          <meta property="article:published_time"
            content="2022-06-01T12:00:00Z">
        </head><body><article>
          <div class="article__content-body">
            <audio data-audio-subtype="podcast">
              <source src="https://example.com/episode.mp3"
                type="audio/mpeg">
            </audio>
            <p>The episode examines class and inclusion at work through
            interviews with employees, executives and specialist reporters.</p>
            <p>Want more?</p>
            <p>A useful employer toolkit on social mobility.</p>
            <p>FT subscriber? Sign up for the weekly Working It newsletter.
            We cover all things workplace and management.</p>
            <p>What's coming next. One-click sign-up at
            www.ft.com/newsletters</p>
            <p>Subscribe to Working It wherever you get your podcasts.</p>
            <p>See acast.com/privacy for privacy and opt-out information.</p>
          </div>
        </article></body></html>
        """,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "1f5aa82d-d25b-48d9-b71e-661ad539c8b2"
        ),
    )

    assert fashion.quality.status.value == "complete"
    assert "@financialtimesfashion" not in fashion.plain_text
    assert "strategy, finances" in fashion.plain_text
    assert podcast.content_type.value == "audio"
    assert podcast.quality.status.value == "complete"
    assert "A useful employer toolkit" in podcast.plain_text
    assert "FT subscriber?" not in podcast.plain_text
    assert "acast.com/privacy" not in podcast.plain_text
    assert podcast.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_removes_newsletter_cards_and_scoreboard_signup():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Markets and sport">
          <meta property="article:published_time"
            content="2020-06-01T12:00:00Z">
        </head><body><article><div class="article-body"
          itemprop="articleBody">
          <p>Markets moved sharply as investors assessed the economic
          outlook and company results across several major sectors.</p>
          <experimental data-layout-name="card" data-layout-width="fullWidth">
            <h2>Coronavirus business update</h2>
            <p>Stay briefed with our
              <a href="https://ep.ft.com/pages/newsletters/example/subscribe/">
                coronavirus newsletter
              </a>
            </p>
          </experimental>
          <p>Reporting also examined the commercial outlook for sport
          and the changing value of international broadcast contracts.</p>
          <p><a href="https://ep.ft.com/newsletters/scoreboard/subscribe">
            <em>Sign up</em></a><em> to </em>
            <a href="https://www.ft.com/scoreboard"><em>Scoreboard</em></a>,
            <em>our new must-read weekly briefing on the business of sport.</em>
          </p>
        </div></article></body></html>
        """,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "caab44aa-5dcf-40f2-88be-4944a933ecda"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Markets moved sharply" in result.plain_text
    assert "commercial outlook for sport" in result.plain_text
    assert "Coronavirus business update" not in result.plain_text
    assert "Stay briefed with our" not in result.plain_text
    assert "Sign up to Scoreboard" not in result.plain_text
    assert result.extraction.parser_version == "ft-parser/0.8.29"


def test_ft_parser_handles_image_proxy_with_nested_fragment_url():
    proxy = (
        "https://www.ft.com/__origami/service/image/v2/images/raw/"
        "https%3A%2F%2Fci5.googleusercontent.com%2Fproxy%2Fabc"
        "%23https%3A%2F%2Fwww.ft.com%2F__origami%2Fservice%2Fimage"
        "%2Fv2%2Fimages%2Fraw%2Fhttps%253A%252F%252Fexample.com"
        "%252Fnewsletter.jpg%3Fsource%3Dtest?source=test"
    )
    html = f"""
      <html><head>
        <meta property="og:title" content="A Financial Times newsletter">
      </head><body><article><div class="article-body"
        itemprop="articleBody">
        <p>The newsletter contains a complete report on European markets,
        companies and economic policy for readers around the world.</p>
        <figure><img src="{proxy}" alt="Market illustration"></figure>
        <p>Analysts discussed the outlook and the risks facing investors
        during the remainder of the year.</p>
      </div></article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "2d079096-5ddf-11e8-9334-2218e7146b04"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.images
    assert result.extraction.parser_version == "ft-parser/0.8.29"


def test_wsj_parser_removes_buy_side_recommendation_widget():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="A corporate report">
          <meta property="article:published_time"
            content="2024-06-01T12:00:00Z">
        </head><body><article>
          <p>The company filed for bankruptcy after freight demand
          weakened and financing costs increased across the industry.</p>
          <div class="clearfix byline-wrap">
            <div class="author-info">
              <a class="author icon bio">Biography</a>
              <a class="author icon email">reporter@wsj.com</a>
            </div>
          </div>
          <p>Executives said customers would continue to receive service
          while the business completes an orderly restructuring. Court
              filings described the company's assets, liabilities, lenders and
              plans for maintaining essential operations during the process.
              Industry analysts said the case reflected a broader slowdown in
              shipping volumes after several years of rapid expansion, while
              creditors prepared to review the proposed financing package.</p>
          <button class="author-button">Show author details</button>
          <p>-For more WSJ Technology analysis, reviews, advice and
          headlines, sign up for our weekly newsletter.</p>
          <p><div>
            <a href="https://www.wsj.com/buyside/?mod=wsj_article_buy_widget">
              Buy Side from WSJ
            </a>
            <p>Expert recommendations on products and services,
            independent from The Wall Street Journal newsroom.</p>
            <a href="/buyside/personal-finance/taxes">Will Filing Taxes
            Jointly Save Money?</a>
          </div></p>
        </article></body></html>
        """,
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/articles/"
            "trucker-us-logistics-solutions-shuts-down-in-bankruptcy-285275d6"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "filed for bankruptcy" in result.plain_text
    assert "orderly restructuring" in result.plain_text
    assert "Buy Side from WSJ" not in result.plain_text
    assert "Will Filing Taxes" not in result.plain_text
    assert "weekly newsletter" not in result.plain_text
    assert "Biography" not in result.plain_text
    assert "reporter@wsj.com" not in result.plain_text
    assert "<button" not in result.body_html
    assert result.extraction.parser_version == "wsj-parser/0.8.45"


def test_ap_parser_removes_legacy_terminal_period_paragraph():
    result = parse_article(
        """
        <html><head>
          <meta property="og:title" content="A court report">
          <meta property="article:published_time"
            content="2012-02-23T12:00:00Z">
        </head><body><article>
          <div class="RichTextStoryBody RichTextBody">
            <p>Lawyers presented opening arguments in federal court and
            questioned witnesses about the evidence in the case.</p>
            <p>The defendant denied the allegations while prosecutors
            described documents and testimony they planned to introduce.
            The trial was expected to continue for several weeks, with
            additional witnesses scheduled to appear before the jury.</p>
            <p>_</p>
            <p>______</p>
            <p>——————————</p>
            <p>&lt;</p>
            <p>.</p>
            <table><tr><td></td><td>—————</td></tr></table>
          </div>
        </article></body></html>
        """.encode(),
        publisher="ap",
        canonical_url=(
            "https://apnews.com/article/"
            "africa-business-genocides-rwanda-test"
        ),
    )

    assert result.quality.status.value == "complete"
    assert result.blocks[-1].text != "."
    assert not any(
        len(block.text) >= 2 and set(block.text) == {"_"}
        for block in result.blocks
    )
    assert result.plain_text.rstrip().endswith("jury.")
    assert not any(
        block.text in {"_", "——————————", "<"}
        for block in result.blocks
    )
    assert result.extraction.parser_version == "ap-parser/0.6.17"


def test_ap_parser_deduplicates_dims_variants_by_underlying_asset():
    underlying = (
        "https%3A%2F%2Fstorage.googleapis.com%2Fafs-prod%2Fmedia%2F"
        "c9878444c74a4f1a8d76031a46775c2a%2F3000.jpeg"
    )
    lead = (
        "https://dims.apnews.com/dims4/default/one/2147483647/"
        f"resize/980x653!/quality/90/?url={underlying}"
    )
    body = (
        "https://dims.apnews.com/dims4/default/two/2147483647/"
        f"resize/599x399!/quality/90/?url={underlying}"
    )
    html = f"""
      <html><head>
        <meta property="og:title" content="AP gallery report">
        <meta property="og:image" content="{lead}">
      </head><body><article>
        <p>This AP report contains enough substantive text to parse.</p>
        <figure><img src="{body}" alt="A useful image caption"></figure>
      </article></body></html>
    """.encode()

    result = parse_article(
        html,
        publisher="ap",
        canonical_url="https://apnews.com/article/example-gallery",
    )

    matching = [
        image
        for image in result.images
        if "c9878444c74a4f1a8d76031a46775c2a" in image.original_url
    ]
    assert len(matching) == 1
    assert body in matching[0].candidate_urls


def test_bloomberg_parser_removes_legacy_related_stories_list():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="A company acquisition">
          <meta property="article:published_time"
            content="2017-06-11T12:00:00Z">
        </head><body><article><div class="body-copy">
          <p>The investor said the amended acquisition offer remained
          unfair and asked the buyer to improve the proposed price.</p>
          <p>The company defended its recommendation after reviewing the
          expected synergies, financing terms and alternative proposals.
          Shareholders were scheduled to vote after receiving additional
          information about the transaction and its valuation.</p>
          <aside class="content-accessories">
            <div class="text-to-speech">
              <h2>LISTEN TO ARTICLE</h2>
              <button aria-label="Listen to article"></button>
              <audio><source src="https://assets.bwbx.io/read.mp3"></audio>
            </div>
            <div class="brokerboxarticle page-ad"></div>
          </aside>
          <p>Related stories:</p>
          <ul>
            <li><a href="/news/related-one">Hedge Fund Pushes for a
            Better Deal</a></li>
            <li><a href="/news/related-two">Company Amends Offer</a></li>
          </ul>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2017-06-11/"
            "oasis-says-amended-offer-is-still-unfair"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "amended acquisition offer" in result.plain_text
    assert "Related stories" not in result.plain_text
    assert "Hedge Fund Pushes" not in result.plain_text
    assert "LISTEN TO ARTICLE" not in result.plain_text
    assert "<button" not in result.body_html
    assert "read.mp3" not in result.body_html
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_removes_legacy_contact_social_and_partner_footers():
    result = parse_article(
        """
        <html><head>
          <meta property="og:title" content="China adjusts energy targets">
          <meta property="article:published_time"
            content="2013-03-05T12:00:00Z">
          <meta property="og:image" content="null">
        </head><body><article><div class="body-copy">
          <p>Officials increased the national energy-efficiency target
          after reviewing industrial output and pollution data.</p>
          <h2>&#x2018;</h2>
          <p>The revised plan gives provincial authorities additional
          benchmarks and requires companies to report their progress.</p>
          <p>The currency recovered after an unusually volatile session.
          <em>Want more personal finance news? Sign up for our weekly personal
          finance newsletter,
          <a href="https://login.bloomberg.com/register">
          Wealth Watch.</a></em></p>
          <h2>Fed Officials Cut Forecast for End-2015 Fed Funds Rate</h2>
          <h2>Fed Says Unemployment Can Fall More Before Inflation a Risk</h2>
          <p>To contact Bloomberg News staff for this story:
          Penny Peng in Beijing at ppeng18@bloomberg.net</p>
          <p>To contact the writer of this column:
          James Russell at jrussell@example.com</p>
          <p>To contact the writer of this review:
          Linda Yablonsky in New York at fabyab@earthlink.net</p>
          <p>To contact the editor responsible for this column:
          Manuela Hoelterhoff at editor@example.com</p>
          <p>To contact the author of this blog post:
          Chandrahas Choudhury at author@example.com</p>
          <p>To contact the editor responsible for this post:
          Kirsten Salyer at post-editor@example.com</p>
          <p>To contact the editor responsible for this slideshow:
          Maria Wood</p>
          <p>To see a slideshow of photos relating to the election go to
          {EXT2 &lt;GO&gt;} or click {1 &lt;GO&gt;}.</p>
          <p>To contact the writer on the story: Jeffrey Burke at
          writer@example.com</p>
          <p>To contact the editor responsible for the story:
          Manuela Hoelterhoff at legacy-editor@example.com</p>
          <p>To contact the editor responsible for this story
          Craig Stirling at editor-without-colon@example.com</p>
          <p>To contact the reporter responsible for his story:
          Ken Wells at reporter@example.com</p>
          <p>To contact the reporter for this story:
          Phoebe Sedgman at reporter-for@example.com</p>
          <p>To contact the writers on the story:
          Richard Vines at writers@example.com</p>
          <p>Click on &ldquo;Send Comment&rdquo; in the sidebar display to send a
          letter to the editor.</p>
          <p>Click on &ldquo;Send Comment&rdquo; in sidebar display to send a
          letter to the editor.</p>
          <p>To contact the Bloomberg News staff for this story:
          Michael Wei at staff@example.com</p>
          <p>Editors: Jonathan Neumann, Gail Roche</p>
          <p>-- Editors: Robert Jameson, Brendan Walsh</p>
          <p>-Editors: Chris Peterson, Peter Branton</p>
          <p>Editors:</p>
          <p>-- Zhang Shidong. Editor: Allen Wan, Linus Chua</p>
          <p>- Helen Sun. Editor: James Poole</p>
          <p>spearson3@bloomberg.net</p>
          <p>pathurtado@bloomberg.net; Bob Van Voris in federal court
          at rvanvoris@bloomberg.net</p>
          <p>-- With assistance from Willow Bay and Shivaune Field in
          Los Angeles. Editors: Anne Reifenberg, Lisa Kassenaar.</p>
          <p>-With assistance from Anand Menon in Singapore and Jack
          Kaskey in Houston. Editors: Jake Lloyd-Smith, Jarrett Banks</p>
          <p>With assistance from Toru Fujioka and Aki Ito in Tokyo.
          -- Editors: Steve Dickson, William Ahearn</p>
          <p>With assistance from Anuchit Nguyen and Supunnabul
          Suwannakij.</p>
          <p>— With assistance by Jun Luo, and Steven Yang</p>
          <p>(With assistance from Lili Rosboch . Zinta Lundborg is an
          editor for Muse, the arts and leisure section of Bloomberg News.
          The opinions expressed are her own.)</p>
          <p>Read more opinion online from Bloomberg View.</p>
          <p>Read more online opinion from Bloomberg View.</p>
          <p>Read more online from Bloomberg View.</p>
          <p>Read more opinion online from Bloomberg View .</p>
          <p>Today’s highlights: the View editors on Greece’s political
          deadlock and saving the Volcker rule.</p>
          <p>Also, the editors on battling terrorism in the Egyptian Sinai;
          Caroline Baum on the election; Margaret Carlson on Congress.</p>
          <p>Read more opinion online from Bloomberg View. Subscribe to
          receive a daily e-mail highlighting new View columns, editorials
          and op-ed articles.</p>
          <p>Read more opinion online from Bloomberg View . Subscribe to
          receive a daily e-mail highlighting new View editorials, columns
          and op-ed articles.</p>
          <p>For more quick commentary from Bloomberg View,
          go to The Ticker.</p>
          <p>Read more breaking commentary from Bloomberg View at the
          Ticker.</p>
          <p>Read more breaking commentary from Bloomberg View at the
          Ticker .</p>
          <p>Read more breaking commentary from Bloomberg View columnists
          and editors at the Ticker .</p>
          <p>Read more breaking commentary from Josh Barro and other
          Bloomberg View columnists and editors at the Ticker .</p>
          <p>Follow @tsrandall on Twitter for more on what's cool</p>
          <p>Read more Bloomberg View editorials .</p>
          <p>Read more Bloomberg View op-eds .</p>
          <p>Muse highlights include Ryan Sutton on dining and Katya
          Kazakina on art.</p>
          <p>Today’s Muse highlights include Jeremy Gerard on theater
          and John Mariani on wine.</p>
          <p>Muse highlights include: Farah Nayeri on film at Cannes,
          Guy Collins on wine and James Russell on architecture.</p>
          <p>Related News and Information:</p>
          <p>The case is Example v. Company, 13-bk-53846,
          <a href="/court">U.S. Bankruptcy Court</a>, Eastern District.
          For Related News and Information:</p>
          <p>For more trademark news, click here.</p>
          <p>For trademark news, click here.</p>
          <p>For the latest verdict and settlement news, click here.</p>
          <p>For the latest new suits news, click here. For copies of
          recent civil complaints, click here. For the latest lawsuits
          news, click here.</p>
          <p>For the latest litigation department news, click here.</p>
          <p>For the latest trial and appeals news, click here.</p>
          <p>This is a Bloomberg podcast. To download, watch or listen
          to this report now, click here.</p>
          <p>This is a Bloomberg podcast. To download, watch or listen
          now, click here.</p>
          <p>(To listen to the podcast, click here .)</p>
          <h2>For more, read this next:</h2>
          <ul><li><a href="/quicktake/apple">QuickTake: Apple</a></li></ul>
          <p>For more, read this next: Bankers Flock to India Festival
          for Drag Races and $79,000 Bikes</p>
          <p>For a much better rundown of tonight's event, read this next:</p>
          <ul><li>Tesla Related Story</li></ul>
          <p>For more, click here.</p>
          <p>For more, click here, and click here.</p>
          <p>For more, click here and here.</p>
          <ul><li>NOTE: Vincent Cignarella is an FX strategist who writes
          for Bloomberg. The observations he makes are his own. To subscribe
          to Inside Canada, click here, hit “Display &amp; Edit” and then
          “Set Alert Delivery”</li></ul>
          <p>For the video, click here, and for more, click here.</p>
          <p>For the video, click here.</p>
          <p>For the audio, click here.</p>
          <p>To read more from Echoes, Bloomberg View's economic history
          blog, click here .</p>
          <p>Read more Echoes columns online .</p>
          <p>Read more from Echoes online .</p>
          <p>Read more Bloomberg View columns .</p>
          <p>Read more Bloomberg sustainability news and follow us on
          Twitter.</p>
          <p>Read more in our full story</p>
          <p>Read more from Echoes, Bloomberg View's economic history
          blog.</p>
          <p>Read more from Echoes , Bloomberg View's economic history
          blog.</p>
          <p><em>This story appears in the September issue of </em>
          <a href="/markets-magazine">Bloomberg Markets</a>
          <em> magazine. With assistance from Debjit Chakraborty.</em></p>
          <p>(Josh Barro is lead writer for the Ticker. E-mail him and)</p>
          <p>For more, read this QuickTake:
          <a href="https://www.bloomberg.com/quicktake/currency-pegs">
          Currency Pegs</a></p>
          <p>NSN MJ557B6S973D &lt;GO&gt;
          Mexico Agency Rejects Telecom Accord From Phone Regulator</p>
          <p>FIFW NSN NCVIJV6JTSF5 &lt;GO&gt;
          Sanofi’s Viehbacher Sees No Reason to Join Pharma M&amp;A Frenzy</p>
          <p class="article-audio-attachment">Download:
          <a class="article-audio-attachment__link" href=""></a></p>
          <p>To analyze this 13F:
          {89754 1 &lt;client&gt; PORT&lt;go&gt;}
          {FLNG 89754 1 14&lt;go&gt;}
          To analyze all 13F's filed, {FLNG&lt;go&gt;}</p>
          <p>Emerging-markets market view: {EMMV &lt;go&gt;}</p>
          <p>Following is a list of the key events facing Greece before the
          end of the year. For full details on Greece’s funding commitments,
          click here. See EXT4 for more on the European debt crisis.</p>
          <p>相關新聞和信息： 彭博率先報道滾動屏: FIRST &lt;GO&gt;
          中文彭博率先報道: NI BFWCH BBG &lt;GO&gt;</p>
          <p>관련 기사 및 정보 보기:
          First Word 스크롤 화면: FIRST&lt;GO&gt;
          First Word 기사 나열: NH BFW&lt;GO&gt;</p>
          <p>원본 기사:
          Bank of Japan Sovereign Bond Paper Profit Tops Zuckerberg Wealth</p>
          <p>--취재보조: Daisuke Sakai(Tokyo), Masaki Kondo(Singapore).</p>
          <p>본 기사의 번역자: 조은경(서울), echo8@bloomberg.net
          원본 기사의 기자: Wes Goodman(Singapore)
          원본 기사의 편집책임자: Garfield Reynolds</p>
          <p>* Link to earlier story: China Lets Local Governments Swap</p>
          <p>Debt Into Municipal Bonds NSN NKXKOY6JTSE9&lt;GO&gt;</p>
          <p>Link to Company News: {DIST SL &lt;Equity&gt; CN}</p>
          <p>Link to Company News:{3218437Z US &lt;Equity&gt; CN &lt;GO&gt;}
          Link to Company News:{FMCC US &lt;Equity&gt; CN &lt;GO&gt;}
          Link to Company News:{FNMA US &lt;Equity&gt; CN &lt;GO&gt;}</p>
          <p>Link to Company News:
          {ALLY1 US &lt;Equity&gt; CN &lt;GO&gt;}
          {JPM US &lt;Equity&gt; CN &lt;GO&gt;}
          {BAC US &lt;Equity&gt; CN &lt;GO&gt;}</p>
          <p>Link to statement: {http://tinyurl.com/lg8rjcj}
          Link to Company News:{8243133Z US &lt;Equity&gt; CN &lt;GO&gt;}</p>
          <p>Link to Statement:{NSN NETO0G3MMTC1 &lt;GO&gt;}</p>
          <p>***END OF TRANSCRIPT***</p>
          <p>Running Time: 01:58</p>
          <p>Running time 04:00</p>
          <p>Terminal Users: Click here to play now.</p>
          <p>Provider ID: 671dc54518c848b7b92dcfc35e635b3a</p>
          <p>Contributed via: Bloomberg Publisher WEB Service</p>
          <p>Generated by Bloomberg Publisher WEB Service</p>
          <p>UBI 3 MONTHS 9.36 DCAP * T Contributed via:
          Bloomberg Publisher WEB Service Provider ID:
          6bfbad8794b347b7b671776908e79658</p>
          <pre>CD TABLE ROW 99.75 9.25
          Contributed via: Bloomberg Publisher WEB Service
          Provider ID: c5e93d3ce68346de8e2cccf1b1fb5fc3</pre>
          <p>Carlo Piovano in London and Pamela Sampson in Bangkok
          contributed to this report.</p>
          <p>AP Technology Writer Andrew Vanacore contributed to this story
          from New York.</p>
          <p>To watch the video, click here.</p>
          <p>WebRep currentVote noRating noWeight</p>
          <p>-- Tracey White in New York (+1)212-617-4312</p>
          <p>Siehe dazu auch:
          Fortlaufende Kurzmeldungen: FIRST&lt;GO&gt;
          First Word Überschriften: NH BFW&lt;GO&gt;</p>
          <p>Überschrift des Artikels im Original:
          Gross’s Terminal Keyboard to Be Displayed at Smithsonian (1)</p>
          <p>A fifth embassy transgression would make that case hard to
          swallow. Read more opinion online from Bloomberg View.
          Subscribe to receive a daily e-mail highlighting new View
          editorials, columns and op-ed articles.</p>
          <p>-- Bloomberg Radio +1-212-617-5560</p>
          <p>To contact the lead author of this column:
          Michael Crow at michael.crow@asu.edu</p>
          <p>Erika Riggs, a real estate writer for Zillow Blog,
          covers unusual properties. Read more of her work here.</p>
          <p>-- Alex Tanzi</p>
          <p>Major League Baseball’s championship series begins.
          Teams and times to be determined. Click here for playoff
          schedule.</p>
          <p>To see the patent, click: 5,969,156.</p>
          <p>To see the patent: 7,139,761.</p>
          <p>“The Big Short” is published by Norton (266 pages, $27.95).
          To buy this book in North America , click here .</p>
          <p>“Earth, Inc.” is published by Harvard Business Press
          (189 pages, $24.95). To order in North America, click here.</p>
          <p>To buy this book in North America, click here.</p>
          <p>To buy this book in North America , click here .</p>
          <p>To read the publisher’s Web page on the book,
          http://books.example.com/the-big-short/</p>
          <p>(P&amp;G executives will hold a conference call at 8:30 a.m.
          New York time to discuss results. To listen, go to LIVE
          &lt;GO&gt;.)</p>
          <p>(Target will hold a conference call on the results at 10:30 a.m.
          New York time. To listen, visit TGT US &lt;EQUITY&gt; EVT
          &lt;GO&gt;.)</p>
          <p>(Corrects dollar amount of Solyndra loss to taxpayers in ninth
          paragraph. For more Bloomberg View, click on VIEW
          &lt;GO&gt;.)</p>
          <p>(Televisa is scheduled to discuss results on a conference call
          at 10 a.m. New York time. For details, click here.)</p>
          <p>• • • • •</p>
          <p>source</p>
          <p>http://www.businessweek.com/articles/2014-01-09/example</p>
          <div data-role="memberSignature" class="ipsEntry__signature">
          <p>ThaiVisa, it's also in French</p>
          </div>
          <pre>Top 10 Films Grosses
          <h2>Year-to-date Revenue</h2>
          2015 2014 YTD YTD Pct.</pre>
          <h2>statistics</h2>
          <p><em>Watch Charlie Rose on Bloomberg TV weeknights at
          7 p.m. and 10 p.m. ET.</em></p>
          <p>—With Isabella Cota</p>
          <p>-- Alex Kim in Hong Kong 852-2977-6507</p>
          <p><em>Richard Vines is the chief food critic for Bloomberg.
          Follow him on Twitter @richardvines.</em></p>
          <p>(William Pesek is a Bloomberg View columnist.
          Follow him on Twitter .)</p>
          <p>(Susan Crawford is the author of Captive Audience.
          Follow her on Twitter at @scrawford.)</p>
          <p><em>Visit </em>
          <a href="http://www.bloomberg.com/sustainability/">
          <em>www.bloomberg.com/sustainability</em></a>
          <em> for the latest from Bloomberg News about energy,
          natural resources and global business</em></p>
          <p><strong>More by Eric Roston (</strong>
          <a href="https://twitter.com/eroston">@eroston</a>
          <strong> on Twitter):</strong></p>
          <ul>
            <li><a href="/news/related-energy-one">
            A related energy story</a></li>
            <li><a href="/news/related-energy-two">
            Another related energy story</a></li>
          </ul>
          <p><em>Visit </em>
          <a href="http://www.bloomberg.com/sustainability/the-grid">
          The Grid</a>
          <em> for the latest about energy, natural resources and
          global business.</em></p>
          <p>(To save a copy of the chart, click here.)</p>
          <p>*T *T</p>
          <p>##</p>
          <p>___</p>
          <p>Join the discussion on the Bloomberg Businessweek Business
          School Forum, visit us on Facebook, and follow @BWbschools on
          Twitter.</p>
          <section class="comments">
            <p>Bloomberg reserves the right to edit or remove comments.</p>
            <p>Please enable JavaScript to view the comments powered by
            Disqus.</p>
          </section>
          <ul id="story_tools_bottom">
            <li>Tweet</li><li>More</li><li>Business Exchange</li>
            <li>Buzz up!</li><li>Digg</li><li>Print</li><li>Email</li>
          </ul>
          <ul class="entry_sharing">
            <li>Facebook</li><li>Twitter</li><li>Google+</li>
            <li>LinkedIn</li>
          </ul>
          <h2>Read this next:</h2>
          <ul>
            <li><a href="/news/articles/2013-03-04/first-related">
            First related report</a></li>
            <li><a href="/news/articles/2013-03-04/second-related">
            Second related report</a></li>
          </ul>
          <h2>For more on the global economy, check out Benchmark:</h2>
          <ul>
            <li><a href="/news/articles/2013-03-04/benchmark-one">
            First Benchmark recommendation</a></li>
            <li><a href="/news/articles/2013-03-04/benchmark-two">
            Second Benchmark recommendation</a></li>
          </ul>
          <p><strong>Related</strong>:</p>
          <ul>
            <li><a href="https://example.com/related-craftsman">
            A related partner article</a></li>
          </ul>
          <p>Daily Podcast</p>
          <p>WaMu Evidence Issues, Auctions, Solo Cup Challenges: Audio</p>
          <p>Several bankruptcy cases are discussed in the bankruptcy
          podcast on the Bloomberg terminal. To listen, click here.</p>
          <p>More from Condé Nast Traveler:</p>
          <ul>
            <li><a href="/news/articles/2013-09-30/vineyards">
            Gorgeous Vineyards Around the World</a></li>
            <li><a href="/news/articles/2013-09-30/islands">
            Island Escapes</a></li>
          </ul>
          <p>-0- Jan/13/2011 00:35 GMT</p>
          <p>#&lt;535521.2245115.2.1.46.17993.25&gt;#
          -0- Jul/07/2010 14:03 GMT</p>
          <p>A legitimate fund classification sentence.
          {CACX 80671055 &lt;GO&gt;}</p>
          <p>* CNX Nifty unchanged at 8,491; Futures premium 43.8 rupees
          * For change in stock futures OI, see FMON &lt;GO&gt;</p>
          <p>Copyright notice in quoted source material remains relevant.</p>
          <p><a href="/news/articles/2013-03-04/related-energy-report">
          A related Bloomberg report</a></p>
          <p>&#169; 2026 Trend News Agency. All rights reserved.</p>
        </div></article></body></html>
        """.encode("utf-8"),
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-03-05/"
            "china-increases-energy-efficiency-targets"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "revised plan" in result.plain_text
    assert all(block.text != "‘" for block in result.blocks)
    assert "currency recovered" in result.plain_text
    assert "Want more personal finance news" not in result.plain_text
    assert "Fed Officials Cut Forecast" not in result.plain_text
    assert "Fed Says Unemployment" not in result.plain_text
    assert "Wealth Watch" not in result.plain_text
    assert "quoted source material remains relevant" in result.plain_text
    assert "To contact Bloomberg" not in result.plain_text
    assert "To see the patent:" not in result.plain_text
    assert "To buy this book" not in result.plain_text
    assert "To order in North America" not in result.plain_text
    assert "“Earth, Inc.” is published" in result.plain_text
    assert "CNX Nifty unchanged at 8,491" in result.plain_text
    assert "FMON" not in result.plain_text
    assert "UBI 3 MONTHS 9.36 DCAP" in result.plain_text
    assert "CD TABLE ROW 99.75 9.25" in result.plain_text
    assert "Provider ID:" not in result.plain_text
    assert "published by Norton" in result.plain_text
    assert "For details, click here" not in result.plain_text
    assert "Televisa is scheduled" in result.plain_text
    assert "• • •" not in result.plain_text
    assert "businessweek.com/articles" not in result.plain_text
    assert "ThaiVisa" not in result.plain_text
    assert result.plain_text.count("Year-to-date Revenue") == 1
    assert "\n\nstatistics" not in result.plain_text
    assert "publisher’s Web page" not in result.plain_text
    assert "To listen, go to LIVE" not in result.plain_text
    assert "To listen, visit TGT" not in result.plain_text
    assert "will hold a conference call" in result.plain_text
    assert "Target will hold a conference call" in result.plain_text
    assert "Corrects dollar amount of Solyndra loss" in result.plain_text
    assert "click on VIEW" not in result.plain_text
    assert "Watch Charlie Rose" not in result.plain_text
    assert "With Isabella Cota" not in result.plain_text
    assert "To contact the writer" not in result.plain_text
    assert "To contact the editor" not in result.plain_text
    assert "To contact the author" not in result.plain_text
    assert "To contact the editor responsible for this slideshow" not in (
        result.plain_text
    )
    assert "To see a slideshow of photos" not in result.plain_text
    assert "To contact the reporter" not in result.plain_text
    assert "To contact the writers" not in result.plain_text
    assert "Send Comment" not in result.plain_text
    assert "Bloomberg News staff" not in result.plain_text
    assert "Editors:" not in result.plain_text
    assert "Helen Sun" not in result.plain_text
    assert "spearson3@bloomberg.net" not in result.plain_text
    assert "pathurtado@bloomberg.net" not in result.plain_text
    assert "With assistance from Willow Bay" not in result.plain_text
    assert "With assistance from Anand Menon" not in result.plain_text
    assert "With assistance from Toru Fujioka" not in result.plain_text
    assert "With assistance from Anuchit Nguyen" not in result.plain_text
    assert "With assistance by Jun Luo" not in result.plain_text
    assert "With assistance from Lili Rosboch" not in result.plain_text
    assert "Zinta Lundborg is an editor for Muse" in result.plain_text
    assert "Read more opinion online" not in result.plain_text
    assert "Read more online from Bloomberg View" not in result.plain_text
    assert "Today’s highlights" not in result.plain_text
    assert "Also, the editors on" not in result.plain_text
    assert "daily e-mail highlighting" not in result.plain_text
    assert "latest verdict and settlement news" not in result.plain_text
    assert "latest new suits news" not in result.plain_text
    assert "latest lawsuits news" not in result.plain_text
    assert "latest litigation department news" not in result.plain_text
    assert "latest trial and appeals news" not in result.plain_text
    assert "This is a Bloomberg podcast" not in result.plain_text
    assert "listen to the podcast" not in result.plain_text
    assert "For more, read this next" not in result.plain_text
    assert "FIFW NSN" not in result.plain_text
    assert "Download:" not in result.plain_text
    assert "Bankers Flock to India Festival" not in result.plain_text
    assert "Tesla Related Story" not in result.plain_text
    assert "QuickTake: Apple" not in result.plain_text
    assert "For more, click here" not in result.plain_text
    assert "Vincent Cignarella is an FX strategist" in result.plain_text
    assert "Set Alert Delivery" not in result.plain_text
    assert "For the video, click here" not in result.plain_text
    assert "For the audio, click here" not in result.plain_text
    assert "read more from Echoes" not in result.plain_text
    assert "Read more Echoes columns" not in result.plain_text
    assert "Read more Bloomberg View columns" not in result.plain_text
    assert "economic history blog" not in result.plain_text
    assert "This story appears in the September issue" in result.plain_text
    assert "With assistance from Debjit Chakraborty" not in result.plain_text
    assert "(Josh Barro is lead writer for the Ticker.)" in result.plain_text
    assert "E-mail him and" not in result.plain_text
    assert "For more quick commentary" not in result.plain_text
    assert "Read more breaking commentary" not in result.plain_text
    assert "Follow @tsrandall" not in result.plain_text
    assert "Read more Bloomberg View editorials" not in result.plain_text
    assert "Read more Bloomberg View op-eds" not in result.plain_text
    assert "Muse highlights include" not in result.plain_text
    assert "Today’s Muse highlights" not in result.plain_text
    assert "Farah Nayeri on film" not in result.plain_text
    assert "Related News and Information:" not in result.plain_text
    assert "The case is Example v. Company" in result.plain_text
    assert "For more trademark news" not in result.plain_text
    assert "For trademark news" not in result.plain_text
    assert "read this QuickTake" not in result.plain_text
    assert "MJ557B6S973D" not in result.plain_text
    assert "To analyze this 13F" not in result.plain_text
    assert "Emerging-markets market view" not in result.plain_text
    assert "Following is a list of the key events facing Greece" in (
        result.plain_text
    )
    assert "funding commitments, click here" not in result.plain_text
    assert "See EXT4" not in result.plain_text
    assert "相關新聞和信息" not in result.plain_text
    assert "관련 기사 및 정보 보기" not in result.plain_text
    assert "원본 기사" not in result.plain_text
    assert "취재보조" not in result.plain_text
    assert "본 기사의 번역자" not in result.plain_text
    assert "Link to earlier story" not in result.plain_text
    assert "NKXKOY6JTSE9" not in result.plain_text
    assert "Link to Company News" not in result.plain_text
    assert "Link to statement" not in result.plain_text
    assert "NETO0G3MMTC1" not in result.plain_text
    assert "END OF TRANSCRIPT" not in result.plain_text
    assert "Running Time:" not in result.plain_text
    assert "Running time 04:00" not in result.plain_text
    assert "Terminal Users" not in result.plain_text
    assert "Provider ID:" not in result.plain_text
    assert "Contributed via:" not in result.plain_text
    assert "Generated by Bloomberg" not in result.plain_text
    assert "contributed to this report" not in result.plain_text
    assert "contributed to this story" not in result.plain_text
    assert "To watch the video" not in result.plain_text
    assert "WebRep currentVote" not in result.plain_text
    assert "Tracey White in New York" not in result.plain_text
    assert "Fortlaufende Kurzmeldungen" not in result.plain_text
    assert "Überschrift des Artikels im Original" not in result.plain_text
    assert "A fifth embassy transgression" in result.plain_text
    assert "Read more opinion online" not in result.plain_text
    assert "Bloomberg Radio +1" not in result.plain_text
    assert "Michael Crow at michael.crow" not in result.plain_text
    assert "Erika Riggs" in result.plain_text
    assert "Read more of her work" not in result.plain_text
    assert "Alex Tanzi" not in result.plain_text
    assert "Click here for playoff schedule" not in result.plain_text
    assert "championship series begins" in result.plain_text
    assert "To see the patent" not in result.plain_text
    assert "Alex Kim in Hong Kong" not in result.plain_text
    assert "Follow him on Twitter" not in result.plain_text
    assert "A related Bloomberg report" not in result.plain_text
    assert "bloomberg.com/sustainability" not in result.plain_text
    assert "Visit The Grid" not in result.plain_text
    assert "More by Eric Roston" not in result.plain_text
    assert "A related energy story" not in result.plain_text
    assert "Another related energy story" not in result.plain_text
    assert "To save a copy of the chart" not in result.plain_text
    assert "*T *T" not in result.plain_text
    assert all(not image.original_url.endswith("/null") for image in result.images)
    assert "##" not in result.plain_text
    assert "___" not in result.plain_text
    assert "Join the discussion" not in result.plain_text
    assert "powered by Disqus" not in result.plain_text
    assert "right to edit or remove comments" not in result.plain_text
    assert "Business Exchange" not in result.plain_text
    assert "Google+" not in result.plain_text
    assert "First related report" not in result.plain_text
    assert "Second related report" not in result.plain_text
    assert "First Benchmark recommendation" not in result.plain_text
    assert "A related partner article" not in result.plain_text
    assert "Daily Podcast" not in result.plain_text
    assert "WaMu Evidence Issues" not in result.plain_text
    assert "More from Condé Nast Traveler" not in result.plain_text
    assert "Gorgeous Vineyards Around the World" not in result.plain_text
    assert "Second Benchmark recommendation" not in result.plain_text
    assert "Jan/13/2011 00:35 GMT" not in result.plain_text
    assert "Jul/07/2010 14:03 GMT" not in result.plain_text
    assert "CACX 80671055" not in result.plain_text
    assert "legitimate fund classification sentence" in result.plain_text
    assert "Trend News Agency" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_trims_marketwire_release_distribution_tail():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title"
            content="Bombardier wins a transportation contract">
          <meta property="article:published_time"
            content="2010-07-23T12:00:00Z">
        </head><body><article><div id="story_content">
          <p>Bombardier won a contract to supply public transportation
          equipment after a competitive procurement process.</p>
          <p>About Bombardier</p>
          <p>Bombardier makes transportation equipment worldwide.</p>
          <p>Note to Editors</p>
          <p>A photo is available on our web site.</p>
          <p>BOMBARDIER and FLEXITY are trademarks of Bombardier Inc.</p>
          <p>-30-</p>
          <p>FOR FURTHER INFORMATION PLEASE CONTACT:</p>
          <p>North America: +1 450 441 3007 press@example.com</p>
          <p>INDUSTRY: Transportation and Logistics</p>
          <p>SUBJECT: BFC</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-07-23/"
            "bombardier-wins-a-transportation-contract"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "competitive procurement process" in result.plain_text
    assert "Bombardier makes transportation equipment" in result.plain_text
    assert "Note to Editors" not in result.plain_text
    assert "FOR FURTHER INFORMATION" not in result.plain_text
    assert "INDUSTRY: Transportation" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_removes_partner_author_bio_tail():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title"
            content="A sustainability innovation column">
          <meta property="article:published_time"
            content="2012-05-15T20:21:49Z">
        </head><body class="harvard_business_review">
          <div id="story_head">
            <cite class="byline">By Andrew Winston</cite>
          </div>
          <div id="story_content">
            <p>The manufacturer developed durable materials that reduce
            energy use and waste across several industrial processes.</p>
            <p>The program also saved money by preventing pollution before
            factories needed to clean it up after production.</p>
            <p>Andrew Winston is the co-author of the best-seller Green to
            Gold and the author of Green Recovery. He advises some of the
            world's biggest companies on environmental strategy. Follow him
            on Twitter at @GreenAdvantage.</p>
          </div>
        </body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-05-15/"
            "a-sustainability-innovation-column"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "durable materials" in result.plain_text
    assert "preventing pollution" in result.plain_text
    assert "Andrew Winston is the co-author" not in result.plain_text
    assert "Follow him on Twitter" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_separates_inline_media_credit_from_caption():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Charts explain a court ruling">
          <meta property="article:published_time"
            content="2015-09-30T12:00:00Z">
        </head><body><article>
          <p>The court ruling changed how states administer sentences and
          prompted a broader review of the available evidence.</p>
          <p>A second paragraph supplies enough reporting for a complete
          archived article rather than a short media shell.</p>
          <figure class="inline-image inline-media">
            <img src="https://assets.bwbx.io/images/chart-one/v1/-1x-1.png">
            <figcaption class="inline-media__info">
              <div class="inline-media__caption">Sentences by year</div>
              <div class="inline-media__credit">Source: U.S. Supreme Court</div>
            </figcaption>
          </figure>
          <figure class="inline-image inline-media">
            <img src="https://assets.bwbx.io/images/chart-two/v1/-1x-1.png">
            <figcaption class="inline-media__info">
              <div class="inline-media__caption">Source: U.S. Supreme Court</div>
            </figcaption>
          </figure>
        </article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-09-30/"
            "charts-explain-a-court-ruling"
        ),
    )

    assert result.quality.status.value == "complete"
    assert len(result.images) == 2
    assert result.images[0].caption == "Sentences by year"
    assert result.images[0].credit == "Source: U.S. Supreme Court"
    assert result.images[1].caption is None
    assert result.images[1].credit == "Source: U.S. Supreme Court"
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_insurance_journal_copy_excludes_default_and_poll_images():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title"
            content="Former senate leader pleads not guilty">
          <meta property="og:url"
            content="https://www.insurancejournal.com/news/east/2015/06/01/370078.htm">
          <meta property="og:image"
            content="https://www.insurancejournal.com/img/social/opengraph/ij-social-default-1200x630.png">
          <meta property="article:published_time"
            content="2015-06-01T12:00:00Z">
        </head><body><article>
          <div class="entry-content">
            <div class="article-content clearfix">
              <p>The former state senate leader pleaded not guilty to
              corruption charges during a hearing in federal court.</p>
              <p>Prosecutors described the alleged payments while defense
              lawyers disputed the government's account of the transactions.</p>
            </div>
            <div class="article-poll">
              <div class="article-poll-more-articles">
                <div class="article-grid-list row">
                  <a href="/news/2026/recommendation">
                    <img width="134" height="134"
                      src="https://www.insurancejournal.com/app/uploads/2026/07/recommendation-150x150.jpg">
                    A current unrelated recommendation
                  </a>
                </div>
              </div>
            </div>
          </div>
        </article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-06-01/"
            "former-senate-leader-pleads-not-guilty"
        ),
        allow_generic_syndication=True,
    )

    assert result.quality.status.value == "complete"
    assert "pleaded not guilty" in result.plain_text
    assert "defense lawyers" in result.plain_text
    assert "unrelated recommendation" not in result.plain_text
    assert all(not image.should_archive for image in result.images)
    assert all(
        "ij-social-default" not in image.original_url
        for image in result.images
    )
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_removes_legacy_view_author_module_and_avatar():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="A legacy Bloomberg View column">
          <meta property="article:published_time"
            content="2013-06-26T12:00:00Z">
          <meta property="og:image"
            content="https://www.bloomberg.com/images/bview/columnists/60x80/carlson_margaret.jpg">
        </head><body><article><div id="story_display">
          <p>The opening paragraph explains the court decision and its
          consequences for families across the country.</p>
          <p>The second paragraph provides enough reporting and analysis to
          preserve the complete substance of this archived column.</p>
          <div class="story_inline assets clearfix">
            <div class="author clearfix">
              <img alt="Margaret Carlson"
                src="http://cdn.gotraffic.net/v/archive/images/bview/columnists/60x80/carlson_margaret.jpg">
              <div class="bio">
                <h4>About Margaret Carlson</h4>
                <p>Margaret Carlson is a Bloomberg View columnist. MORE</p>
              </div>
            </div>
            <div class="related">
              <h4>More from Margaret Carlson:</h4>
              <ul><li><a href="/news/related-column">
              An unrelated earlier column</a></li></ul>
            </div>
          </div>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-06-26/"
            "a-legacy-bloomberg-view-column"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "opening paragraph" in result.plain_text
    assert "second paragraph" in result.plain_text
    assert "About Margaret Carlson" not in result.plain_text
    assert "More from Margaret Carlson" not in result.plain_text
    assert "unrelated earlier column" not in result.plain_text
    assert all(
        "/bview/columnists/" not in image.original_url
        for image in result.images
    )
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_keeps_short_article_wrapped_with_contact_footer():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Producer closes flooded wells">
          <meta property="article:published_time"
            content="2010-07-23T12:00:00Z">
        </head><body><article><div class="body-copy">
          <p>The producer closed 130 wells after flooding reached its
          northeastern oil field, the company said in a statement.</p>
          <p>Output was reduced while crews inspected equipment and
          prepared repairs at the affected facilities.</p>
          <p>To contact the reporter for this story:
          Reporter Name at reporter@bloomberg.net</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-07-23/"
            "producer-closes-flooded-wells"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "producer closed 130 wells" in result.plain_text
    assert "crews inspected equipment" in result.plain_text
    assert "@bloomberg.net" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_drops_single_letter_before_contact_footer():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title"
            content="Economic warrants rise with reported growth">
          <meta property="article:published_time"
            content="2013-09-23T12:00:00Z">
        </head><body><article><div class="body-copy">
          <p>Warrants tied to economic growth rose after the government
          published its latest estimate for the second quarter.</p>
          <p>Analysts said the official figure could trigger a payment to
          investors under the securities' contractual terms.</p>
          <p>a </p>
          <p>To contact the reporter on this story:
          Reporter Name at reporter@bloomberg.net</p>
          <p>To contact the editor responsible for this story:
          Editor Name at editor@bloomberg.net</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-09-23/"
            "economic-warrants-rise-with-reported-growth"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "published its latest estimate" in result.plain_text
    assert "\na\n" not in f"\n{result.plain_text}\n"
    assert "@bloomberg.net" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_drops_veritas_prep_trial_cta():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title"
            content="Five Easy Ways to Improve Your Speed on the SAT">
          <meta property="article:published_time"
            content="2014-01-15T12:00:00Z">
        </head><body><article>
          <p><em>This tip on improving your SAT score was provided by
          <a href="https://www.veritasprep.com/sat/">Veritas Prep</a>.</em></p>
          <p>Time is of the essence on the SAT, and each section gives
          students barely enough time to answer all of the questions.</p>
          <p>Circle final answers in the test booklet before transferring
          a whole page of responses to the answer sheet.</p>
          <p>Plan on taking the SAT soon?
          <a href="https://www.veritasprep.com/sat/free-trial/">
          Sign-up for a trial</a> of Veritas Prep SAT 2400 on Demand.</p>
        </article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-01-15/"
            "five-easy-ways-to-improve-your-speed-on-the-sat"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "provided by Veritas Prep" in result.plain_text
    assert "Circle final answers" in result.plain_text
    assert "Sign-up for a trial" not in result.plain_text
    assert "SAT 2400 on Demand" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_drops_view_read_more_colon_footer():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title"
            content="Congress Faces Difficult Budget Choices">
          <meta property="article:published_time"
            content="2013-01-30T12:00:00Z">
        </head><body><article><div id="story_content">
          <p>Congress will have to make difficult choices about taxes and
          spending as lawmakers prepare the next federal budget.</p>
          <p>Getting to balance in a decade will require sustained work
          beyond the first round of negotiations.</p>
          <p>(A Bloomberg View columnist. The opinions expressed are her
          own.)</p>
          <p>Read more opinion online from
          <a href="https://www.bloomberg.com/view/">Bloomberg View</a>:</p>
          <p>To contact the writer of this article:
          writer@bloomberg.net</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-01-30/"
            "congress-faces-difficult-budget-choices"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "difficult choices" in result.plain_text
    assert "opinions expressed are her own" in result.plain_text
    assert "Read more opinion online" not in result.plain_text
    assert "@bloomberg.net" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_trims_contacts_from_preformatted_table():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Bank reduces compensation">
          <meta property="article:published_time"
            content="2011-01-19T14:30:37Z">
        </head><body><article><div class="body-copy">
          <p>The bank reduced compensation after annual revenue declined,
          according to a statement released Wednesday.</p>
          <p>The following table compares compensation at two banks.</p>
          <p>-- Bloomberg News</p>
          <pre>
Revenue                  $39.2 billion       $26.2 billion
Compensation             $15.4 billion       $9.73 billion
Average Comp/Employee    $430,700            $369,651

To contact the reporters on this story:
Reporter One at reporter1@bloomberg.net
To contact the editor responsible for this story:
Editor One at editor1@bloomberg.net
          </pre>
          <pre>
x - Ex-dividend.
* - Ex-earnings.

-- Bloomberg News
To contact Bloomberg News for this story:
+1-212-318-2000 or newsdev@bloomberg.net

-0- Dec/28/2010 16:30 GMT
          </pre>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-01-19/"
            "bank-reduces-compensation"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "$39.2 billion" in result.plain_text
    assert "Average Comp/Employee" in result.plain_text
    assert "x - Ex-dividend" in result.plain_text
    assert "* - Ex-earnings" in result.plain_text
    assert "-- Bloomberg News" not in result.plain_text
    assert "To contact" not in result.plain_text
    assert "@bloomberg.net" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_drops_terminal_regional_news_footer():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title"
            content="USDA Boxed Beef Cutout Closing Prices for June 16">
          <meta property="article:published_time"
            content="2015-06-16T19:00:00Z">
        </head><body><article><div class="body-copy">
          <p>The following table lists U.S. boxed beef cutout values
          reported by the Department of Agriculture.</p>
          <pre>
Choice 600-900       249.41
Select 600-900       241.58
Total loads          122.37
          </pre>
          <p>Market News: Commodity News: NI LVS Livestock NI CMD
          Commodities NI CATTLE Cattle CMDY Commodity news NI AGR
          Agriculture</p>
          <p>Regional News: NI US United States NI NE Nebraska</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2015-06-16/"
            "usda-boxed-beef-cutout-closing-prices-for-june-16"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Choice 600-900" in result.plain_text
    assert "Total loads" in result.plain_text
    assert "Regional News" not in result.plain_text
    assert "Market News" not in result.plain_text
    assert "Commodity News" not in result.plain_text
    assert "NI US" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_drops_terminal_wire_end_marker():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Hong Kong Short Selling Turnover">
          <meta property="article:published_time" content="2011-03-14T05:00:00Z">
        </head><body><article><div class="body-copy">
          <p>Total short-selling turnover was HKD 1,304,792,532.</p>
          <p>Note: Figures are preliminary and subject to revision.</p>
          <p>-0- Mar/14/2011 5:00 GMT</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-03-14/"
            "hong-kong-short-selling-turnover-recorded-03-14-2011-table-"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "short-selling turnover" in result.plain_text
    assert "Figures are preliminary" in result.plain_text
    assert "-0- Mar/14/2011" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_drops_terminal_updated_go_instruction():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Michaels Plans Debt">
          <meta property="article:published_time" content="2010-10-07T05:00:00Z">
        </head><body><article><div class="body-copy">
          <p>Borrowers returned to the market as spreads narrowed, allowing
          companies with pending financing plans to consider new debt sales
          after investors showed renewed appetite for high-yield securities.</p>
          <p>(Updated June 17. See {TNI NEWBON MONGOLIA &lt;GO&gt;}.)</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-10-07/"
            "michaels-plans-debt-as-junk-spreads-narrow-new-issue-alert"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Borrowers returned" in result.plain_text
    assert "TNI NEWBON" not in result.plain_text
    assert "Updated June 17" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_strips_terminal_go_code_from_source_line():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Weekly Petroleum Status Report">
          <meta property="article:published_time" content="2014-09-24T05:00:00Z">
        </head><body><article><div class="body-copy">
          <p>Jet fuel product supplied is up compared with last year, while
          refinery utilization and crude inventories changed during the week
          covered by the Energy Department's latest status report.</p>
          <p>SOURCE: U.S. Department of Energy {DOE &lt;GO&gt;}</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-09-24/"
            "u-s-doe-weekly-petroleum-status-report-for-sept-19-text-"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "SOURCE: U.S. Department of Energy" in result.plain_text
    assert "DOE <GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_strips_inline_terminal_calendar_codes():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Japan Calendar">
          <meta property="article:published_time" content="2010-11-28T05:00:00Z">
        </head><body><article><div class="body-copy">
          <p>The following is a calendar of expected financial events in Japan.
          Other calendars can be found at: {ECO JN &lt;GO&gt;} for economic
          indicators, {ACDR &lt;GO&gt;} for earnings, and {CACT &lt;GO&gt;} for
          corporate actions.</p>
          <p>Times may change.</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2010-11-28/"
            "japan-calendar-nov-29-dec-3"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "calendar of expected financial events" in result.plain_text
    assert "for economic indicators" in result.plain_text
    assert "ECO JN" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_strips_unbraced_inline_terminal_calendar_code():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Daily Briefing">
          <meta property="article:published_time" content="2014-11-10T05:00:00Z">
        </head><body><article><div class="body-copy">
          <p>The APEC summit is under way. Click here for coverage or enter
          GMEET &lt;GO&gt;. Sifma holds its annual meeting.</p>
          <p>Markets were mixed as investors assessed earnings, economic
          reports and the outlook for central-bank policy through the end of
          the year. Traders said the calendar may influence short-term
          positioning, while longer-term valuations remained the dominant
          consideration for portfolio managers.</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-11-10/"
            "pink-floyd-s-weekend-wasn-t-as-good-as-n-y-jets-opening-line"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Click here for coverage or enter" in result.plain_text
    assert "Sifma holds its annual meeting." in result.plain_text
    assert "GMEET" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_drops_bankruptcy_column_terminal_notice():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Palm Harbor Bankruptcy Update">
          <meta property="article:published_time" content="2011-08-10T12:00:00Z">
        </head><body><article><div class="body-copy">
          <p>The court approved an extension for the debtor to file a plan.</p>
          <p>Bill Rochelle is away. For today\xe2\x80\x99s U.S. bankruptcy column and
          any updates, see {TNI BCY US BN &lt;GO&gt;}.</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2011-08-10/"
            "palm-harbor-schomac-group-southwest-georgia-bankruptcy"
        ),
    )

    assert "court approved an extension" in result.plain_text
    assert "Bill Rochelle is away" not in result.plain_text
    assert "TNI BCY US BN" not in result.plain_text
    assert "<GO>" not in result.plain_text


def test_bloomberg_parser_trims_terminal_related_news_suffix():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title"
            content="Olympic Host City Shortlist Announced">
          <meta property="article:published_time"
            content="2012-05-23T12:00:00Z">
        </head><body><article><div class="body-copy">
          <p>Election of host city will take place Sept. 7, 2013.
          &lt;ul&gt;&lt;li&gt;Baku and Doha were other applicants&lt;/li&gt;
          &lt;li&gt;IOC report http://alturl.com/xrj67&lt;/li&gt;&lt;/ul&gt;
          For Related News and Information: First Word scrolling panel:
          {FIRST&lt;GO&gt;} First Word newswire: {NH BFW&lt;GO&gt;}</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2012-05-23/"
            "istanbul-tokyo-madrid-shortlisted-for-2020-olympics"
        ),
    )

    assert result.quality.status.value == "complete"
    assert "Election of host city" in result.plain_text
    assert "Baku and Doha were other applicants" in result.plain_text
    assert "IOC report http://alturl.com/xrj67" in result.plain_text
    assert "<ul>" not in result.plain_text
    assert "For Related News" not in result.plain_text
    assert "First Word" not in result.plain_text
    assert "<GO>" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_bloomberg_parser_removes_inline_index_terminal_command():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Copper Traders Increase Bets">
          <meta property="article:published_time" content="2014-12-05T12:00:00Z">
        </head><body><article><div class="body-copy">
          <p>Speculators increased their net long position by 4,654 contracts.
          See CFCDTSWN &lt;Index&gt; GP W &lt;GO&gt;.</p>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2014-12-05/"
            "copper-traders-increase-bets-on-price-drop-cftc-data-shows"
        ),
    )

    assert "Speculators increased their net long position" in result.plain_text
    assert "CFCDTSWN" not in result.plain_text
    assert "<GO>" not in result.plain_text


def test_bloomberg_parser_drops_food_safety_news_donation_card():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Meat Inspector Furloughs">
          <meta property="article:published_time"
            content="2013-03-05T12:00:00Z">
        </head><body><article><div class="body-copy">
          <p>Meat inspector furloughs will take several months to take
          effect, the agriculture secretary said.</p>
          <p>State agencies said they would continue monitoring food
          safety while the federal plan is implemented.</p>
          <div class="mx-5 bg-blue-primary">
            <h2>Your Support Protects Public Health</h2>
            <p>Food Safety News is nonprofit and reader-funded. Your
            TAX-FREE gift ensures ongoing coverage of outbreaks, recalls,
            and regulations for everyone.</p>
            <a href="https://example.com/donate">Donate Today</a>
          </div>
        </div></article></body></html>
        """,
        publisher="bloomberg",
        canonical_url=(
            "https://www.bloomberg.com/news/articles/2013-03-05/"
            "meat-inspector-furloughs-to-take-several-months-vilsack-says"
        ),
    )

    assert "Meat inspector furloughs" in result.plain_text
    assert "Food Safety News is nonprofit" not in result.plain_text
    assert "Your Support Protects Public Health" not in result.plain_text
    assert "Donate Today" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.300"


def test_npr_parser_removes_underscore_only_separators():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="NPR transcript example">
          <meta property="article:published_time"
                content="2020-01-04T12:00:00Z">
        </head><body><div id="storytext">
          <p>The first paragraph contains enough substantive reporting to be
          retained as part of the archived NPR article body.</p>
          <p>_______________________________________________________</p>
          <p>The second paragraph continues the report and must remain after
          the visual separator is removed from the normalized article.</p>
          <p>___</p>
        </div></body></html>
        """,
        publisher="npr",
        canonical_url=(
            "https://www.npr.org/2020/01/04/793364307/"
            "timeline-example"
        ),
    )

    assert "first paragraph" in result.plain_text
    assert "second paragraph" in result.plain_text
    assert "___" not in result.plain_text
    assert result.extraction.parser_version == "npr-parser/0.1.5"


def test_npr_parser_preserves_short_audio_story_mp3():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="NPR audio story">
          <meta property="article:published_time"
                content="2021-01-20T12:00:00Z">
        </head><body class="no-transcript">
          <div id="storytext">
            <p>Today on The Indicator: an update on the trade spat between
            China and the United States.</p>
            <figure><img src="https://media.npr.org/example.jpg">
              <figcaption>JIM WATSON/Jim Watson/AFP/Getty Images</figcaption>
            </figure>
          </div>
          <article class="bucketwrap resaudio">
            <div class="audio-module">
              <a class="audio-module-listen"
                 href="https://ondemand.npr.org/example.mp3?dl=1">
                Listen
              </a>
            </div>
          </article>
        </body></html>
        """,
        publisher="npr",
        canonical_url=(
            "https://www.npr.org/2021/01/20/958689598/audio-story"
        ),
    )

    assert result.content_type.value == "audio"
    assert result.quality.status.value == "complete"
    assert "Today on The Indicator" in result.plain_text
    assert [
        block.embed_url for block in result.blocks if block.type.value == "embed"
    ] == ["https://ondemand.npr.org/example.mp3?dl=1"]
    assert result.extraction.parser_version == "npr-parser/0.1.5"


def test_npr_parser_classifies_unavailable_short_audio_story():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="NPR upcoming audio story">
          <meta property="article:published_time"
                content="2026-01-18T12:00:00Z">
        </head><body class="is-DACS-only no-transcript">
          <div id="storytext"><p>A short audio introduction.</p></div>
        </body></html>
        """,
        publisher="npr",
        canonical_url=(
            "https://www.npr.org/2026/01/18/nx-s1-5668490/audio-story"
        ),
    )

    assert result.content_type.value == "audio"
    assert result.quality.status.value == "partial"
    assert result.plain_text == "A short audio introduction."
    assert not any(block.type.value == "embed" for block in result.blocks)
    assert result.extraction.parser_version == "npr-parser/0.1.5"


def test_npr_parser_prefers_complete_legacy_transcript_over_teaser():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Legacy NPR transcript">
          <meta property="article:published_time"
                content="2012-01-03T12:00:00Z">
        </head><body class="tmplNewsStory">
          <div id="storytext"><p>A short introduction to the segment.</p></div>
          <div class="transcript">
            <p class="disclaimer">Copyright 2012 National Public Radio.
            For personal, noncommercial use only. See Terms of Use.</p>
            <p>HOST: The full archived interview starts here and contains
            the substantive reporting that the short introduction omits.</p>
            <p>REPORTER: This second paragraph adds enough detail to prove
            that the complete transcript, rather than the teaser, was kept.</p>
            <p>GUEST: The final exchange preserves the rest of the historical
            radio segment for research and reproducible parsing.</p>
          </div>
          <img src="https://media.npr.org/chrome/news/nprlogo_138x46.gif">
        </body></html>
        """,
        publisher="npr",
        canonical_url=(
            "https://www.npr.org/2012/01/03/144647124/legacy-transcript"
        ),
    )

    assert result.content_type == ContentType.TRANSCRIPT
    assert result.quality.status == ArticleStatus.COMPLETE
    assert "full archived interview" in result.plain_text
    assert "A short introduction to the segment." not in result.plain_text
    assert "noncommercial use" not in result.plain_text
    assert result.quality.images_selected == 0
    assert result.extraction.parser_version == "npr-parser/0.1.5"


def test_npr_parser_recovers_legacy_multimedia_slideshow_image():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="Legacy NPR photo gallery">
          <meta property="article:published_time"
                content="2012-01-09T12:00:00Z">
        </head><body class="tmplNewsMultimedia type1">
          <div id="sectionWrap" class="multimediaPage">
            <nav><img src="https://media.npr.org/chrome/news/nprlogo.gif"></nav>
            <div id="slideshow144931285">
              <img src="https://media.npr.org/assets/fullscreen/onthetrail_01.jpg"
                   alt="Slideshow">
              <p>Images from the campaign trail.</p>
            </div>
            <aside><img src="https://ads.example/promo.jpg"></aside>
          </div>
        </body></html>
        """,
        publisher="npr",
        canonical_url=(
            "https://www.npr.org/2012/01/09/144931252/legacy-gallery"
        ),
    )

    assert result.content_type == ContentType.GALLERY
    assert result.quality.status == ArticleStatus.COMPLETE
    assert len(result.images) == 1
    assert result.images[0].should_archive is True
    assert "onthetrail_01.jpg" in result.images[0].original_url
    assert "promo.jpg" not in result.body_html
    assert result.extraction.parser_version == "npr-parser/0.1.5"


def test_nyt_parser_separates_credit_only_captions_and_removes_byline_avatar():
    result = parse_article(
        b"""
        <html><head>
          <meta property="og:title" content="How to travel safely">
          <meta property="article:published_time"
            content="2020-09-09T12:00:00Z">
        </head><body><article>
          <p>Transit riders can reduce risk by choosing less crowded
          services and following updated guidance inside stations.</p>
          <figure>
            <img src="https://static01.nyt.com/images/transit1.jpg">
            <figcaption><span data-testid="credit">
            Mark Wickens for The New York Times</span>
            </figcaption>
          </figure>
          <p>Passengers should allow extra time, wear a mask when
          required and pay attention to signs directing foot traffic.
          Operators may adjust service as conditions change.</p>
          <button aria-label="expand or collapse modal">
            Expand related media
          </button>
          <button class="ad-slide-skip">Skip advertisement</button>
          <button class="button comments-button"></button>
          <figure data-testid="byline">
            <img alt="Katherine Cusumano"
              src="https://static01.nyt.com/images/author-katherine.jpg">
            <strong data-test-id="author-name">Katherine Cusumano</strong>
          </figure>
          <p>___</p>
          <p>[ Like the Science Times page on Facebook. | Sign up for
          the Science Times newsletter. ]</p>
          <figure>
            <img alt="Maggie Astor"
              src="https://static01.nyt.com/images/2018/07/18/multimedia/author-maggie-astor/author-maggie-astor-thumbLarge.png">
          </figure>
        </article></body></html>
        """,
        publisher="nyt",
        canonical_url=(
            "https://www.nytimes.com/interactive/2020/09/09/"
            "at-home/coronavirus-mass-transit-safety.html"
        ),
    )

    transit = next(
        image
        for image in result.images
        if image.original_url.endswith("transit1.jpg")
    )
    assert transit.caption is None
    assert transit.credit == "Mark Wickens for The New York Times"
    assert all(
        "author-katherine" not in image.original_url
        for image in result.images
    )
    assert "Katherine Cusumano" not in result.plain_text
    avatar = next(
        image
        for image in result.images
        if "author-maggie-astor-thumbLarge" in image.original_url
    )
    assert avatar.role.value == "author-avatar"
    assert avatar.should_archive is False
    assert "___" not in result.plain_text
    assert "Science Times newsletter" not in result.plain_text
    assert "<button" not in result.body_html
    assert "Skip advertisement" not in result.plain_text
    assert result.extraction.parser_version == "nyt-parser/0.8.54"
