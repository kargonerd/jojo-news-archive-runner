# JOJO news archive pipeline

## Design

Raw acquisition and article interpretation are separate:

```text
publisher catalog
  -> Wayback CDX candidates
  -> publication-near Common Crawl WARC fallback where configured
  -> validated partner HTML or explicit Infini-News derived fallback
  -> jojo-raw-capture/1 + content-addressed HTML
  -> versioned publisher parser
  -> jojo-article/1
  -> selected editorial image downloads (later stage)
```

The raw stage normally stores response bytes before Beautiful Soup or any other
parser changes them. The one explicit exception is the FT `infini-news`
fallback: when a mapped live partner page and archive captures are unusable, it
stores deterministic HTML adapted from the complete extracted CC-News row.
Those records are marked `representation: derived-html`, retain the dataset row
URL, partner URL, WARC filename, and content hash in provenance, and can be
excluded from raw-DOM studies. Ordinary responses remain `raw-html`. Image URLs
are recorded only by the parser. Images are not downloaded by
`capture_archive_batch.py`; derived Infini rows contain no images unless a
separate validated page supplies them.

Parser readiness is measured on a reproducible, publisher-and-year-stratified
random sample. The archive workflow uses a stable SHA-256 pseudo-random
priority, captures years in round-robin order, and evaluates at least 800
articles for every configured year. The stable priority prevents resumptions
from changing the selected sample while keeping selection independent of URL
order. Already stored raw captures are sampled and replayed first; uncaptured
URLs fill only the remaining shortfall. Validation stores metrics and issue
codes, never article body text. A publisher/year is not ready until it has 800
evaluated samples, no parser exceptions, at least a 95% complete-extraction
rate, and a 100% QA-pass rate.

## B2 layout

For a publisher and discovery window, the workflow writes:

```text
news-archive/v1/{publisher}/{fromYear}-{toYear}/{manifestMode}/
  catalog/
    discovery.sqlite3.gz
    manifest.jsonl.gz
  raw/
    objects/html/{sha256[0:2]}/{sha256}.html.gz
    records/{articleSha256[0:2]}/{articleSha256}.json
  state/
    capture.sqlite3.gz
    summary.json
```

The capture checkpoint also contains the deterministic sample plan and parser
validation results. `state/summary.json` exposes progress and readiness by
year under `parserValidation`.

When a full publisher shard is progressing too slowly to exercise every year,
the `Parser validation accelerator` workflow filters that shard's existing
manifest to one year and uses an independent checkpoint. It never creates a
second raw corpus: canonical raw objects and records remain in the publisher
shard above.

```text
news-archive/v2/validation-state/{cohort}/{publisher}/{year}/
  catalog/manifest.jsonl.gz
  state/capture.sqlite3.gz
  state/summary.json
```

The filtered manifest is cached in B2 after its first use. Sampling uses the
same publisher, year, seed, parser version, 800-article target, and QA gates as
the full shard, so the result is directly comparable. The independent prefix
allows multiple years to run concurrently without two Actions jobs writing the
same SQLite checkpoint. It is an accelerator, not a second archive or a
replacement for the full archive shard.

The watchdogs share a controlled budget of at most two sustained catalog or
validation runs. Parser validation stores its selected canonical raw samples
without duplicating them below validation state. The archive watchdog is
catalog-only (`max_captures=0`), so automatic source expansion cannot silently
restart a full raw-corpus download. B2 must retain its keep-latest lifecycle
policy so superseded checkpoints do not accumulate as hidden object versions.

HTML objects are addressed by the SHA-256 of the uncompressed response or
explicitly derived representation. Gzip is deterministic (`mtime=0`), so
repeated identical captures produce the same B2 object. The raw capture record's
`representation` field distinguishes the two without changing object layout.
Each canonical publisher URL appears once in a manifest with ranked
fallback snapshots. The capture worker evaluates usable candidates and stores
only the highest-quality response; it stops early when a response reaches the
maximum raw quality score. FT capture first queries the historically
higher-yield exact Wayback timemap. Common Crawl is the bounded fallback: it
queries the nearest indexes for the exact canonical URL, range-downloads only
the indexed WARC record, validates the WARC target URL, reconstructs the
original HTTP response, and applies the same subscription-shell and raw-quality
gates. Publication-near guesses are used only when the timemap is empty.
Common Crawl and Wayback use separate host circuit breakers and bounded retries
so one unhealthy archive cannot stall every source. The raw record retains the
Common Crawl object URL, WARC filename, offset, and length.

Wayback URL-key discovery may begin capture before every query is exhausted
once every requested year has at least 1,100 unique article candidates. Discovery
continues in later resumable runs, while the 300-article buffer allows the
800-sample parser gate to tolerate unusable archive responses.

Objects and records are uploaded before the capture checkpoint. A restored
checkpoint therefore never references data that has not reached B2.
Cancelling a workflow skips checkpoint and object publishing, while ordinary
failures still publish a recoverable checkpoint.

