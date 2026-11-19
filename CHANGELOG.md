# Changelog

## 1.0.13 - 2026-11-19

- 修复更新组件缺失导致无法升级的问题，增加内置更新方式。
- 改善首次启动加载不完整的问题，增加有限重试与恢复提示。

## 1.0.12 - 2026-11-19

- 修复更新组件缺失检查，避免不完整安装继续升级。
- 补齐更新前后校验，异常时保留备份并回滚。
- 增加完整包恢复指引，保留原资料库。

## 1.0.11 - 2026-11-19

- 优化汇编范围识别，减少无关内容。
- 支持按指定表格行汇编，保留引用。
- 减少无效计算，加快部分汇编任务。

## 1.0.10 - 2026-11-18

- 修复打包版资料汇编计算缓存指纹时依赖不存在的 Python 源文件，导致无法生成。
- 修复更新准备回执短暂拒读即失败；保留身份校验、有界重试和安全退出次序。


## 1.0.9 - 2026-11-18

- 引用合并展示、定位更准确。
- 减少部分问答的无效计算和无关上下文，缩短首次提问的等待。
- 报告支持批量删除、一键清空和撤销删除。

## 1.0.8 - 2026-11-16

- Save edits to existing notes and tabs automatically after an idle pause; replace the editor's Done action with Read. Keep explicit first save for new notes and local drafts for recovery.
- Serialize saves, protect IME input and navigation, reject stale content from another window, and preserve a pre-edit history checkpoint. Failed saves remain recoverable.
- Coalesce background AI organization after editing settles; do not apply results computed from an outdated note group. Keep edited content searchable.
- Recover explicitly dated actions from empty to-do extraction results, and bind complete copied lists to a uniquely supported original source without dropping their conditions.
- Preserve the current 4B model, data, settings, accepted themes and update mechanism.

See [release notes](docs/RELEASE_1.0.8.md) for verification scope and limits.

## 1.0.7 - 2026-11-16

- Extend deterministic answers to complete, source-bounded lists and explicitly requested source sections, while retaining conditions, source identity and the existing 4B fallback.
- Restore original-sentence highlighting for supported inline/suffix citations and conservative, lightly rewritten multi-clause answers. Preserve full context, existing citation numbers and original source coordinates.
- Remove the repeated tray notification on closing the window; tray restore and exit remain available.
- Keep models, saved notes, preferences, accepted themes and the updater unchanged. New installations retain AI organization and warmup defaults; updates keep saved settings.

Reuse the validated isolated-candidate checks and historical admission/display replays; these are not a new 100-question accuracy evaluation or a target-machine speed claim. See [release notes](docs/RELEASE_1.0.7.md) for scope and verification limits.

## 1.0.6 - 2026-11-15

- Align the search and Q&A shell with the middle column, including resized sidebars and narrow windows. Preserve the purple Q&A background and animated stars.
- Unify header button dimensions, borders, centered labels and local MIT-licensed Tabler outline icons; use a clearer soft yellow for New Note.
- Reduce the draft status row from 47px to 31px while retaining the draft box and retry controls.
- Preserve all five themes and existing 4B, RAG, storage, desktop and updater behavior. New installs retain AI organization and warmup defaults; upgrades retain saved preferences.

Validated the newly built native EXE and bundled wide/narrow UI; reused unchanged source UI evidence. Models are identical to 1.0.5. No new RAG generation, target-machine upgrade/rollback run, or performance claim. See [release notes](docs/RELEASE_1.0.6.md).

## 1.0.5 - 2026-11-15

- Improve PDF citation display and original-text highlighting while retaining source identities and full evidence for verification.
- Extend long-document learning/research reports with source-bound knowledge points, conditions and cross-page context. Simplify export feedback and expose the saved file and HTML download.
- Add native notification-area restore/quit controls and restore the Windows folder picker owned by the desktop window.
- Synchronize the intervening 1.0.1–1.0.4 desktop DLL, Windows short-path, model-load diagnostics and update handoff fixes into the public source. Retain the existing 4B model, data/configuration preservation and failed-start rollback design.
- New installs default to AI organization and background model warmup; upgrades retain saved preferences.

Source checks and package integrity were verified. Further 1.0.5 EXE upgrade/rollback and native-picker acceptance was stopped at the owner's request; it remains pending on the target PC. No new performance or general RAG-accuracy claim is made. See [release notes](docs/RELEASE_1.0.5.md).

## 1.0.0 - 2026-11-11

First public stable release. Earlier 1.2.x builds were internal release candidates; their numbering is historical, not an upgrade predecessor for this new-user release.

- Retain the verified local 4B, RAG/source safeguards, report workbench, drafts, import recovery, local tools and safe program updates.
- Keep the subdued sidebar statistics; remove the duplicate total-note row. Today/week/categories/tags remain in a two-column grid.
- Publish the complete offline download at the stable PocketMemory-latest.zip object; program updates remain versioned and model-free.
- Canonical repository: https://github.com/AshleySama/pocket-memory-agent.

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
