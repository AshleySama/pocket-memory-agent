# Update failure safety validation — 2026-11-23

> Historical results, corrected 2026-11-26: these results came from an isolated
> working source tree, not the exact published `c546838` Git tree. Publication
> duplicated headers in three files, introducing a syntax error in
> `pocket_memory/embedded_update.py`. The old figures below do not validate that
> published commit. Current cleanup and focused checks are documented in
> [GITHUB_CLEANUP_20261126.md](GITHUB_CLEANUP_20261126.md).

Development branch only. This is not a new binary release. The version remains
1.0.16; main, published packages and public update feeds are unchanged.

## Retained safety changes

- Distinguish missing, unreadable, damaged and inventory-mismatched components.
- Persist a failed-update hold across restarts; rechecking a hold is read-only.
  Do not repeatedly download, launch, or exit the application after a failure.
- Hide stale success messages and require current transaction/version health
  before confirming a successful reconnect.
- Export a minimal allowlisted JSON diagnostic without note content, paths,
  account identifiers, raw exceptions, tokens or URLs. A file failure alone
  does not establish its cause or prove endpoint security intervention.
- Preserve exact inventory, size/hash, protected-data, and transaction checks.
- Retain prior executor declaration enforcement: when the installation declares
  its embedded executor, verify that executor; corruption must not select a
  fallback worker. No MSI or alternate endpoint-bypass route is introduced.
- Retain release signature/identity/timestamp checks and refusal of the obsolete
  raw-directory ZIP entry. Do not invoke internal packaging functions to skip them.

The signature requirement above describes the historical experiment. Current
packaging uses signature verification only when explicitly requested by the
publisher; exact inventory/hash and transaction validation remain mandatory.
The persistent hold now excludes a valid offer or user context changing before
execution: that condition clears the stale offer and requires a fresh check.

## Revalidation

- Current backend suite: **709 passed, 4 skipped, 713 total**, 103.236 seconds.
  Skips: three Windows 8.3-path fixtures and one directory-symlink privilege fixture.
- All **12 frontend test files** passed, including the new failure/resume/diagnostic
  harness that is not yet enumerated by the older five-file frontend runner.
- Initial failures were retained in local evidence. One assertion expected the old
  English exception text; it now checks the typed integrity failure. One startup
  test leaked a feature-switch environment variable to a later synthetic report
  test; it now restores its environment. No production answer or report logic
  was modified to make these tests pass.
- A disposable-library source server completed real HTTP note creation/autosave,
  multi-tab reads, keyword search, graph loading, actual OCR and XLSX import.
- One real local 4B question returned the expected synthetic fact and matching
  original-source citation in 9.859 seconds. This is a functional smoke sample,
  not a quality benchmark or target-laptop latency estimate.
- An actual no-filter source compilation preserved both note tabs and two source
  references. Full-document edit, reopen, HTML export, batch delete/undo, and ZIP
  backup/restore passed; restored SQLite integrity was OK with no FK violations.
- Browser checks covered onboarding, tabs, settings/update view and saved-report
  reopening. Update transaction success, refusal and rollback checks use controlled
  inert fixtures; they are not acceptance of a newly distributed EXE.

## Release remains blocked

No usable authorized code-signing certificate was present on the development host.
No new distribution executable/archive was built or uploaded. The enterprise
endpoint-security rejection remains unresolved and has not been tested by these
local checks. A signature alone would not guarantee a company's policy approval.
No endpoint policy, certificate trust or quarantine state was changed.

## Reproduction

From an isolated checkout with dependencies installed, use a writable test TEMP:

```text
python -m unittest discover -s tests/release_current -p "test_*.py" -v
```

Run each `tests/frontend_current/test_*.cjs` with Node. Preserve symlinks for the
main module if the local Windows sandbox cannot canonicalize its profile path.
Model-backed HTTP smoke requires the existing local model assets and a disposable
library. Do not point release or update tests at a user's live installation.

Keep validated source separate from binary release approval. Do not describe this
branch as resolving enterprise blocking or as proof that all report/RAG cases pass.