During a long capture batch, the workflow also creates a consistent SQLite
backup and incrementally uploads completed objects every ten minutes. The
checkpoint is still published last. A runner failure therefore loses at most
the work completed since the latest live checkpoint rather than the whole
batch.

## Schemas

- [`schemas/jojo-raw-capture-v1.schema.json`](schemas/jojo-raw-capture-v1.schema.json)
  describes retrieval provenance, candidate snapshots, response metadata,
  quality signals, and the raw HTML blob reference.
- [`schemas/jojo-article-v1.schema.json`](schemas/jojo-article-v1.schema.json)
  describes normalized metadata, ordered body blocks, source links, parser
  version, extraction quality, and classified image candidates.

Regenerate them after a model change:

```bash
python tools/export_news_schemas.py
```

## Local discovery

Build or resume a publisher manifest:

```bash
python tools/build_wayback_manifest.py \
  --publisher reuters \
  --from-year 2016 \
  --to-year 2026 \
  --output .archive-work/reuters/catalog/manifest.jsonl.gz \
  --state .archive-work/reuters/catalog/discovery.sqlite3 \
  --max-pages 5
```

Supported publisher IDs are `ap`, `wsj`, `bloomberg`, `nyt`, `reuters`, `ft`,
`axios`, `npr`, `nikkei`, `zaobao`, `aljazeera`, `scmp`, and `caixin`.

## Local raw capture

```bash
python tools/capture_archive_batch.py \
  --publisher reuters \
  --manifest .archive-work/reuters/catalog/manifest.jsonl.gz \
  --output-dir .archive-work/reuters/raw \
  --workers 4 \
  --max-captures 100
```

Replay one stored capture through its versioned parser:

```bash
python tools/parse_raw_capture.py \
  --capture-record .archive-work/reuters/raw/records/aa/article.json \
  --archive-root .archive-work/reuters/raw \
  --output .archive-work/reuters/parsed/article.json
```

## GitHub Actions

The `News raw archive` workflow requires:

- `B2_ARCHIVE_KEY_ID`
- `B2_ARCHIVE_APPLICATION_KEY`
- `B2_ARCHIVE_BUCKET`

The B2 key must be restricted to the private archive bucket and allow bucket
listing plus file list/read/write/delete operations.

Set `max_captures` to `0` for a catalog-only run. That mode restores and
publishes only `catalog/discovery.sqlite3.gz` and `catalog/manifest.jsonl.gz`;
it does not restore raw capture state, download article HTML, replay a parser,
or write anything below `raw/` or `state/`.

For a storage smoke test, select:

```text
publisher: bloomberg
from_year: 2020
to_year: 2020
manifest_mode: committed-bloomberg-2020
max_captures: 2
auto_continue: false
```

For AP, Bloomberg, NYT, and FT, use `manifest_mode: sitemap-wayback`. It obtains
the canonical URL and publication month from the publisher's historical
sitemaps, then asks Wayback for snapshots near publication. This avoids large
CDX prefix queries for AP and NYT and tends to select better article versions.
For Bloomberg and FT, the same mode additionally advances bounded,
resume-keyed Wayback URL-key queries and merges their exact snapshots into the
sitemap manifest. Exact URL-key candidates take precedence over guessed
publication-near timestamps, while sitemap or validated partner publication
dates remain authoritative when available. This fills historical sitemap gaps
without creating a second capture database or discarding existing progress.

Use `manifest_mode: wayback` for partitioned CDX adapters such as WSJ. Discovery
checkpoints are published every ten minutes and after each bounded run. A
sitemap-based shard may begin capture after its sitemap baseline is complete
while its supplemental URL-key and partner catalogs continue to grow.

WSJ and legacy Reuters also run a parallel `wayback-urlkey` shard. It asks CDX
for one first capture per unique URL, instead of paging through every distinct
HTML digest. This provides the cross-year parser-validation corpus much sooner
while the original digest-mode shards continue the deeper, three-candidate
archive discovery. When a canonical URL has no embedded date, the first capture
timestamp supplies the provisional sampling year; the parser still prefers the
publication metadata contained in the archived page.

For WSJ years 2016–2023, the URL-key shard also searches Infini-News for
historical WSJ paywall/copyright templates, draws a reproducible random sample
across every matching shard, and accepts only normalized official `wsj.com`
article URLs with matching-year metadata. Infini-News supplies URL discovery
metadata only; its extracted text is never used as the raw article. Each
official URL still goes through the normal publication-near Wayback capture and
the same 800-article parser gate. This avoids treating other Dow Jones
publications that share the copyright template as WSJ articles.

For the sparse 2016–2018 WSJ years, the same catalog also performs bounded,
resumable scans of Infini-News' year-partitioned Parquet metadata. It reads only
the URL, hostname, date, headline, text-length, language, and WARC-provenance
columns until each year has 1,600 strict official-origin candidates. A row is
accepted only when the metadata hostname and URL hostname agree on an official
WSJ host, URL normalization accepts an article path, the year agrees, the
headline and text-length gates pass, the language is English, and the WARC is a
`CC-NEWS-*.warc.gz` object. The remote Parquet files and extracted article text
are never copied to B2; only the small resumable catalog state and manifest are
stored. As with the text-query catalog, the discovered URL must still produce a
usable archived page before it can enter parser validation.

