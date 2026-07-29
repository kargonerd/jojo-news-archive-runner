from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.parse import quote

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
    assert result.extraction.parser_version == "wsj-parser/0.8.11"


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
      validate image rendition deduplication without relying on a shell.</p>
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
    assert result.extraction.parser_version == "wsj-parser/0.8.11"


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
    assert result.extraction.parser_version == "wsj-parser/0.8.11"


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
      <p>Officials said engineers were still investigating the...</p>
    </div></article></body></html>
    """

    result = parse_article(
        html,
        publisher="wsj",
        canonical_url="https://www.wsj.com/articles/bangladesh-power-1414915894",
    )

    assert result.quality.status.value == "partial"
    assert "truncated-body" in result.quality.warnings


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
    assert result.extraction.parser_version == "wsj-parser/0.8.11"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.28"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.8"


def test_bloomberg_parser_keeps_listen_to_article_as_article():
    reporting = " ".join(["Bloomberg reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="A Bloomberg Text Article">
      <meta property="article:published_time"
            content="2020-01-04T12:00:00Z">
    </head><body><article>
      <div class="body-copy-v2">
        <h2>LISTEN TO ARTICLE</h2>
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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.8"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.8"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.8"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.8"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.8"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.28"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.9"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.9"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.9"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.9"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.8"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.28"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.28"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.28"


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
    assert article.extraction.parser_version == "ft-parser/0.8.12"


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
          – Copyright The Financial Times Limited 2025
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
    assert article.extraction.parser_version == "ft-parser/0.8.12"


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
    assert article.extraction.parser_version == "ft-parser/0.8.12"


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
    assert article.extraction.parser_version == "ft-parser/0.8.12"


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
    assert article.extraction.parser_version == "ft-parser/0.8.12"


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
    assert article.extraction.parser_version == "ft-parser/0.8.12"


def test_ap_parser_removes_legacy_newsletter_promo_and_separator():
    reporting = " ".join(["AP reporting sentence."] * 30)
    html = f"""
    <html><head>
      <meta property="og:title" content="AP regional report">
      <meta property="article:published_time"
            content="2018-04-12T12:00:00Z">
    </head><body><article>
      <p>{reporting}</p>
      <p>___</p>
      <p>More AP college football: https://apnews.com/college-football.
      Sign up for the AP’s weekly newsletter showcasing our best
      reporting: http://apne.ws/example</p>
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
    assert "Jackpot.com" not in result.plain_text
    assert "___" not in result.plain_text
    assert result.extraction.parser_version == "ap-parser/0.6.13"


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
    assert article.extraction.parser_version == "ap-parser/0.6.13"


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
    assert result.extraction.parser_version == "ap-parser/0.6.13"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.28"


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
        state[image_id] = {
            "__typename": "Image",
            "legacyHtmlCaption": f"<strong>Bag {index}</strong>, $100.",
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
        f"Bag {index} , $100."
        for index in range(3)
    ]
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
    assert result.extraction.parser_version == "nyt-parser/0.8.28"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.28"


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
          <div class="Post__body">{paragraphs}</div>
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
