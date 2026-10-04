# Using BookGuard

BookGuard records what it finds, explains the evidence, and asks for a decision
when it cannot prove a safe outcome. Start with Home and Review. Advanced
replacement workflows have their own prerequisites and confirmations.

## The main pages

| Page | What you do there |
|---|---|
| Home (`/`) | Check health, see grouped problems, start a scan, and read recent activity |
| Review (`/review`) | Filter open REVIEW/REJECT items and inspect a book's evidence and actions |
| Activity (`/activity`) | Follow operations and their outcomes, inspect records, undo eligible repairs, or put back eligible quarantines |
| System (`/system`) | Diagnose health problems, inspect scheduled tasks, and open Advanced diagnostics |
| Settings (`/settings`) | Change supported scan, verification, repair, and action settings |

Review also retains Triage, Repairs, Unmatched, Duplicates, and Scan results tabs
for their specialized workflows. The UI redesign is still in progress; the
[design status](design/ui-redesign.md) identifies the remaining work.

## Scan, verify, then decide

1. Run **Scan library** from Home to check existing Bindery file associations.
2. Open **Review** and choose a book. Read **What's wrong**, **How BookGuard
   knows**, and **Suggestion**.
3. Use **Verify now** for an unverified item. Verification checks content
   identity and applicable file safety, including ClamAV when enabled.
4. Choose the supported action only after reviewing its preview and confirmation.
5. Check **Activity** for the result and any follow-up that needs your attention.

Review shows 100 books per page. Filters apply before pagination, and the totals
cover all open review items. Activity links to a book locate its page
automatically. Missing tracked files and other scan classifications can be
inspected in **Scan results**; the main Review list contains REVIEW and REJECT
results that have not been resolved.

Verification can remain inconclusive. A scanner outage or an uncertain identity
is a reason to gather more evidence, not proof that the file is unsafe.

## Choosing an action

| Action | Effect |
|---|---|
| Verify now / Verify again | Gather and record identity and safety evidence |
| Mark reviewed / Keep | Record your decision in BookGuard without changing the file or Bindery |
| Review metadata fix | Preview supported title/author or audio-tag changes; applying them requires Safe repair mode and writable media |
| Move to correct book | Ask Bindery to move a proven misfiled ebook to the identified book; this does not download a new file |
| Quarantine | Move the file out of the library and remove its Bindery association after the action's checks |
| Replace | Quarantine the wrong file, then ask Bindery to search for a replacement; follow-up problems remain visible in Activity |
| Detach from Bindery | Remove the exact association and leave the physical file in place |

An unavailable action may need a deployment setting or writable alias, not just
the UI action switch. **System → Health** explains whether ebook Quarantine,
Replace, and Put back are ready. See [safety and permissions](safety.md).

## Undo and Put back

For an applied metadata repair, open its Activity entry and use its guarded
**Undo** action. Repair history is also retained in the Repairs tab.

For an eligible Triage/Review quarantine, open the quarantine's Activity record
and choose **Put back**. BookGuard checks the recorded original path and bytes,
refuses an occupied destination or another copy already assigned to the book,
restores the file, and asks Bindery to associate it with the same book again.
Review any follow-up message in Activity. Older quarantines without the required
recorded paths and hashes cannot be put back this way.

Put back is not a general undo for every automatic quarantine, cleanup, or
replacement workflow. The action appears only when that record is eligible.

## Specialized reviews

- **Unmatched** checks files from Bindery's Import page and explains whether
  they are junk, belong to a library book, are an extra copy, are another-language
  editions, are not in the library, or remain uncertain. Attach uses Bindery's
  adopt operation only for supported proven matches.
- **Duplicates** lists duplicate Bindery entries. Fix now can hide proven empty
  extras using Bindery's exclude operation. Entries with files remain unchanged;
  split ebook/audiobook entries and multiple copies are listed for your review.
  Hourly fixing is off by default and is controlled in Settings → Schedule.
- **Triage** retains the detailed shared-file panel. Use the
  [advanced workflow reference](advanced-workflows.md) for its gates and
  recovery behavior.

## Supported checks and capabilities