For WSJ articles from 2023 onward, the same shard also enumerates the public
Wall Street Journal category on To Vima, resolves each licensed-copy headline
to its canonical `wsj.com` URL, and records the partner page as a direct
candidate. A copy is accepted only when the final host and `/wsj/` path,
headline, publication date, complete-body threshold, and visible Wall Street
Journal attribution all pass. Failed provenance checks never enter the parser
validation sample.

Axios uses a separate, resumable Common Crawl prefix catalog for 2017–2026.
The catalog checks recent collections first, because current Axios URLs retain
their publication year while older collection/prefix pairs are frequently
empty. Both successful pages and empty page-count queries are checkpointed;
each run has independent page and query limits, so a broad prefix cannot turn a
nominally bounded run into an unbounded scan. Only normalized official Axios
article paths with a URL-derived matching publication year and exact WARC
coordinates enter the supplemental manifest. Parser validation merges that
manifest with the Wayback URL-key source, then applies the same fresh-cohort,
zero-overlap 800-article gate; Common Crawl catalog capacity alone is never
treated as parser convergence.

FT discovery also augments sparse Wayback results with licensed partner
copies. It searches Infini-News' CC-News index for the exact visible
`Copyright The Financial Times Limited` attribution, samples occurrences
across the whole result range for each year, and retains the CC-News WARC
filename and document index as discovery provenance. Each partner headline is
resolved to an `ft.com/content/` URL with an exact-title search. Capture first
tries exact publisher archives and the live partner HTML. Partner HTML enters
the archive only when its final host, headline, publication date, complete-body
threshold, and visible FT copyright statement all pass.

The discovery checkpoint also serves as a local headline-and-date provenance
index for all accepted Infini-News partner rows, including rows whose canonical
FT URL could not be found by a search engine. After an exact FT archive response
reveals the original headline, capture can match it against that local index
with a same-year, two-day and 90%-token-overlap gate. It tries the indexed raw
partner URL first and the derived dataset row second. This reuses already
verified discovery work and avoids scanning hundreds of gigabytes of Parquet
files during each validator run; all downstream host, headline, date, body,
copyright, row-index and WARC checks still apply.

If those raw candidates fail, a mapped row can be fetched from Infini-News'
official Hugging Face dataset by its exact year and document index. The adapter
accepts only the expected dataset endpoint, one exact row, the mapped partner
URL and headline, a `CC-NEWS-*.warc.gz` provenance match, at least 400 body
characters, the publication-date gate, and visible FT copyright attribution.
It then creates deterministic, escaped article HTML for the same FT parser.
The resulting capture is explicitly `derived-html`, never presented as original
FT or partner HTML, and retains both source links. Failed or ambiguous mappings
remain outside the parser validation sample.

Bloomberg discovery augments sparse canonical Wayback results with licensed
partner copies. For 2017 onward, it searches Infini-News' CC-News index for the
exact year-specific visible `©YYYY Bloomberg L.P.` statement and draws a
reproducible random sample across each year's entire occurrence range. It
retains the CC-News WARC filename and document index solely as discovery
provenance, resolves each partner headline to its canonical `bloomberg.com`
URL, then obtains the publication-near partner capture from Wayback.

From 2025 onward it additionally enumerates BNN Bloomberg's public
date-addressable daily sitemaps. Because older BNN article routes now redirect
to the home page, it resolves the nearest exact Wayback capture of each partner
URL. Every Bloomberg partner capture must pass complete-body, headline,
publication-date, Bloomberg News attribution, and visible year-matched
`Bloomberg L.P.` copyright checks. BNN copies must additionally contain the
canonical Bloomberg link or a matching mirrored source slug. Infini-News text
is never stored as article content; only the independently fetched and
validated archived partner HTML is stored, with the canonical Bloomberg URL
preserved as the source link.

Reuters uses two catalog shards because its URL design changed:

- `wayback` for the legacy `/article/` catalog (2016–2020);
- `reuters-sitemap-wayback` for 2021 onward. This mode enumerates archived
  snapshots of Reuters' rolling sitemap, extracts canonical article URLs, and
  then selects publication-near Wayback captures. For gaps after Reuters'
  archived rolling sitemaps stop, bounded weekly searches of public urlscan.io
  metadata contribute canonical Reuters URLs. Those rows retain `urlscan` as
  discovery provenance and try Wayback before a live-origin fallback; urlscan
  metadata is never treated as article content.

`News archive watchdog` runs at 7 and 37 minutes past each hour, offset from
the parser-validation watchdog. A single dispatcher counts active
`news-raw-*` and `parser-*` runs, fills only the available portion of the
two-run budget, skips shards whose B2 manifest summary is already complete,
and advances incomplete shards in explicit research-priority order. It always
uses catalog-only mode; any future full-corpus capture must be a separate,
deliberate operation with a new storage-cost review.
