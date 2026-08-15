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
- Active convergence is being advanced explicitly by publisher/year while the
  watchdog remains disabled on the default branch. Bloomberg's completed
  cells no longer occupy validation slots; the branch watchdog now admits all
  remaining in-scope publishers and filters each year by source capacity.
- A cell counts as converged only when its 800-row summary and parser-bound
  content audit both pass, plus the zero-overlap rotation audit for holdouts.
  Failed content audits keep their checkpoint, raw HTML, and audit evidence in
  B2 but are quarantined from automatic retries until the parser or QA policy
  changes.
- FT 2016 `holdout-v9` and FT 2017 `holdout-v8` have formally converged on
  `ft-parser/0.8.42`. It follows fixes for legacy
  podcast RSS chrome, a flattened JSON-LD related-story tail, dead
  expander/video controls, an AMP ``Read more`` link group, and FT brand
  favicons and v3 open-graph branding found during partial content audits.
  All superseded FT cohorts remain mandatory exclusions. The 2016 holdout
  finished at 800/800 with QA 100%, zero parser errors, zero prior/exclusion
  overlap, zero hard content anomalies, and all 800 extraction statuses
  complete; its content audit retained 905 selected images. The 2017 audit
  retained 794 selected images. The parser fixes covered legacy podcast RSS
  chrome, a flattened JSON-LD related-story tail, dead expander/video
  controls, an AMP ``Read more`` link group, and FT brand favicons and v3
  open-graph branding found during partial content audits.
- Axios 2018, 2019, and 2022--2025 have formally converged at 800/800 on
  `axios-parser/0.1.19`
  and QA revision 4 after removal of a partner financial-newsletter call to
  action and recovery of publisher-authored short quote-card attributions.
  The 2018 cohort excluded 3,999 previously evaluated URLs, had zero
  prior-cohort overlap and zero hard content anomalies, and preserved 871
  selected images during the final reparse audit. The 2019 holdout-v4 excluded
  3,189 previously evaluated URLs, likewise had zero overlap and zero hard
  anomalies, and preserved all 930 selected images. The 2022 holdout-v4
  excluded 3,191 previously evaluated URLs, had zero overlap and zero hard
  anomalies, and preserved all 949 selected images. The 2023 holdout-v1
  excluded 768 normalized unique URLs, had zero overlap and zero hard
  anomalies, and preserved all 1,018 selected images. The 2024 holdout-v1
  excluded 763 normalized unique URLs, had zero overlap and zero hard
  anomalies, and preserved all 1,006 selected images. The 2025 holdout-v1
  excluded 705 normalized unique URLs, had zero overlap and zero hard
  anomalies, and preserved all 953 selected images. Axios has no remaining
  year in the current replay queue.
  The previous 2017 cohort contained two confirmed CMS fixtures;
  the previous 2022--2025
  cohorts contained respectively 1, 34, 51, and 110 malformed trailing URL
  aliases. Manifest ingestion and sample planning now collapse or skip those
  aliases. Axios 2017, 2020, and 2021 now have formal current-version
  evidence: 800/800, zero overlap, zero hard content anomalies, and all
  selected images preserved. The 2017 cohort excluded 6,644 previously
  evaluated URLs and preserved 701 selected images.
- Caixin 2013 holdout-v1 has formally converged at 800/800 on
  `caixin-parser/0.1.9` with QA revision 1, zero prior-cohort overlap, zero
  hard content anomalies, and all 228 selected images preserved. The parser
  removes the legacy Two Sessions topic-recirculation tail exposed by the
  initial 751-article audit. Caixin 2014 holdout-v1 has also formally
  converged at 800/800 with zero prior-cohort overlap, zero hard content
  anomalies, and all 3 selected images preserved. Caixin 2015 holdout-v1 has
  now formally converged at 800/800 on the same parser and QA revision, with
  zero prior-cohort overlap, zero hard content anomalies, and 2 selected
  images preserved (one non-hard review candidate). These first holdouts had
  no earlier cohort, so their rotation audits correctly treated the prior
  union as empty. Every currently queued Caixin year has at least 3,901
  candidates. The first bounded 2016--2026 catalog
  pass found at least 1,144 candidates in every year except 2018. A focused
  Common Crawl supplement now adds 3,068 independently cataloged 2018 URLs,
  making every 2016--2026 year eligible for an initial 800-article cohort.
  Caixin 2010 had only 580 accepted current-cohort articles after exhausting
  its 940 eligible primary candidates. Its resumable Common Crawl supplement
  now prioritizes recent indexes: 40 high-yield pages found 1,220 URLs,
  including 404 new article-desk URLs absent from the primary manifest. The
  2010 and 2011 have now formally converged at 800/800 with zero
  prior-cohort overlap and zero hard content anomalies. The 2010 run also
  screened 41 photo/video desk pages outside the article target; the merged
  pool exposed 1,708 eligible candidates. The 2011 run had 2,301 eligible
  candidates. Caixin 2012 has also formally converged at 800/800 with zero
  parser errors and zero hard content anomalies; all 29 selected images were
  preserved. Keep completing both broader resumable catalogs for future
  zero-overlap rotations. After the separator fix, Caixin 2017 has now also
  passed the current `caixin-parser/0.1.10` validation at 800/800 with zero
  parser errors, zero hard content anomalies, and 22 selected images.
