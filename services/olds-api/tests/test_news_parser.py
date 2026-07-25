from __future__ import annotations

from datetime import datetime, timezone
import json

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
    assert article.extraction.parser_version == "ft-parser/0.5.0"


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
    assert article.extraction.parser_version == "ap-parser/0.5.0"


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
    assert result.extraction.parser_version == "nyt-parser/0.5.0"
