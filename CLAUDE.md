# Continuing BookGuard

Start with [docs/handoff.md](docs/handoff.md) for the current work, branch
dependencies, tested revisions and the next unfinished step. Verify the current
GitHub branch and PR heads before resuming; the handoff is a dated checkpoint.

Use [docs/architecture.md](docs/architecture.md) to find the responsible module
and [docs/development.md](docs/development.md) for setup and required checks.
Update the handoff and relevant user guides before pausing or handing work to
another assistant. Record measured results, skips and limitations explicitly.

The everyday flow is **Check library → Review → apply a supported fix**.
Check library verifies unresolved REVIEW/REJECT results; it does not force
content verification of every PASS file. Keep specialized controls in Advanced.

Preserve independent action permissions, current-scan checks, fresh verification
at mutation boundaries, no-overwrite behavior and durable recovery journals.
An author's name alone cannot prove a particular ebook. Scanner outages and
insufficient identity evidence are not proof that a file is unsafe.

BookGuard currently hands replacement downloading/importing to Bindery.
Observe is read-only; the self-download, direct-publication and Automatic Mode
executors were removed. Design proposals and old roadmap issues describe
historical or deferred work, not implemented capabilities.

Host acceptance is separate from CI. Follow [docs/operations.md](docs/operations.md)
for idle-job checks, the guarded Unraid upgrade, provenance, runtime-hardening
and live ClamAV verification. Keep the deployed revision distinct from the
reviewed repository revision in every status report.
