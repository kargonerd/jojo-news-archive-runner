from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.parse import quote
import warnings

import pytest

from jojo_olds_api.news_models import (
    BlobReference,
    CaptureCandidate,
    CaptureProvider,
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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"
    assert "role=\"button\"" not in result.body_html
    assert "tabindex=" not in result.body_html
    assert "Open image in viewer" not in result.body_html
    assert "SocialShare-" not in result.body_html
    assert "Go to comments" not in result.body_html
    assert ">Comments<" not in result.body_html
    assert "Editorial photograph" in result.body_html


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"

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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert "Here's Why Apple" not in result.plain_text
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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.24"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert "Editors share highlights" in result.plain_text
    assert result.blocks[-1].embed_url == (
        "https://www.nytimes.com/interactive/projects/fashion"
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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"


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
          Copyright Business Wire 2010</p>
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
    assert "Copyright Business Wire" not in business_wire.plain_text
    assert "Investor Contact John Example" in business_wire.plain_text
    assert (
        market_wire.extraction.parser_version
        == "reuters-parser/0.7.24"
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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
          <p>Read more opinion online from Bloomberg View.</p>
          <p>Read more online opinion from Bloomberg View.</p>
          <p>Read more opinion online from Bloomberg View .</p>
          <p>Today’s highlights: the View editors on Greece’s political
          deadlock and saving the Volcker rule.</p>
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
          <p>Muse highlights include Ryan Sutton on dining and Katya
          Kazakina on art.</p>
          <p>Today’s Muse highlights include Jeremy Gerard on theater
          and John Mariani on wine.</p>
          <p>Muse highlights include: Farah Nayeri on film at Cannes,
          Guy Collins on wine and James Russell on architecture.</p>
          <p>Related News and Information:</p>
          <p>For more trademark news, click here.</p>
          <p>For trademark news, click here.</p>
          <p>For the latest verdict and settlement news, click here.</p>
          <p>For the latest new suits news, click here. For copies of
          recent civil complaints, click here.</p>
          <p>This is a Bloomberg podcast. To download, watch or listen
          to this report now, click here.</p>
          <p>For more, click here.</p>
          <p>To read more from Echoes, Bloomberg View's economic history
          blog, click here .</p>
          <p>Read more Echoes columns online .</p>
          <p>For more, read this QuickTake:
          <a href="https://www.bloomberg.com/quicktake/currency-pegs">
          Currency Pegs</a></p>
          <p>NSN MJ557B6S973D &lt;GO&gt;
          Mexico Agency Rejects Telecom Accord From Phone Regulator</p>
          <p>To analyze this 13F:
          {89754 1 &lt;client&gt; PORT&lt;go&gt;}
          {FLNG 89754 1 14&lt;go&gt;}
          To analyze all 13F's filed, {FLNG&lt;go&gt;}</p>
          <p>Emerging-markets market view: {EMMV &lt;go&gt;}</p>
          <p>相關新聞和信息： 彭博率先報道滾動屏: FIRST &lt;GO&gt;
          中文彭博率先報道: NI BFWCH BBG &lt;GO&gt;</p>
          <p>* Link to earlier story: China Lets Local Governments Swap</p>
          <p>Debt Into Municipal Bonds NSN NKXKOY6JTSE9&lt;GO&gt;</p>
          <p>Link to Company News: {DIST SL &lt;Equity&gt; CN}</p>
          <p>***END OF TRANSCRIPT***</p>
          <p>Running Time: 01:58</p>
          <p>Running time 04:00</p>
          <p>Provider ID: 671dc54518c848b7b92dcfc35e635b3a</p>
          <p>Contributed via: Bloomberg Publisher WEB Service</p>
          <p>To see the patent, click: 5,969,156.</p>
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
    assert "To contact the writer" not in result.plain_text
    assert "To contact the editor" not in result.plain_text
    assert "To contact the author" not in result.plain_text
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
    assert "Read more opinion online" not in result.plain_text
    assert "Today’s highlights" not in result.plain_text
    assert "daily e-mail highlighting" not in result.plain_text
    assert "latest verdict and settlement news" not in result.plain_text
    assert "latest new suits news" not in result.plain_text
    assert "This is a Bloomberg podcast" not in result.plain_text
    assert "For more, click here" not in result.plain_text
    assert "read more from Echoes" not in result.plain_text
    assert "Read more Echoes columns" not in result.plain_text
    assert "For more quick commentary" not in result.plain_text
    assert "Read more breaking commentary" not in result.plain_text
    assert "Muse highlights include" not in result.plain_text
    assert "Today’s Muse highlights" not in result.plain_text
    assert "Farah Nayeri on film" not in result.plain_text
    assert "Related News and Information:" not in result.plain_text
    assert "For more trademark news" not in result.plain_text
    assert "For trademark news" not in result.plain_text
    assert "read this QuickTake" not in result.plain_text
    assert "MJ557B6S973D" not in result.plain_text
    assert "To analyze this 13F" not in result.plain_text
    assert "Emerging-markets market view" not in result.plain_text
    assert "相關新聞和信息" not in result.plain_text
    assert "Link to earlier story" not in result.plain_text
    assert "NKXKOY6JTSE9" not in result.plain_text
    assert "Link to Company News" not in result.plain_text
    assert "END OF TRANSCRIPT" not in result.plain_text
    assert "Running Time:" not in result.plain_text
    assert "Running time 04:00" not in result.plain_text
    assert "Provider ID:" not in result.plain_text
    assert "Contributed via:" not in result.plain_text
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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
          <pre>
Revenue                  $39.2 billion       $26.2 billion
Compensation             $15.4 billion       $9.73 billion
Average Comp/Employee    $430,700            $369,651

To contact the reporters on this story:
Reporter One at reporter1@bloomberg.net
To contact the editor responsible for this story:
Editor One at editor1@bloomberg.net
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
    assert "To contact" not in result.plain_text
    assert "@bloomberg.net" not in result.plain_text
    assert result.extraction.parser_version == "bloomberg-parser/0.10.149"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.49"

