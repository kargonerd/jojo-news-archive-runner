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
- FT 2016 `holdout-v9` and FT 2017 `holdout-v8` formally converged on the
  previous `ft-parser/0.8.42`, but their evidence is now historical. The
  current parser is `ft-parser/0.8.44`, which removes residual `Sign in`
  paragraphs and buttons plus the FT Business School briefing CTA found in
  the 2020 audit; fresh `holdout-v11` runs for
  FT 2016--2020 are queued/running. It follows fixes for legacy
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
- Axios's current parser is `axios-parser/0.1.23`, after fixes for partner
  financial-newsletter CTAs, short quote-card attributions, malformed URL
  aliases, legacy Draft.js `Read more` headings, and the historical
  ``Sign up for the New Axios Space newsletter`` CTA. The B2 summaries show a
  current-version `holdout-v11` pass for 2018, while several other year
  summaries still carry older parser versions; those are historical evidence,
  not current convergence. A fresh `holdout-v12` run reached 800 rows for
  2017 but its content audit exposed that heading defect, so the superseded
  v12 rotations were stopped. The v13 rotations exposed the newsletter CTA
  in the 2019 audit, plus a standalone YouTube subscription CTA found in the
  2026 audit (including list-item markup); fresh zero-overlap `holdout-v16`
  runs for 2017--2026 have now been dispatched on 0.1.23. Years 2017--2023
  and 2026 have passed the 800-sample content and rotation audits with zero
  hard anomalies; 2024 and 2025 are still capturing toward 800, and 2016 is
  source-limited. Axios remains open until the remaining eligible cells are
  resolved.
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
  Caixin 2018 `holdout-v1` has now formally converged on
  `caixin-parser/0.1.10`: 800/800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, zero hard content anomalies, and all 800 audited
  extraction statuses complete. No article images were selected by this
  parser, and one non-hard review candidate remains.
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
  Caixin 2019's fresh zero-overlap `holdout-v1` has now formally converged on
  the same parser: 800/800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, zero hard content anomalies, and 4 selected images
  (one non-hard review candidate).
  Caixin 2020's fresh zero-overlap `holdout-v1` has now also formally
  converged on `caixin-parser/0.1.10`: 800/800 QA-passing rows, zero parser
  errors, zero prior/exclusion overlap, zero hard content anomalies, and 10
  selected images (one non-hard review candidate).
  Caixin 2021's fresh zero-overlap `holdout-v1` has now also formally
  converged on the same parser: 800/800 QA-passing rows, zero parser errors,
  zero prior/exclusion overlap, zero hard content anomalies, no selected
  article images, and one non-hard review candidate.
  Caixin 2022's fresh zero-overlap `holdout-v1` has now also formally
  converged on the same parser: 800/800 QA-passing rows, zero parser errors,
  zero prior/exclusion overlap, zero hard content anomalies, and 470 selected
  images (two non-hard review candidates).
  Caixin 2023's fresh zero-overlap `holdout-v1` has now also formally
  converged on the same parser: 800/800 QA-passing rows, zero parser errors,
  zero prior/exclusion overlap, zero hard content anomalies, and 816 selected
  images (two non-hard review candidates).
  Caixin 2024's fresh zero-overlap `holdout-v1` has now also formally
  converged on `caixin-parser/0.1.10`: 800/800 QA-passing rows, zero parser
  errors, zero prior/exclusion overlap, zero hard content anomalies, and 817
  selected images (two non-hard review candidates). Caixin 2025 has likewise
  formally converged at 800/800 with the same parser and gates, retaining 718
  selected images (two non-hard review candidates).
  The earlier 2010--2015 holdouts used older parser versions; fresh current
  `caixin-parser/0.1.10` rotations for all six years are now running against
  the 3,901--5,996 candidate Wayback windows (with the 2010 Common Crawl
  supplement available as an additional source). A fresh `holdout-v8` is
  replacing those superseded cohorts: all six years 2010--2015 have now
  passed both content and zero-overlap audits at 800/800 on
  `caixin-parser/0.1.10` after the Common Crawl/Wayback candidate merge.
- WSJ remains source-limited in the older 2020 cell (the current checkpoint
  has 797 complete rows after 826 evaluations); that cell is kept as a
  recorded TODO until its candidate pool is enlarged. `wsj-parser/0.8.55`
  removes the flattened `related stories` interface marker. QA revision 2
  additionally excludes Infini-News media-only shells that explicitly say
  `Article Not Supported` and `To Read the Full Story`, while retaining their
  raw captures. Current 0.8.55 zero-overlap audits have now passed for 2016,
  2018, 2019, 2021, and 2022 at 800+ rows with zero hard anomalies; the 2013
  and 2020 cells remain source-limited.
