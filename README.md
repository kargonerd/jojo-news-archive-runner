# JOJO News Archive Runner

Open-source research tooling for building reproducible, resumable news archives
from publicly indexed web snapshots.

This temporary runner repository supports the nonprofit JOJO Platform research
project while the main platform is being prepared for open-source release. It
currently runs a bounded Bloomberg 2020 archive job on GitHub Actions.

## What is public

- Downloader and extraction source code
- Tests and GitHub Actions workflows
- Snapshot URL manifest and archive metadata

## What stays private

Downloaded HTML, extracted article bodies, images, and SQLite checkpoints are
written to a B2 bucket that the workflow verifies is private. Downloaded content
is never committed to Git and is not uploaded as a GitHub Actions artifact.

## How the continuous job works

1. Restore the latest checkpoint from B2.
2. Process a bounded batch with a fixed runtime limit.
3. Upload content-addressed archive objects.
4. Upload the SQLite checkpoint last.
5. Dispatch the next bounded batch.

A six-hour schedule acts as a watchdog, and a concurrency lock prevents two
workers from writing the same archive at once.

See [the Bloomberg Actions runbook](services/olds-api/BLOOMBERG_ACTIONS.md) for
configuration and operating details.

## License and content notice

The software in this repository is licensed under the MIT License. Third-party
news content is not distributed by this repository and remains subject to the
rights and terms of its original publishers and archive providers. Users are
responsible for ensuring that their use is authorized and lawful.