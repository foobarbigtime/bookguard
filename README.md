# BookGuard

**BookGuard** is a conservative validation and quarantine companion for [Bindery](https://github.com/vavallee/bindery).

It answers a question that a normal download/import pipeline often cannot:

> Does the file Bindery imported actually look like the book Bindery thinks it is?

BookGuard is designed around **audit first, destructive actions second**. By default it is read-only.

## What v0.3 does

- Reads Bindery's `book_files`, books, and authors from the Bindery SQLite database in **read-only mode**.
- Audits audiobook associations with `ffprobe` and samples representative files across an audiobook folder.
- Compares embedded album/title and artist/author information to Bindery's expected title and author.
- Detects obvious music-like imports when they do not match the expected book.
- Detects strong spoken-word semantic mismatches when stable embedded title + author metadata identifies another work.
- Supports configurable author aliases/pen names to protect legitimate cases such as Stephen King / Richard Bachman and J.K. Rowling / Robert Galbraith.
- Audits EPUB, PDF, MOBI, AZW/AZW3, CBZ ComicInfo.xml, RTF metadata, and explicit Title:/Author: headers in TXT files.
- Leaves unsupported ebook formats as `REVIEW / UNSUPPORTED` instead of treating them as bad.
- Finds tracked Bindery paths that no longer exist.
- Assigns each result a state: `PASS`, `REVIEW`, `REJECT`, or `MISSING`.
- Adds reason groups such as `MATCH`, `MISMATCH`, `PARTIAL_MATCH`, `NO_METADATA`, `UNSUPPORTED`, `MUSIC_MISMATCH`, and `STRONG_MISMATCH`.
- Shows expected and detected title/author side by side on the dashboard.
- Stores scan history and persistent settings in `/config/bookguard.db`.
- Provides a live web dashboard on port `8788` with progress, counts, elapsed time, ETA, incoming results, and a REVIEW breakdown.
- Provides a Settings page for Bindery connection details, path mapping, scan behavior, matching rules, aliases, dashboard behavior, music detection, strong mismatch detection, and safety controls.
- Can optionally detach a tracked path using Bindery's API.
- Can optionally detach and move a single, non-shared tracked path into quarantine.

BookGuard **never automatically deletes media**.

## Safety model

The default is:

```env
BOOKGUARD_ALLOW_ACTIONS=false
```

In that mode BookGuard only reads the Bindery database, audiobook files, and ebook files. The supplied Compose file mounts those sources read-only.

If actions are enabled, BookGuard still refuses to detach or quarantine a result classified as `PASS`. Quarantine also refuses to move a path when more than one Bindery `book_files` association lives under that tracked path.

### Detach vs quarantine

**Detach** calls Bindery's path-specific endpoint:

```text
DELETE /api/v1/book/{book_id}/file?path=<stored path>
```

It does not intentionally delete the physical file.

**Quarantine** first performs that safe path-specific detach and then moves the physical path into `/quarantine`.

Because the example media mounts are read-only, quarantine will not work until you deliberately change the relevant media mount from `:ro` to read/write.

## Unraid quick start

Clone the repository:

```bash
cd /mnt/cache/appdata
git clone git@github.com:foobarbigtime/bookguard.git
cd bookguard
cp .env.example .env
```

For audit-only use, you can leave the API key blank. The example uses Unraid's host gateway to reach Bindery, so BookGuard does not need to share Bindery's Docker network:

```env
BINDERY_URL=http://host.docker.internal:8787
BINDERY_API_KEY=
BOOKGUARD_ALLOW_ACTIONS=false
```

Then build and run:

```bash
docker compose up -d --build
```

Open:

```text
http://<unraid-ip>:8788
```

The supplied Compose file uses these host paths:

```text
/mnt/cache/appdata/bindery
/mnt/user/data/media/audiobooks
/mnt/user/data/media/books
/mnt/cache/appdata/bookguard
/mnt/user/data/bookguard-quarantine
```

Adjust `compose.yaml` if your layout differs.

## Settings

Most BookGuard behavior can be changed at `/settings` without editing `.env` or recreating the container. UI settings are persisted in `/config/bookguard.db` and override environment defaults.

Configurable areas include:

- Bindery URL, database path, and API key
- audiobook/ebook path mapping and quarantine path
- scan scope, sample count, scan-on-start, and progress cadence
- title matching and optional surname-only author matching
- author alias / pen-name groups
- strong spoken-word mismatch rejection, evidence count, and consensus threshold
- music rejection behavior and music genre list
- live dashboard polling and result limits
- action enable/disable safety control

Docker bind mounts remain deployment-level settings. Changing a container path in BookGuard does not create a Docker mount automatically.

## Updating

From the repository directory:

```bash
git pull
docker compose up -d --build
```

After a scanner upgrade, run a **new scan**. Historical scan rows remain intact and are not silently reclassified.

## Enabling detach actions

When you are satisfied with BookGuard's classifications, configure the Bindery API key and enable actions either in the Settings page or through environment defaults:

```env
BINDERY_API_KEY=your_key_here
BOOKGUARD_ALLOW_ACTIONS=true
```

The media mounts can remain read-only if you only want the **Detach** button.

## Enabling quarantine

Quarantine physically moves media. Keep it disabled until you trust the scanner.

To use it, actions must be enabled and the appropriate media mount must be writable. For example:

```yaml
- /mnt/user/data/media/audiobooks:/audiobooks
```

rather than:

```yaml
- /mnt/user/data/media/audiobooks:/audiobooks:ro
```

BookGuard does not enable writable mounts for you.

## Matching philosophy

BookGuard favors `REVIEW` when the available evidence is weak, but it can use two deliberately narrow `REJECT` paths:

- **MUSIC_MISMATCH** — audio is music-like and supports neither the expected title nor author.
- **STRONG_MISMATCH** — non-music audio has explicit, stable title + author metadata that identifies another work, with no support for the expected title, expected author, or configured author aliases.

Generic audio labels such as `Chapter 01` and `Track 01` are not enough to trigger strong semantic rejection. Blank or incomplete metadata remains `REVIEW`.

Ebook mismatches remain `REVIEW` in v0.3 rather than automatically becoming `REJECT`; this is intentional while real-library alias and publisher metadata behavior is still being validated.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
PYTHONPATH=. pytest -q
uvicorn app.main:app --reload --port 8788
```

## Status

This is an early safety-first build. Run it in audit-only mode first and compare its findings with known-good and known-bad imports before enabling actions.
