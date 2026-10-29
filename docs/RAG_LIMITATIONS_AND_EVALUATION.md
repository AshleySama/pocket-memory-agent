# RAG Limitations and Evaluation

## What the system is designed to do

Pocket Memory retrieves local notes and document chunks, reranks candidates, and presents a source-backed answer. Its useful output is not only a sentence: it includes the evidence sentence, source title, and location needed to inspect the original context.

## What it cannot guarantee

- A semantically related chunk may still be the wrong source for the exact question.
- Long PDF, OCR, and complex spreadsheet content may be extracted or chunked imperfectly.
- A local model can paraphrase incompletely, over-compress a list, or fail to answer despite correct retrieval.
- Short follow-up questions can be ambiguous. The application should only use prior conversation context when the current question clearly depends on it.

Never treat an answer as the authoritative record when a source document is available. Verify high-impact facts in the linked source.

## 1.2.0-beta.1 known limits

The integrated retained backend and 4B model remain a Beta boundary, not a new claim of production-level answer accuracy. Cross-document lists can be incomplete; a model can bind a claim to the wrong source even when the citation number is valid; long material can be omitted or clipped by evidence/context selection. Long-note preparation and reranking can take substantial time. Complex OCR, table layouts and source highlighting need review against original files. Automatic generation retries and a larger answer model are not introduced by this release.

Source grouping, original-span metadata and clarification references improve inspectability. They do not prove that all relevant material was supplied, that the answer is complete, or that every claim is supported. Some locations remain fragment-level when exact original spans cannot be validated.

The 2026-10-29 source checks cover 567 distinct Python tests and 36 frontend checks, with a stale source-label test expectation corrected and rechecked. These are engineering results, not factual-accuracy scores. Package smoke, target-machine performance and final user acceptance remain separate; package checks are pending for this documentation snapshot. No private score tables or user libraries are published here.

## Public synthetic fixtures

The fixture files under `tests/fixtures/` are synthetic or publicly verifiable regression materials. They exercise retrieval behavior and answer contracts, but they do not establish production readiness. Reported failures are represented by portable synthetic or demonstration cases; actual user libraries, attachments, private acceptance results, and raw feedback logs are not included.

The October 2026 updates add coverage for table identity and header mapping, single/multiple field requests, Chinese/English name spacing, source-tab coverage, and citation highlighting. A successful targeted test does not imply that arbitrary table layouts, OCR output, or business questions are supported without error.

## Recommended acceptance set

For an overall quality decision, maintain a fixed stratified set of about 100 questions in a separate private evaluation library, with redistribution-permitted public questions, real material and synthetic fixtures reported separately. Keep the questions, expected facts, sources and failure history stable across versions. Cover:

- Exact parameters, dates, and values
- Multi-note comparison and enumeration
- Long notes and long documents
- Spreadsheet rows, worksheets, and headers
- OCR noise and scanned PDF content
- Similar-concept distractions
- No-answer and ambiguous questions
- Follow-up questions with and without valid subject continuity
- Source-opening and evidence-highlighting behavior

Measure retrieval recall and precision, source/evidence support, factual correctness, correct refusal, source-opening accuracy, and latency separately. Keyword overlap, valid citation numbers and automated support proxies are not human factual review. Present a small question/expected facts/actual answer/citation packet early, record unresolved human judgments, and identify any confirmed release blocker instead of hiding it behind an average score.

## Regression rule

Use focused original-failure and adjacent checks during a fix, one affected batch regression after stabilization, and applicable final-input checks at the agreed release point. Reuse valid evidence when code, dependencies, models, data and configuration are unchanged. Changes to retrieval or answer behavior should include a representative synthetic regression case; do not rerun the entire benchmark for unrelated documentation or UI changes. Keep real-user feedback, full source corpora and private acceptance results outside the public repository.