- Paused after an already-started WSJ 2020 holdout exposed poor source yield
  (17 accepted samples after 305 capture failures). TODO: enlarge and audit the
  replay candidate pool before resuming that cell; do not expand into another
  WSJ year automatically.
- NYT 2019 `holdout-v2` has formally converged at 800/800 on
  `nyt-parser/0.8.62`: QA 100%, zero parser errors, zero prior/exclusion
  overlap, and all 800 content-audit rows complete with zero hard anomalies.
  The audit retained 1,016 selected images and left one review candidate.
  NYT 2018 has now completed the fresh zero-overlap `holdout-v11` on
  `nyt-parser/0.8.64`: 800/800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, and all 800 extraction statuses complete with zero
  hard content anomalies. The final audit retained 1,023 selected images and
  left one non-hard review candidate. This supersedes the earlier 2018
  `holdout-v9` evidence after the interactive-sprite and Campaign Reporter
  content-audit fixes.
  The older NYT 2018 `holdout-v7` reached 800/800 on `nyt-parser/0.8.58`
  without the current content-audit gate. The `holdout-v8` replay reached
  800 QA-passing rows after evaluating 801, but one archived `/admin/` teaser
  prevented the 100% QA gate. QA revision 2 now screens such unrecoverable
  teasers as `nonarticle-desk`. The fresh `holdout-v9` has now converged at
  800/800 under that policy: zero parser errors, zero prior/exclusion overlap,
  zero hard content anomalies, and all 800 extraction statuses complete; its
  content audit retained 962 selected images.
- AP 2012 `holdout-v1` has formally converged at 800/800 on
  `ap-parser/0.6.21`: QA 100%, zero parser errors, zero prior/exclusion
  overlap, zero hard content anomalies, and all 800 extraction statuses
  complete. Its content audit retained 383 selected images and two review
  candidates. AP 2013 has now also formally converged at 800/800 with QA
  100%, zero parser errors, zero prior/exclusion overlap, zero hard content
  anomalies, and all 800 extraction statuses complete; its content audit
  retained 13 selected images. AP 2014 has now formally converged at 800/800
  on the same parser and sitemap shard, with QA 100%, zero parser errors,
  zero prior/exclusion overlap, zero hard content anomalies, and all 800
  extraction statuses complete; its content audit retained 41 selected
  images. AP 2015 has now formally converged at 800/800 on the same parser
  and sitemap shard, with QA 100%, zero parser errors, zero prior/exclusion
  overlap, zero hard content anomalies, and all 800 extraction statuses
  complete; its content audit retained 91 selected images and left one
  non-hard review candidate. The AP 2010 catalog
  currently exposes fewer than 800 distinct candidates; 2012 and later years
  have materially larger pools and are being validated first. AP 2016 has now
  completed the current `ap-parser/0.6.21` validation at 800/800 QA-passing
  rows, with zero parser errors, all 800 extraction statuses complete, zero
  hard content anomalies, and 17 selected images (one non-hard review
  candidate). AP 2017 has now also formally converged at 800/800 on the same
  parser: QA 100%, zero parser errors, zero prior/exclusion overlap, zero hard
  content anomalies, all 800 extraction statuses complete, and 62 selected
  images (one non-hard review candidate); this is historical evidence for
  `ap-parser/0.6.21`. AP 2016 has now also formally converged on
  `ap-parser/0.6.22` at 800/800: QA 100%, zero parser errors,
  zero prior/exclusion overlap, zero hard content anomalies, all extraction
  statuses complete, and 21 selected images (one non-hard review candidate).
  A fresh audit of AP 2017 on `ap-parser/0.6.22` has now also formally
  converged at 800/800: QA 100%, zero parser errors, zero prior/exclusion
  overlap, zero hard content anomalies, all extraction statuses complete, and
  77 selected images (one non-hard review candidate).
  AP 2018's first content audit found
  one legacy inline `RELATED` interface marker. The parser now removes that
  marker as `ap-parser/0.6.22`; fresh zero-overlap `holdout-v1` evidence has
  formally converged at 800/800: QA 100%, zero parser errors, zero prior or
  exclusion overlap, zero hard content anomalies, all extraction statuses
  complete, and 56 selected images (two non-hard review candidates).
