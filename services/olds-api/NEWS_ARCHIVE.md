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

## B2 layout

For a publisher and discovery window, the workflow writes:

```text
news-archive/v1/{publisher}/{fromYear}-{toYear}/
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

HTML objects are addressed by the SHA-256 of the uncompressed response. Gzip is
deterministic (`mtime=0`), so repeated identical captures produce the same B2
object. Each canonical publisher URL appears once in a manifest with at most
three ranked fallback snapshots. A successful capture stores only the first
usable candidate.

Objects and records are uploaded before the capture checkpoint. A restored
checkpoint therefore never references data that has not reached B2.

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

For normal operation, use `manifest_mode: wayback`. Discovery checkpoints are
published after each bounded run. Capture begins only after every configured CDX
query for that publisher window is complete.
