# Changelog

## 1.1.1 - 2026-10-12

RAG correctness, source navigation, and interface updates.

- Preserve complete table headers and row identity when answering named field questions; prefer complete table text over partial OCR evidence.
- Handle Chinese/English spacing, single-field requests, and multiple fields for one named subject without confusing them with multi-subject comparisons or source-relation questions.
- Strengthen evidence checks for numeric limits, conditions, negation, causal explanations, and explicit conversational follow-ups.
- Keep relevant note-tab sources and highlight the requested table row and fields when opening a citation.
- Repair settings interactions and refine navigation, editor tabs, Enter hints, the animated Q&A button, and Windows icons while preserving the existing themes.
- Add synthetic query, source-location, structured-table, evaluation-artifact, and frontend regression coverage.
- Preserve complete GGUF files in full-model builds while excluding incomplete download fragments. Model weights and release archives remain outside Git.

## 1.1.1 - 2026-10-02

Public-source synchronization and attribution hardening.

- Synced the current application runtime, frontend, scripts, tests, and public regression fixtures from the maintained product line.
- Added project-wide copyright and SPDX header verification to the GitHub Actions test workflow.
- Clarified original-project attribution in NOTICE and README while retaining Apache-2.0 licensing and third-party notices.
- Continued to exclude model weights, user data, release archives, local reports, and private evaluation material.

## 1.1.1-beta - 2026-09-16

First public-source preparation baseline.

- Local multi-tab notes, document import, OCR, local retrieval, cited Q&A, relation graph, and HTML topic reports.
- Windows source-run and PyInstaller packaging scripts.
- Public repository hygiene: no model weights, user data, release archives, internal reports, competition material, or real-user regression fixtures.

## Compatibility note

This release line is Beta software. RAG answer quality varies with source quality, parsing, retrieval, model compatibility, and the user's question. Use cited source text for verification.
