# Safety and permissions

BookGuard uses layered access, repair, and action safeguards.

### Application access

BookGuard requires HTTP Basic authentication for every page and API route
except `/health`:

```env
BOOKGUARD_AUTH_USERNAME=bookguard
BOOKGUARD_AUTH_PASSWORD=replace-with-a-long-random-password
```

Because browsers resend Basic credentials automatically, every state-changing
request (anything other than `GET`/`HEAD`/`OPTIONS`) is also rejected with
`403` when the browser marks it cross-site, when its `Origin` does not match the
request host, or when it has a body that is not `application/json`. Scripts that
call the API with `curl -H 'Content-Type: application/json'` are unaffected. If
BookGuard sits behind a reverse proxy that rewrites the `Host` header, list the
public origin(s) the browser uses:

```env
BOOKGUARD_TRUSTED_ORIGINS=https://bookguard.example.com
```

Compose requires a password before deployment; protected routes refuse access
when the application has no configured password. The supplied Compose file
also binds to localhost by default:

```env
BOOKGUARD_BIND_ADDRESS=127.0.0.1
```

For direct LAN access, set that value to the Unraid server's LAN address. Basic
authentication controls access but does not encrypt traffic. Keep plain HTTP on
a trusted LAN; use an HTTPS reverse proxy or VPN for remote access, and do not
publish BookGuard directly to the internet.

Operations such as repair, undo, scan, reset, detach, quarantine, remediation,
and reconciliation require their operation-specific confirmation
where defined by the route. Read the request model for the particular endpoint;
individual verification requests, for example, do not use a confirmation token.
Authentication and operation-specific confirmation serve different purposes.

### Bindery actions

```env
BOOKGUARD_ALLOW_ACTIONS=false
```

This controls Bindery changes and guarded operations such as Detach, Quarantine,
Replace and Put back. Metadata repair has its own
repair-mode and writable-mount checks. `PASS` results remain protected
from destructive actions.

### Metadata repair

```env
BOOKGUARD_METADATA_REPAIR_MODE=preview
```

The default `preview` mode builds repair proposals but never writes media. `safe` mode permits only repairs that pass BookGuard's conservative identity rules, and only if the relevant media mount is writable.

The supplied Compose file deliberately mounts ebooks and audiobooks read-only:

```yaml
- /mnt/user/data/media/audiobooks:/audiobooks:ro
- /mnt/user/data/media/books:/books:ro
```

So even if Safe mode is selected accidentally, Docker still blocks writes. Remove `:ro` only when you deliberately want metadata repair to modify that library.

Guarded ebook quarantine and wrong-content remediation do not require making
`/books` writable. They use a separate, independently gated alias:

```env
BOOKGUARD_EBOOK_ACTIONS_ENABLED=false
BOOKGUARD_EBOOK_ACTION_ROOT=/action-books
```

To switch ebook quarantine on, set `BOOKGUARD_EBOOK_ACTIONS_ENABLED=true` in
`.env` and run the usual upgrade command. `scripts/safer-upgrade.sh` then adds
`compose.actions.yaml` (the writable alias) by itself, on this and every later
upgrade, and its hardening check accepts the alias only while ebook actions are
on and the alias is the same host folder as the read-only `/books` mount.
System → Health says whether Quarantine, Replace and Put back are ready, and if
not, the one thing to do. The read-only source path and writable alias must have
identical library-relative paths and must resolve to the same device and inode
before BookGuard will move anything. A failed move occurs before Bindery is
changed; a later detach failure causes BookGuard to restore the file through the
writable alias.

## Metadata repair philosophy

BookGuard distinguishes **bad metadata** from **wrong content**.

A safe repair candidate might look like:

```text
Bindery:  Bel Canto — Ann Patchett
Embedded title:  Patchett, Ann
Embedded author: Bel Canto
```

BookGuard can recognize the swapped fields, preview the correction, write the canonical title/author, verify the result, record the before/after values, and allow Undo.

A wrong-content import such as:

```text
Bindery: Cat of Death! — Aaron Blabey
Embedded: Death of a Texan — Cat Hickey
```

is **not** repaired. BookGuard will not relabel unrelated content to make it appear correct.

For audiobooks, BookGuard currently changes only book-level fields selected in Settings. Track/chapter Title and narrator/Artist are preserved.

## Matching philosophy

BookGuard favors `REVIEW` when evidence is weak. Automatic `REJECT` remains deliberately narrow:

- **MUSIC_MISMATCH** — audio is explicitly music-like and does not support the expected author.
- **STRONG_MISMATCH** — stable embedded title + author metadata identifies a different spoken-word work.

Generic labels such as `Chapter 01` and `Track 01` are never enough for strong semantic rejection.

Metadata repair is narrower still: only already-confirmed identities or explicitly recognized mechanical metadata faults are eligible for Safe repair.

See [configuration](configuration.md) for scanner settings and
[advanced workflows](advanced-workflows.md) for the shared-file correction gates.
