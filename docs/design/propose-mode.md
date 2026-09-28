# Propose mode: from evidence to earned autonomy

Status: design, not yet implemented.

## Goal

BookGuard should work out what to do about each problem it finds, explain
why, and act on its own only for kinds of decisions that have earned it.

Today BookGuard can *prove* many problems: wrong content, unsafe files,
wrong media type. It still stops short of resolving them, for three
different reasons:

1. **Missing executors.** The `RECOVER_WRONG_CONTENT` plan has no automatic
   executor for `correct_or_quarantine` or `admit_and_reconcile`.
2. **Release choice is deliberately human.** E4 never selects a replacement
   candidate.
3. **Judgment that rules cannot supply.** Pen names, anthologies,
   translations, and bogus catalog entries need knowledge, not stricter
   matching.

Propose mode addresses the third reason directly. It also gives the first
two a safe path forward: automation is granted per decision type, only
after a measured track record.

## Evidence from the first live run (Unraid, 2026-09-28)

Of 855 REVIEW items, 42 were verified `WRONG_CONTENT`. Operator review
classified them as:

| Classification | Count | What BookGuard would need to know |
|---|---:|---|
| Genuinely wrong book, real catalog entry | 10 | Nothing new; replacement is the answer |
| Genuinely wrong book, catalog entry uncertain | 13 | Whether Bindery's entry is a real book |
| Bogus catalog entry (series name, box set, no year) | 5 | That the entry itself is the problem |
| Same book under a house pen name (Franklin W. Dixon) | 4 | That the pen name belongs to the expected author |
| Vague catalog entry holding real books (Nancy Drew) | 2 | That the entry, not the file, is wrong |
| Audiobook folder with multiple work titles | 8 | Whether it is a collection or a real mix-up |

Only 10 of 42 were clear-cut replacements. The rest needed judgment that a
person supplied in minutes. Separately, 484 of 604 initial verdicts were
`INSUFFICIENT_EVIDENCE`, 284 of them MOBI/AZW3 files that BookGuard cannot
read yet.

## Design

### 1. Better evidence first

Propose mode is only as good as its inputs. Two evidence PRs come first:

- **Native MOBI/AZW3 reading:** EXTH metadata (title, author, ISBN,
  language, publisher) plus PalmDOC and HUFF/CDIC text decoding, with the
  same defensive limits as the other parsers. The sampled files were
  DRM-free.
- **ISBN matching:** compare embedded identifiers with Bindery's
  `editions.isbn_13` / `editions.isbn_10` for the expected book (strong
  support) and for other books (strong contradiction).

### 2. Proposals

Each flagged subject gets exactly one proposal from a fixed menu:

| Classification | Proposed action |
|---|---|
| `wrong_book` | Quarantine and replace |
| `pen_name` | Add an author alias, then re-verify |
| `collection_or_anthology` | Accept, or flag the wrong volume |
| `translation` | Accept, or replace if the original language is wanted |
| `bogus_catalog_entry` | Quarantine the file; flag the Bindery entry for the operator |
| `unsure` | Ask the operator one specific question |

A proposal records:
- the classification and proposed action;
- the evidence it relied on;
- a plain-language rationale;
- a confidence level;
- which component proposed it (a deterministic rule or the reasoning layer).

Proposals extend the existing Observe decision journal
(`automation_observations`) and recovery plans (`recovery_plans`). They do
not replace them.

**Deterministic rules come first:**
- a consistent alternate author across a series suggests a pen name;
- catalog titles that are series names or sets, or entries with no year, no
  ISBN, and no editions, suggest a bogus entry;
- an embedded title that is a superset of the expected title with multiple
  credited authors suggests an anthology.

### 3. Optional reasoning layer

For subjects the rules cannot classify, BookGuard may ask a language model
(Claude via the Anthropic API).

**Off by default.** Enabling it is a deployment decision:
- `BOOKGUARD_REASONING_ENABLED=false`
- `ANTHROPIC_API_KEY` is set in the deployment only, like the clamd
  endpoint, so the web UI cannot become an arbitrary network client.
- `BOOKGUARD_REASONING_MAX_EXCERPT_CHARS` bounds what is sent.

**Evidence packet (the only data sent):**
- the expected title and author from Bindery;
- Bindery catalog facts for that entry: year, ISBNs, edition count, series;
- the embedded title, author, language, and identifiers;
- a bounded excerpt of front-of-book text (title page and copyright page);
- the deterministic verdict and its explanation.

**Never sent:** whole files, library paths, credentials, or anything for
subjects the rules already decided.

**Response:** strict JSON with `classification`, `proposedAction`,
`rationale`, `confidence`, and `claims` (the specific facts the model
relied on).

**Corroboration before a proposal is accepted:**

| Model claim | Must be backed by |
|---|---|
| `pen_name` | Titles match under normal title rules |
| `bogus_catalog_entry` | Bindery's own data: no year, no ISBN, or no editions |
| `wrong_book` | An existing deterministic `WRONG_CONTENT` verdict |
| `translation` | The embedded language differs, and an edition, ISBN, or title relation holds |

A claim that is not corroborated downgrades the proposal to `unsure`. An
unavailable model, a timeout, or malformed output also yields `unsure`,
never an action.

The model proposes; deterministic evidence confirms; existing guarded
executors act.

### 4. Scorecard and autonomy ladder

Every operator response to a proposal is recorded, per decision type:
accept, reject, or reject with a correction.

Each decision type sits on a ladder the operator controls:

```
off  ->  propose (operator clicks Accept)  ->  automatic
```

- **Promotion to `automatic`** requires N consecutive accepted proposals of
  that type and zero rejections. N is configurable, with a conservative
  default of 20.
- **Any rejection** returns the type to `propose` and resets the count.
- **Some actions are never automatic,** regardless of track record:
  deleting media, removing a Bindery catalog entry, and changing Bindery
  import settings.
- **Automatic actions still run through E4.** Allowlists, fresh boundary
  revalidation, one external mutation per cycle, quarantine-never-deletes,
  and admission-requires-`VERIFIED_CORRECT` all still apply. Autonomy
  decides *whether* to act, never *how safely*.

### 5. Missing executors

Automating `wrong_book` needs E4 executors for:
- quarantining exact proven wrong media (following the existing
  unsafe-media quarantine executor);
- admitting a verified replacement.

Automatic release choice becomes an opt-in, limited to unambiguous cases:
exactly one candidate passes every gate, preferably with an ISBN match. Any
ambiguity stays with the operator.

## First milestone (read-only)

Run Propose mode on the 42 `WRONG_CONTENT` results from the first live run
and compare its proposals with the operator's hand classification above.
Nothing is automated.

- **Where it agrees,** the approach is validated for that decision type.
- **Where it disagrees,** the disagreement shows exactly what evidence or
  knowledge is missing.

Report agreement per decision type, both with and without the reasoning
layer.

## Build order

1. Native MOBI/AZW3 reading
2. ISBN matching against Bindery editions
3. Proposals and scorecard, deterministic rules only; first milestone report
4. Optional reasoning layer; repeat the milestone report
5. Wrong-content quarantine and admission executors
6. Per-type automation as each type earns it; opt-in unambiguous release choice

## Non-goals

- **Replacing deterministic verification with model judgment.** Verdicts
  that drive irreversible actions stay deterministic.
- **Editing Bindery's catalog automatically.**
- **Sending library content anywhere unless the operator enables it.**
