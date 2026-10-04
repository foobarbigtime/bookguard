# Configuration

Most operator settings can be changed at `/settings` and are persisted in
`/config/bookguard.db`. Authentication, the scanner endpoint, automation mode,
and the ebook action alias also have deployment-only settings.
Use [`.env.example`](../.env.example) and [Compose](../compose.yaml) for the
deployment defaults, and the [workflow reference](advanced-workflows.md) for
Observe mode.

Persisted settings are loaded at startup over ordinary environment defaults.
Changing an environment default does not override an already saved UI setting;
change that setting in the UI or use its reset control. Deployment-only settings
are read from the environment independently.

Verification and security settings include the following checks. File-signature,
archive, EPUB-structure, and PDF-integrity checks are on by default; ClamAV is
optional and off by default:

- the file signature must match the ebook extension when the format has a fixed signature
- EPUB/CBZ archives reject traversal paths, symlinks, encryption, duplicate or case-colliding names, excessive member counts and sizes, and dangerous expansion ratios
- EPUB files must pass ZIP CRC, mimetype, container, package, manifest, and spine validation
- PDF files must have a valid header, cross-reference structure, page tree, and EOF marker
- optional ClamAV malware scanning can require a clean result before identity verification

The ClamAV check is disabled by default. When enabled, BookGuard streams the file bytes to a
deployment-configured `clamd` endpoint with the INSTREAM protocol; it does not give ClamAV a
host/library path. A malware detection, scanner error, timeout, missing scanner configuration, or
file above the configured scan limit fails closed: the file cannot be verified or admitted.
BookGuard does not send a file to ClamAV after an earlier deterministic safety check has already
failed, which avoids handing malformed or archive-bomb-like input to another parser.

Only a detection is evidence that a file is unsafe. When the scanner cannot finish (a timeout,
dropped connection, clamd error, or size limit), the library result is recorded as
`INSUFFICIENT_EVIDENCE` with source `malware-scan-inconclusive`, never as `UNSAFE_FILE`. It is
not reused from the verification cache, so the next verification scans the file again, and
Observe proposes re-verification rather than quarantine. `UNSAFE_FILE` results saved by earlier
releases for scanner failures are recognised and treated the same way.

The scan timeout defaults to 300 seconds (`BOOKGUARD_MALWARE_SCAN_TIMEOUT_SECONDS`, 1–300);
heavily illustrated EPUBs can take several minutes in clamd. The ClamAV overlay raises clamd's
100 MiB `StreamMaxLength`/`MaxFileSize` defaults to 512 MiB and `MaxScanSize` to 1024 MiB to
match `BOOKGUARD_MALWARE_MAX_BYTES`; override them with `BOOKGUARD_CLAMD_STREAM_MAX_LENGTH`,
`BOOKGUARD_CLAMD_MAX_FILE_SIZE` and `BOOKGUARD_CLAMD_MAX_SCAN_SIZE`. clamd reads them at start,
so recreate the scanner (`docker compose ... up -d`) after changing them.

For the optional private ClamAV topology, start BookGuard with the malware
overlay. The scanner has no library mounts and port 3310 is not published to
the host:

```bash
bash scripts/build-with-provenance.sh
docker compose -f compose.yaml -f compose.clamav.yaml up -d --no-build
```

On first start ClamAV may need time to initialize its signature database. Once
the containers are up, run the live acceptance test:

```bash
docker compose -f compose.yaml -f compose.clamav.yaml \
  exec -T bookguard python -m tools.clamav_acceptance
```

The acceptance test creates temporary clean and EICAR antivirus-test files
inside the BookGuard container, proves the clean control is accepted, proves
ClamAV detects EICAR, and proves BookGuard converts that detection into a
fail-closed security result. The temporary files are deleted automatically and
no test file is written to the ebook library, staging, or quarantine paths.

A deterministic failure produces an `UNSAFE_FILE` verdict before content identity is evaluated.
Unsafe files are never treated as the right book. Plain-text and legacy formats
without a reliable fixed signature are reported as not applicable instead of being guessed.
An EPUB whose valid `mimetype` entry is merely compressed, not first, or padded with a UTF-8
BOM or ASCII whitespace receives a visible conformance warning rather than an unsafe verdict;
structural and identity verification continue.

Metadata repair settings include:

- repair mode: Off / Preview only / Safe repairs enabled
- audiobook and ebook repair enable/disable
- canonical normalization for confirmed PASS items
- set audiobook Album from Bindery title
- set audiobook Album Artist from Bindery author
- optional audiobook Genre and configurable genre value

Repair history and Undo are available in Activity and at `/repairs`.

See [safety and permissions](safety.md) before enabling repairs or actions.
