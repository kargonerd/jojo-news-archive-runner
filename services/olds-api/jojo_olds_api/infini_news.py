from __future__ import annotations

import re
from urllib.parse import urlencode


INFINI_DATASET = "ruggsea/infini-news-corpus"
INFINI_DATASET_ROWS_ENDPOINT = "https://datasets-server.huggingface.co/rows"
_SIGNIFICANT_TOKEN_RE = re.compile(r"[a-z0-9]+")


def infini_news_row_url(year: int, document_index: int) -> str:
    if year < 1900 or year > 2200:
        raise ValueError("Infini-News year is outside the supported range")
    if document_index < 0:
        raise ValueError("Infini-News document index must be non-negative")
    return INFINI_DATASET_ROWS_ENDPOINT + "?" + urlencode(
        {
            "dataset": INFINI_DATASET,
            "config": f"year_{year}",
            "split": "train",
            "offset": document_index,
            "length": 1,
        }
    )


def is_ft_subscription_headline(value: str | None) -> bool:
    """Return whether an Infini-News title is a recurring FT access shell."""
    normalized = " ".join(str(value or "").casefold().split())
    tokens = set(_SIGNIFICANT_TOKEN_RE.findall(normalized))
    if not (
        {"subscribe", "subscriber", "subscription"} & tokens
    ):
        return False
    return (
        "subscribe to read" in normalized
        or "become an ft subscriber" in normalized
        or "subscribe to ft" in normalized
        or "purchase a digital trial" in normalized
        or (
            "subscription" in tokens
            and {"purchase", "digital"}.issubset(tokens)
        )
        or {"ft", "com"}.issubset(tokens)
    )
