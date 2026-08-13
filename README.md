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

- Operational TODO: both scheduled watchdog workflows are temporarily
  disabled because GitHub schedules execute the default `main` branch, whose
  queues predate this reduced media set. Re-enable them only after this PR (or
  an equivalent queue-only backport) reaches the default branch. Explicit
  feature-branch validation jobs continue through their own `auto_continue`
  chain meanwhile.

- Completed baseline: Bloomberg.
- Active automatic convergence set: Financial Times, Axios, and Caixin. The
  watchdog may fill at most two validation slots and one catalog slot from this
  set.
- A cell counts as converged only when its 800-row summary and parser-bound
  content audit both pass, plus the zero-overlap rotation audit for holdouts.
  Failed content audits keep their checkpoint, raw HTML, and audit evidence in
  B2 but are quarantined from automatic retries until the parser or QA policy
  changes.
- In flight: FT 2016 holdout-v5 and FT 2017 holdout-v4 are fresh,
  zero-overlap cohorts for `ft-parser/0.8.38` after legacy podcast RSS chrome
  was found during an early partial audit.
- TODO: replay Axios 2017--2025 against QA revision 4. The previous 2017
  cohort contained two confirmed CMS fixtures; the previous 2022--2025
  cohorts contained respectively 1, 34, 51, and 110 malformed trailing URL
  aliases. Manifest ingestion and sample planning now collapse or skip those
  aliases. Axios 2020 and 2021 already pass the new content/identity audit,
  but still need their formal QA-revision checkpoints refreshed.
- TODO: run fresh current-parser cohorts for Caixin 2011--2015; each catalog
  year has at least 3,901 candidates. Complete the still-missing 2016--2026
  catalog summary before scheduling those later years.
- Paused after an already-started WSJ 2020 holdout exposed poor source yield
  (17 accepted samples after 305 capture failures). TODO: enlarge and audit the
  replay candidate pool before resuming that cell; do not expand into another
  WSJ year automatically.
- TODO: revalidate NYT 2019 with a fresh zero-overlap 800-article cohort after
  the latest parser fix, then resume the remaining NYT and WSJ years.
- TODO: resume NPR and AP after the reduced active set has stable per-year
  cohorts. Their existing catalogs and checkpoints remain resumable.
- TODO: then resume Nikkei, Lianhe Zaobao, Al Jazeera, and South China Morning
  Post. Their existing catalogs and checkpoints also remain resumable.
- TODO: add Reuters back to the convergence schedule after its historical
  source windows and acceptance cohorts are reviewed. The adapter remains
  supported; it is not currently scheduled.
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