- Al Jazeera 2019 `validation` has formally converged at 800/800 on
  `aljazeera-parser/0.1.2`: QA 100%, zero parser errors, all 800 extraction
  statuses complete, zero hard content anomalies, and 1,199 selected images.
  That is retained as historical evidence for the pre-0.1.3 parser. A fresh
  `holdout-v1` for 2020 formally converged on `aljazeera-parser/0.1.3` with
  800/800 QA-passing rows, zero parser errors, zero prior/exclusion overlap,
  zero hard content anomalies, 800 complete extraction statuses, and 1,482
  selected images. The 0.1.3 fix removes underscore-only visual separators
  from legacy live-update pages; the 2019 cell must be re-rotated on this
  current parser before it can be considered current-version evidence. A
  follow-up `0.1.4` fix recognizes image-only Al Jazeera gallery snapshots;
  a further `0.1.5` fix also handles legacy gallery shells and heading-only
  live-update separators. The fresh `holdout-v3` rotation for 2019 has now
  formally converged on `0.1.5` at 800/800 with zero parser errors, zero hard
  content anomalies, zero prior overlap, and 1,238 selected images. The
  2017/2020 v3 runs reached 800 QA-passing rows but retained non-clean state
  records, so they are not formal evidence. The fresh `holdout-v4` rotation
  for 2017 has now also formally converged at 800/800, with zero prior
  overlap, zero hard anomalies, and 1,311 selected images. The fresh 2020
  `holdout-v5` rotation has now formally converged on `0.1.5` at 800/800,
  with zero parser errors, zero prior/exclusion overlap, zero hard content
  anomalies, and all 800 extraction statuses complete; its content audit
  retained 1,421 selected images and left two non-hard review candidates.
  All earlier Al Jazeera results remain historical until their current-version
  rotations pass every gate.
- TODO: continue NPR and AP across their remaining eligible years after the
  current holdouts establish the next parser baselines. Their existing
  catalogs and checkpoints remain resumable.
- NPR 2012's fresh zero-overlap `holdout-v14` has now formally converged at
  800/800 on `npr-parser/0.1.26`: QA 100%, zero parser errors, zero prior or
  exclusion overlap, zero hard content anomalies, all 800 extraction statuses
  complete, and 1,276 selected images (two non-hard review candidates). The
  previous failed audit was caused by one Wayback tracking suffix embedded in
  a stored path; manifest import and holdout selection now normalize/reject
  these aliases, with regression tests. NPR 2011's next audit also exposed a
  legacy `Read More` header and one old URL alias; the parser now removes the
  header as `npr-parser/0.1.27`. The fresh zero-overlap `holdout-v15` has now
  formally converged at 800/800: QA 100%, zero parser errors, zero prior or
  exclusion overlap, zero hard content anomalies, all extraction statuses
  complete, and 1,108 selected images (two non-hard review candidates). NPR
  2010 remains source-limited in its parser holdouts; the Common Crawl
  supplement now exposes about 7,485 dated candidates and continues to grow.
- TODO: then resume Nikkei, Lianhe Zaobao, Al Jazeera, and South China Morning
  Post. Their existing catalogs and checkpoints also remain resumable.
- Lianhe Zaobao's 2017 validation exposed four genuine short news briefs and
  embedded site controls in earlier parser versions. `zaobao-parser/0.1.5`
  addressed those cases, while a current holdout replay then exposed legacy
  Drupal pages whose body is stored under `#article-content` with a visible
  Chinese date. `zaobao-parser/0.1.6` now selects that body, parses the local
  date, and keeps the control cleanup; the affected samples are complete in
  local regression fixtures. The interrupted `holdout-v1` reached 158
  evaluated rows before the fix and is not convergence evidence; a fresh
  zero-overlap `holdout-v2` is required.
- SCMP 2017's first validation probe was source-limited: the current Wayback
  URL-key shard initially exposed only 32 candidates, and all captured pages
  identified as 1995 articles rather than 2017 publications. The expanded
  Wayback catalog now has 82 articles (49 in 2017, 32 in 2018, and 1 in
  2019), while the Common Crawl supplement is still being advanced; no SCMP
  800-row parser gate has passed.
- Nikkei's first 2017 validation reached 800 QA rows but its content audit
  found three embedded `form`/`input`/`button` controls. The parser now removes
  those site-wide controls as `nikkei-parser/0.1.7`; the fresh zero-overlap
  `holdout-v1` has formally converged at 800/800 with QA 100%, zero parser
  errors, zero prior or exclusion overlap, zero hard content anomalies, all
  extraction statuses complete, and 761 selected images (15 non-hard review
  candidates). The supplement currently exposes about 6,013 dated 2017
  articles and 1,789 for 2016.
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