- Reads Bindery's `book_files`, books, and authors from the Bindery SQLite database in **read-only mode**.
- Audits audiobook associations with `ffprobe` and representative sampling across audiobook folders.
- Audits EPUB, PDF, MOBI, AZW/AZW3, CBZ ComicInfo.xml, RTF metadata, and explicit Title:/Author: headers in TXT files.
- Detects music/soundtrack imports, strong spoken-word semantic mismatches, missing tracked files, partial matches, and unsupported metadata formats.
- Supports configurable author aliases/pen names.
- Detects mechanically swapped EPUB title/author fields without loosening general matching rules.
- Shows expected and detected title/author side by side.
- Provides metadata repair modes: `off`, `preview`, and `safe`.
- Repairs confirmed EPUB title/creator metadata.
- Repairs common audiobook book-level tags with Mutagen for MP3, FLAC, M4A/M4B/MP4, OGG, and Opus while preserving track titles and narrator/Artist tags.
- Can set audiobook Album, Album Artist, and optionally Genre from the confirmed Bindery assignment.
- Re-reads repaired metadata and verifies the write before reporting success.
- Stores before/after metadata in `/config/bookguard.db` and provides a Repairs page with guarded Undo.
- Refuses to metadata-repair `REJECT` or `MISSING` results.
- Keeps PDF, MOBI/AZW/AZW3, and other ebook metadata writing disabled for now; those formats remain inspection-only until their write paths are proven safe.
- Can optionally detach a tracked path using Bindery's API.
- Can optionally detach and move a single, non-shared tracked path into quarantine.
- Can put a file quarantined from Triage back where it was: open the quarantine in Activity and choose **Put back**. BookGuard refuses if anything is now at the original path, the quarantined file is missing or changed, or the path is outside the library folders. It then gets Bindery to track the file on the same book again (a library scan, adopting the unmatched file to that book, or, if the scan attached it to the wrong book, Bindery's Fix match) and turns monitoring back on. It also refuses if Bindery already has another copy of the book. Quarantines made before this version did not record enough to be put back.
- **Replace** (the main button on a Review book that contains the wrong file) quarantines a bad file and hands the rest to Bindery: it blocklists the download the file came from, keeps the book monitored, and runs Bindery's Automatic search, so Bindery picks, downloads and imports a new copy with your Bindery settings. BookGuard checks the new import like any other. If Bindery's automatic grabbing is off, Activity says so and what to click. Replace refuses when Bindery already has another copy of the book; use Quarantine then.
- **Library languages** (Settings → Scanning, default English) are the languages you keep. A book whose file declares any other language goes to Review → **Not in your library languages**, where the main button is **Replace**: BookGuard quarantines the file (nothing is deleted) and Bindery searches for another copy, so set Bindery's preferred language too. If the book's own title in Bindery is the foreign title, it is a foreign edition; remove it in Bindery instead. Write languages as names or codes (`English`, `en, nl`); an entry BookGuard does not recognise is refused when you save, and an empty field keeps every language. Junk codes some files carry (`xxx`, `un`, `zxx`) count as no language. Review follows the setting right away; run a scan to flag books that passed before.
- **Review → Duplicates** lists books Bindery shows twice for the same author because the titles differ only in case, punctuation or a leading article ("Nightingale" and "The Nightingale"). **Fix now** hides the empty extra entries BookGuard can prove are duplicates of a book you have, with Bindery's own exclude (kept, never searched for again, and can be included again on the book's page); when no entry has files, it keeps one. To do this every hour, turn on **Fix duplicate Bindery entries every hour** under Settings → Schedule (off by default, and only while actions are on). Entries with files are never changed: an ebook and audiobook on two entries, and two copies of the same kind, are only listed. Proof means the titles and author agree, Bindery's series data doesn't contradict it, and Bindery's year and language labels don't disagree, unless a shared ISBN/ASIN or series place proves it anyway; otherwise the pair is left for you with the reason. Anything the last run left alone is listed at the top of the page. Every change is checked again right before, recorded in Activity, and nothing is deleted.
- **Review → Unmatched** checks the files Bindery's library scan could not place (Bindery's Import page) and gives each a plain verdict with the evidence: **Junk** (not the file type it claims, broken, DRM-locked, nearly empty, a sample, silent audio, unreadable audio, or an exact copy of a file the library has), **Not in your library** (a real book whose own metadata/tags/ISBN and names agree, by an author or title Bindery doesn't have), **Belongs to a book** (at least two independent sources, one of them the file's own metadata, tags or ISBN, name the same Bindery book and it has no file of that kind yet; **Attach** uses Bindery's own adopt, which Bindery can undo), **Extra copy** (that book already has a file of this kind, so there is no Attach), **Another-language edition** (a translation of a library book), or **Unsure** (including when a file names a pen name or narrator for a book your library has under the folder's author). Titles match through series and number prefixes ("05 - ", "Book 4 - ", "TDT 0.5 "), ": Series, Book N" subtitles and "(Unabridged)"; authors match in any name order ("Rowling J.K.", "Corey, James S.A."); folders like "eBook", "m4b", "US" or "Edition 1" are skipped for the book folder above them; audio tagged with a music genre is **Junk**. It says **Unsure**, with the reason, when your library lists the book more than once, when only the audio tags name an author the folder disagrees with (often the narrator), or when the title is a series name and number ("Murderbot Diaries 05") of one of the author's Bindery series and Bindery doesn't say which book has that place. When it does, that book is the match. Books you excluded in Bindery are left out, as Bindery hides them too. Bindery's look-alike title suggestions are never taken as proof. Click **Check now** to run it; it only reads files.
- Each Review book shows the one action that fits its problem (Replace, Quarantine, Move or Fix details); the rest is under **More**. "Waiting for you" entries in Activity clear themselves once Bindery shows the problem is fixed.
- Quarantine from Triage stops Bindery monitoring the book first, so Bindery does not download a replacement while the file is out. If the quarantine fails, monitoring is turned back on.

BookGuard **never automatically deletes media**.
