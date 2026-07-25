# JOJO news archive pipeline

## Design

Raw acquisition and article interpretation are separate:

```text
publisher catalog
  -> Wayback CDX candidates
  -> jojo-raw-capture/1 + content-addressed HTML
  -> versioned publisher parser
  -> jojo-article/1
  -> selected editorial image downloads (later stage)
```

The raw stage stores the response bytes before Beautiful Soup or any other
parser changes them. Image URLs are recorded only by the parser. Images are not
downloaded by `capture_archive_batch.py`.

Parser readiness is measured on a reproducible, publisher-and-year-stratified
sample. The archive workflow selects URLs by a stable SHA-256 priority, captures
years in round-robin order, and evaluates at least 500 articles for every
configured year. Validation stores metrics and issue codes, never article body
text. A publisher/year is not ready until it has 500 evaluated samples, no
parser exceptions, at least a 95% complete-extraction rate, and at least a 95%
QA-pass rate.

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

HTML objects are addressed by the SHA-256 of the uncompressed response. Gzip is
deterministic (`mtime=0`), so repeated identical captures produce the same B2
object. Each canonical publisher URL appears once in a manifest with ranked
fallback snapshots. The capture worker evaluates usable candidates and stores
only the highest-quality response; it stops early when a response reaches the
maximum raw quality score. FT manifests try archived AMP article pages before
canonical pages because canonical snapshots can contain only a subscription
shell.

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

Supported publisher IDs are `ap`, `wsj`, `bloomberg`, `nyt`, `reuters`, and
`ft`.

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
CDX prefix queries and tends to select better article versions.

Use `manifest_mode: wayback` for partitioned CDX adapters such as WSJ. Discovery
checkpoints are published after each bounded run. Capture begins only after
every configured query for that publisher window is complete.

WSJ and legacy Reuters also run a parallel `wayback-urlkey` shard. It asks CDX
for one first capture per unique URL, instead of paging through every distinct
HTML digest. This provides the cross-year parser-validation corpus much sooner
while the original digest-mode shards continue the deeper, three-candidate
archive discovery. When a canonical URL has no embedded date, the first capture
timestamp supplies the provisional sampling year; the parser still prefers the
publication metadata contained in the archived page.

Reuters uses two catalog shards because its URL design changed:

- `wayback` for the legacy `/article/` catalog (2016–2020);
- `reuters-sitemap-wayback` for 2021 onward. This mode enumerates archived
  snapshots of Reuters' rolling sitemap, extracts canonical article URLs, and
  then selects publication-near Wayback captures. For gaps after Reuters'
  archived rolling sitemaps stop, bounded weekly searches of public urlscan.io
  metadata contribute canonical Reuters URLs. Those rows retain `urlscan` as
  discovery provenance and try Wayback before a live-origin fallback; urlscan
  metadata is never treated as article content.

`News archive watchdog` checks all configured shards every hour. It skips
shards that already have a queued or running workflow and restarts only stopped
chains. The target workflow's per-shard concurrency lock remains a second guard
against simultaneous writers.
