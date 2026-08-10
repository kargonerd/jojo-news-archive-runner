from __future__ import annotations

from urllib.parse import urlencode


INFINI_DATASET = "ruggsea/infini-news-corpus"
INFINI_DATASET_ROWS_ENDPOINT = "https://datasets-server.huggingface.co/rows"


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
