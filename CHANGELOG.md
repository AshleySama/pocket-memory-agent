# Changelog

## 1.2.0-beta.1 - 2026-10-29

Beta source and separate Windows testing package; the existing Qwen3-4B-Instruct-2507 Q4_K_M model choice is retained.

- Integrate the retained local RAG runtime into the normal application: LlamaIndex BGE/BM25 fusion, local FlashRank reranking, structured evidence handling and request-local source identity. Keep explicit `rag_backend=legacy` rollback.
- Invalidate stale library snapshots, protect indexing against concurrent edits/deletion/library switching, and serialize shared model requests. Keep manual model unload while disabling the previous default idle unload.
- Open notes in reading mode with explicit Edit/Done controls, conditional formatting tools and real save states. Preserve failed drafts for retry in the current page; serialize saves per body tab and clear editing state on library switches. Drafts are not persisted across refresh.
- Group answer sources and expose tab/evidence details and clarification choices. Preserve the accepted themes, visual effects and Enter submission behavior.
- Install the complete application requirements in source setup; document the extra reranker and NLTK assets required by retained mode. Keep weights and private evaluation material outside Git.
- Provide separate `Start-Beta.bat`/`Stop-Beta.bat` launchers with package-local `beta-profile` and `data-beta`; preserve the old stable installation and original library.

Source validation: 567 distinct Python tests and 36 frontend checks passed; one stale note-tab label expectation was updated and rechecked. Packaged local-model Q&A, OCR, document parsing, child-tab writes and backup/restore were exercised. The strict historical quality smoke remains failed: a correct table answer includes an adjacent row in displayed evidence; a correct missing-field refusal cites the inspected table rather than returning zero sources. No RAG code was changed to make these checks pass. Target-machine and business acceptance remain pending. Engineering checks do not establish factual accuracy. Cross-document omissions, semantic citation errors, long-material/context limits, preparation latency and complex OCR/source-location behavior remain known limitations.

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
