# JOJO News Archive Runner

Open-source research tooling for building reproducible, resumable news archives
from publicly indexed web snapshots.

This temporary runner repository supports the nonprofit JOJO Platform research
project while the main platform is being prepared for open-source release.

## Current scope

The generic pipeline supports these publisher adapters:

- AP News
- The Wall Street Journal
- Bloomberg
- The New York Times
- Reuters
- Financial Times
- Axios
- NPR
- Nikkei
- Lianhe Zaobao
- Al Jazeera
- South China Morning Post
- Caixin

The archive is intentionally split into two independent stages:

1. **Raw capture** discovers archive candidates and stores the selected original
   HTML response plus provenance metadata. It does not parse the article or
   download images.
2. **Versioned parsing** replays a raw capture into `jojo-article/1`, preserving
   ordered content blocks and classifying image references before any image is
   selected for archival.

This lets parser changes run against stable source bytes without repeatedly
requesting the upstream archive.

## What is public

- Downloader, discovery, parser, and storage source code
- Tests and GitHub Actions workflows
- JSON Schemas for raw captures and normalized articles
- Snapshot URL manifests and archive metadata

## What stays private

Downloaded HTML, normalized article bodies, images, and SQLite checkpoints are
written to a B2 bucket that the workflow verifies is private. Downloaded content
is never committed to Git and is not uploaded as a GitHub Actions artifact.

## Storage and workflow

The generic `News raw archive` workflow:

1. Restores its Wayback discovery and raw-capture checkpoints from B2.
2. Advances a bounded discovery or capture batch.
3. Uploads immutable, content-addressed raw HTML and capture records.
4. Uploads the SQLite checkpoint last.
5. Optionally dispatches the next bounded run.

See [NEWS_ARCHIVE.md](services/olds-api/NEWS_ARCHIVE.md) for the schema, storage
layout, local commands, and GitHub Actions inputs.

The older Bloomberg-only downloader remains for migration and regression
testing, but new archives use the capture-only pipeline.

## Parser convergence roadmap

The temporary runner remains the active home while validation is in progress.

- Active convergence set: Financial Times, Wall Street Journal, Axios, NPR,
  New York Times, and Caixin.
- Completed baseline: Bloomberg.
- TODO: resume AP, Nikkei, Lianhe Zaobao, Al Jazeera, and South China Morning
  Post after the active set has stable per-year 800-article cohorts.
- TODO: migrate the runner, workflows, secrets documentation, and open
  validation history to the public
  [`kargonerd/jojokanbao`](https://github.com/kargonerd/jojokanbao) repository.
  Do not switch repositories while Actions batches are still using validation
  checkpoints in this repository.

## License and content notice

The software in this repository is licensed under the MIT License. Third-party
news content is not distributed by this repository and remains subject to the
rights and terms of its original publishers and archive providers. Users are
responsible for ensuring that their use is authorized and lawful.