- NYT 2019 `holdout-v2` has formally converged at 800/800 on
  `nyt-parser/0.8.62`: QA 100%, zero parser errors, zero prior/exclusion
  overlap, and all 800 content-audit rows complete with zero hard anomalies.
  The audit retained 1,016 selected images and left one review candidate.
  A fresh zero-overlap `holdout-v6` for NYT 2020 has now formally converged
  on `nyt-parser/0.8.64`: 800/800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, all 800 extraction statuses complete, and zero hard
  content anomalies. The audit retained 1,852 selected images and left two
  non-hard review candidates.
  The current parser is now `nyt-parser/0.8.73`: it filters legacy
  newsgraphics sprite sheets (including the GIF flag sprite found in the 2016
  audit) and standalone `Related` recirculation markers,
  chooses the substantive body when a modern interactive contains a short
  results panel before the main prose, and removes flattened Campaign Reporter
  and Climate Forward newsletter subscription CTAs, including heading-level
  interactive CTAs and dead `Next:` controls. The 0.8.68 `holdout-v15` replay
  exposed the latter CTA in a 2025 article; the 0.8.69 `holdout-v16` replay
  then exposed the heading CTA in 2018 and dead interactive control in 2019.
  The 0.8.70 `holdout-v17` audit also exposed decorative `healthquiz-art`
  responsive images being archived as editorial media. The 0.8.72
  `holdout-v19` audit then exposed a Space and Astronomy Calendar CTA in the
  2023 interactive. Fresh zero-overlap `holdout-v20` runs for 2016--2026 have
  now been dispatched on 0.8.73, and all earlier NYT evidence remains
  historical until these audits pass.
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
  A fresh zero-overlap `holdout-v2` rotation for AP 2019 has now formally
  converged on `ap-parser/0.6.24` after the earnings-page interactive-control
  fix: 800 QA-passing rows, zero parser errors, zero prior/exclusion overlap,
  zero hard content anomalies, all 800 extraction statuses complete, and 140
  selected images (one non-hard review candidate).
  A fresh zero-overlap `holdout-v3` rotation for AP 2020 has now also formally
  converged on the same parser: 800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, zero hard content anomalies, all 800 extraction
  statuses complete, and 309 selected images (one non-hard review candidate).
  AP 2021's fresh zero-overlap `holdout-v3` has now also formally converged on
  `ap-parser/0.6.24`: 800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, zero hard content anomalies, all 800 extraction
  statuses complete, and 466 selected images (two non-hard review candidates).
  AP 2022's fresh zero-overlap `holdout-v3` has now also formally converged on
  the same parser: 800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, zero hard content anomalies, all 800 extraction
  statuses complete, and 455 selected images (two non-hard review candidates).
  AP 2023's fresh zero-overlap `holdout-v4` has now formally converged on
  `ap-parser/0.6.25`: 800/800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, zero hard content anomalies, all 800 extraction
  statuses complete, and 2,132 selected images (eight non-hard review
  candidates).
  AP 2024's fresh zero-overlap `holdout-v4` has now formally converged on
  `ap-parser/0.6.25`: 800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, zero hard content anomalies, all 800 extraction
  statuses complete, and 2,313 selected images (six non-hard review
  candidates).
  Fresh zero-overlap `holdout-v4` rotations for AP 2019 and AP 2022 have now
  also formally converged on `ap-parser/0.6.25`: each reached 800/800 QA,
  zero parser errors and overlaps, zero hard content anomalies, and 800
  complete extraction statuses; the audits retained 189 and 477 selected
  images respectively (one and two non-hard review candidates). AP 2025's
  `holdout-v4` has likewise converged with 1,830 selected images and two
  non-hard review candidates.
  The same fresh `holdout-v4` gate has now formally converged for AP 2017,
  2018, and 2021 on `ap-parser/0.6.25`: each reached 800/800 QA with zero
  parser errors, overlaps, or hard content anomalies; their audits retained
  12, 57, and 496 selected images respectively (one, one, and two non-hard
  review candidates).
  AP 2013--2016 have also passed fresh `holdout-v4` on `ap-parser/0.6.25`:
  each reached 800/800 with zero parser errors, overlaps, or hard content
  anomalies, retaining 32, 46, 53, and 19 selected images respectively.
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
  The fresh 2018 `holdout-v1` rotation has now also formally converged on
  `aljazeera-parser/0.1.5`: 800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, zero hard content anomalies, and 800 complete
  extraction statuses. Its content audit retained 1,299 selected images and
  left two non-hard review candidates; one additional source candidate was
  unsupported and was not part of the 800-row formal sample.
  The fresh 2016 `holdout-v1` rotation has now also formally converged on the
  same parser: 800 QA-passing rows after 810 evaluations, zero parser errors,
  zero prior/exclusion overlap, zero hard content anomalies, and 800 audited
  complete extraction statuses. Its content audit retained 1,503 selected
  images and left two non-hard review candidates.
  QA revision 2 screens short, unrecoverable dynamic LiveBlog shells as
  non-article records while retaining their raw captures. The current-version
  0.1.5 content audits now cover every 2016--2026 year: each reached the
  formal 800-row target with zero hard anomalies and zero parser errors. The
  2016 audit has 798 complete and 2 partial rows; 2023 has 799 complete and
  1 partial row, while the other years have 800 complete rows. Earlier
  pre-0.1.5 evidence remains historical.
