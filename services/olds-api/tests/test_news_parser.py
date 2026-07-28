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
    assert result.extraction.parser_version == "wsj-parser/0.8.1"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.3"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.1"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.1"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.1"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.3"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.0"


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
    assert result.extraction.parser_version == "reuters-parser/0.7.0"


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
    assert result.extraction.parser_version == "bloomberg-parser/0.10.1"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.3"


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
    assert article.extraction.parser_version == "ft-parser/0.8.4"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.3"


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
    assert result.extraction.parser_version == "nyt-parser/0.8.3"


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
