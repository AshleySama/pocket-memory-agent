# RAG Limitations and Evaluation

## What the system is designed to do

Pocket Memory retrieves local notes and document chunks, reranks candidates, and presents a source-backed answer. Its useful output is not only a sentence: it includes the evidence sentence, source title, and location needed to inspect the original context.

## What it cannot guarantee

- A semantically related chunk may still be the wrong source for the exact question.
- Long PDF, OCR, and complex spreadsheet content may be extracted or chunked imperfectly.
- A local model can paraphrase incompletely, over-compress a list, or fail to answer despite correct retrieval.
- Short follow-up questions can be ambiguous. The application should only use prior conversation context when the current question clearly depends on it.

Never treat an answer as the authoritative record when a source document is available. Verify high-impact facts in the linked source.

## Public synthetic fixtures

The fixture files under `tests/fixtures/` are synthetic or publicly verifiable regression materials. They exercise retrieval behavior and answer contracts, but they do not establish production readiness. They intentionally do not include the project's real user-library data or real-feedback regression cases.

## Recommended acceptance set

Before a release, build at least 100 de-identified real-world questions against a separate private evaluation library. Cover:

- Exact parameters, dates, and values
- Multi-note comparison and enumeration
- Long notes and long documents
- Spreadsheet rows, worksheets, and headers
- OCR noise and scanned PDF content
- Similar-concept distractions
- No-answer and ambiguous questions
- Follow-up questions with and without valid subject continuity
- Source-opening and evidence-highlighting behavior

Measure retrieval recall and precision, source/evidence support, factual correctness, correct refusal, source-opening accuracy, and latency. A release should be blocked by any high-risk unsupported answer, not merely by an average score.

## Regression rule

Any change to chunking, query normalization, conversational context, candidate reranking, answer generation, or evidence selection should add a synthetic regression case before merge. Keep de-identified real-user feedback and acceptance results in a private evaluation repository.