- NPR's current parser is now `npr-parser/0.1.40`. The v0.1.31/v0.1.32
  replays exposed legacy podcast, subscription-network, and newsletter CTAs;
  later audits also exposed long podcast/challenge CTAs and legacy `Read more`
  links. The parser removes all of these with regression fixtures. Fresh
  zero-overlap `holdout-v23` rotations are dispatched for 2010--2026 against
  the current parser; all earlier NPR results remain historical until these
  current-version audits pass. The 2019 v23 checkpoint exposed a planner-only
  zero-sample run (the source manifest had candidates but no rows were planned),
  so that year was reissued as `holdout-v24` rather than treated as a parser result.
  QA revision 1 now also screens unrecoverable short NPR audio shells from the
  text-article denominator while retaining their raw captures; the affected
  v23 years are being replayed against that policy. Because the v23 2018 plan
  exhausted at 442 accepted rows, a fresh `holdout-v24` 2018 rotation and a
  `holdout-v25` 2019 rotation were also planner-only zero-sample runs despite
  successful workflow exits. The v26 rotations were likewise planner-only:
  parsed page dates moved samples out of their catalog years. Validation now
  keeps the catalog year unless the canonical URL encodes a stable year;
  fresh zero-overlap `holdout-v27` rotations for 2018 and 2019 were dispatched
  from the fixed runner; the resulting remaining disjoint pools currently
  yield only 30 and 3 accepted rows respectively, so both years are now
  marked source-limited rather than being counted as parser convergence.
  The fresh `holdout-v30` rotations exposed one short NPR newsletter CTA in
  2020; 0.1.39 removed that legacy `subscribe to our newsletter` form. The
  fresh `holdout-v31` rotations then exposed an excerpt copyright tail in
  2013; 0.1.40 removes that exact notice. Fresh zero-overlap `holdout-v32`
  rotations for 2010--2026 are now running against the fixed parser.
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
  2010's fresh zero-overlap `holdout-v17` has now formally converged on
  `npr-parser/0.1.27`: 800/800 QA-passing rows, zero parser errors, zero
  prior/exclusion overlap, zero hard content anomalies, all 800 extraction
  statuses complete, and 748 selected images (two non-hard review candidates).
  The Common Crawl supplement exposed 12,931 eligible candidates for that
  cohort.
- Nikkei's Common Crawl supplement now exposes enough dated candidates for
  2012--2015 (909, 1,055, 915, and 1,085 respectively), and the merged
  2016--2019 windows also have sufficient coverage. Current
  `nikkei-parser/0.1.7` zero-overlap `holdout-v3` runs are dispatched for
  2012--2019; 2020 has only two dated candidates and 2021--2026 have none,
  so those cells are source-limited rather than parser failures. The current
  v3 audits for 2017--2019 have now formally passed 800/800 with zero hard
  anomalies; the 2012--2016 runs remain capture-source limited despite their
  catalog candidate counts. Lianhe Zaobao
  current-version 2016, 2018--2024 have now passed their 800-row content
  audits; 2017 and 2025--2026 remain in progress or source-limited.
