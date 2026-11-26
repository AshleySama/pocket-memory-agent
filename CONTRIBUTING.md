# Contributing to Pocket Memory

Thanks for helping improve Pocket Memory.

## Before opening an issue

- Search existing issues first.
- Use a minimal synthetic example. Never paste personal notes, work documents, credentials, account numbers, or model files.
- State the app version, Windows version, selected model (if relevant), question, expected behavior, actual behavior, and whether the cited source opened to the intended position.

## Pull requests

1. Keep each pull request narrowly scoped.
2. Add or update a regression test for behavior changes.
3. Run `python -m unittest discover -s tests -q`.
4. Do not add model weights, databases, `data/`, release archives, screenshots containing private information, or generated test outputs.
5. Keep the loopback-only server restriction unless a separate security review changes the application model.

## RAG changes

For changes to chunking, query rewriting, reranking, evidence selection, or answer gating, include a synthetic regression case. State whether the change affects retrieval recall, evidence precision, refusal behavior, source opening, or latency.

## Public repository contents

Keep reusable application code, tests, synthetic/public fixtures, user and contributor documentation, release notes and license notices in Git. Keep assistant conversations, review prompts, development handoff notes, internal delivery policies, dated execution reports, raw user feedback, temporary libraries, screenshots containing private material and generated test results local.

Use ignored `outputs/` or `reports/` directories for run artifacts. New public fixtures must declare their provenance and contain no exported user data. Review the staged file list before pushing; `.gitignore` does not remove files that are already tracked. Test a change at the appropriate scope; documentation cleanup alone does not require model runs or packaging.