- Lianhe Zaobao's 2017 validation exposed four genuine short news briefs and
  embedded site controls in earlier parser versions. `zaobao-parser/0.1.5`
  addressed those cases, while a current holdout replay then exposed legacy
  Drupal pages whose body is stored under `#article-content` with a visible
  Chinese date. `zaobao-parser/0.1.6` now selects that body, parses the local
  date, and keeps the control cleanup; the affected samples are complete in
  local regression fixtures. The interrupted `holdout-v1` reached 158
  evaluated rows before the fix and is not convergence evidence; a fresh
  zero-overlap `holdout-v2` has now formally converged on
  `zaobao-parser/0.1.6`: 800 QA-passing rows after 804 evaluations, zero
  parser errors, zero prior/exclusion overlap, zero hard content anomalies,
  and 800 audited complete extraction statuses. Its content audit retained
  1,726 selected images and left two non-hard review candidates.
  A 2020 audit then found a legitimate 28-character Reuters wire brief just
  below the old 60-character floor. `zaobao-parser/0.1.7` lowers only the
  ordinary-article floor to 20 characters with a regression fixture. A 2016
  audit then exposed the legacy `#article_content .a_body` body wrapper;
  `zaobao-parser/0.1.8` now selects it. The fresh zero-overlap `holdout-v3`
  2016 audit formally reached 800 audited complete rows with zero hard
  anomalies. The fresh `holdout-v5` audits for 2019--2024 also reached 800
  complete rows with zero hard anomalies; the remaining cells continue
  running or are source-limited.
- SCMP 2017's first validation probe was source-limited: the current Wayback
  URL-key shard initially exposed only 32 candidates, and all captured pages
  identified as 1995 articles rather than 2017 publications. The Common Crawl
  supplement now exposes 5,298 dated 2017 candidates (and over 43,000 across
  2016--2026). The first broad replay found a parser defect in legacy Drupal
  pages: complete prose lived under `.pane-node-body .pane-content` but was
  not selected, leaving only 50--90 character summaries. `scmp-parser/0.1.2`
  fixes that selector. A later audit exposed a legacy SCMP `bookmark-icon.png`
  sharing control being selected as editorial media; `scmp-parser/0.1.3` now
  filters those legacy sharing/print controls at both metadata and body-image
  stages. The 0.1.3 `holdout-v3` reached 800 audited clean rows, but evaluated
  1,042 candidates because 238 were unsupported, leaving aggregate QA at 76.8%
  and the readiness gate closed. Review of those rows found explicit SCMP
  access shells such as `READ FULL ARTICLE` with no recoverable body. The next
  audit also identified image-only `/infographics/` and `-gallery` pages; QA
  revision 2 screens all three source-limited non-article forms while retaining
  their raw captures. A sampled 2016 capture then exposed a second parser
  defect: legacy Vue pages can retain the full article only in
  `window.__APOLLO_STATE__`, while the DOM article node is empty.
  `scmp-parser/0.1.6` now renders that structured body and restores its
  Apollo inline images (with ads and related chrome excluded). A 2021 audit
  then confirmed Apollo-only image slideshow/newsletter packages with
  `displaySlideShow=true` and no prose; QA revision 3 screens those media-only
  packages while retaining their raw captures. Fresh zero-overlap `holdout-v7`
  replays for 2016--2022 are dispatched against the revised policy. The v7
  content audits for 2016--2020 have each reached 800 complete rows with zero
  hard anomalies; 2017 is now included, while 2021--2022
  remain source-limited because the unexcluded exact-capture pools are too
  small. Earlier v6 evidence remains historical for those cells.
  The 2010--2015 source shard currently exposes
  fewer than 800 dated candidates per year, and 2023+ remains source-limited
  pending additional catalog coverage.
- Nikkei's first 2017 validation reached 800 QA rows but its content audit
  found three embedded `form`/`input`/`button` controls. The parser now removes
  those site-wide controls as `nikkei-parser/0.1.7`; the fresh zero-overlap
  `holdout-v1` has formally converged at 800/800 with QA 100%, zero parser
  errors, zero prior or exclusion overlap, zero hard content anomalies, all
  extraction statuses complete, and 761 selected images (15 non-hard review
  candidates). A fresh zero-overlap `holdout-v1` for 2016 has now also
  formally converged on the same parser: 800/800 QA-passing rows, zero parser
  errors, zero prior/exclusion overlap, zero hard content anomalies, all
  extraction statuses complete, and 592 selected images (16 non-hard review
  candidates). The supplement currently exposes about 6,013 dated 2017
  articles and 1,789 for 2016. The new `holdout-v3` schedule supersedes the
  incomplete 2012--2015 and 2018--2026 probes once its current-version audits
  finish.
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
