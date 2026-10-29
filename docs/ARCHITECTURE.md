# Architecture

## Scope

Pocket Memory is a single-user, Windows-first local application. It is not an internet-facing service and does not implement multi-user authentication, tenancy, or remote collaboration.

## Main flow

```text
Browser or desktop WebView
        |
        v
Loopback HTTP server (127.0.0.1 / localhost / ::1)
        |
        +-- Note store: Markdown, attachments, SQLite metadata and FTS
        +-- Document import: TXT, DOCX, XLSX, PDF, images, OCR
        +-- Chunking: paragraph/page/worksheet/row-aware source segments
        +-- Retrieval: full text + keyword + local semantic candidates
        +-- Reranking and evidence selection
        +-- Local inference: optional GGUF model through llama-cpp-python
        +-- Output: answer, evidence sentence, source, and source location
```

## Storage model

- Markdown and attachments preserve user-visible source material.
- SQLite stores note metadata, FTS indexes, chunk metadata, and local vector data.
- Imported documents keep their original file and store a structured extraction. A chunk records the relevant page, worksheet, heading, paragraph, or row range so users can return to context.

## Retrieval and answer path

The normal entry point remains `app.py`. `rag_backend=retained` is the default for 1.2.0-beta.1; `rag_backend=legacy` is an explicit compatibility fallback requiring a restart. The retained runtime lives in `pocket_memory/rag/retained/` and does not load development reports or private evaluation corpora.

1. The server and `rag/development.py` resolve the question and source scope against the current library. The wrapper owns candidate lifetime, serializes requests, and invalidates snapshots when the source revision changes or the library switches.
2. LlamaIndex combines the saved local BGE vectors with BM25 candidates using reciprocal-rank fusion. Query fusion uses a single query; it does not ask a cloud model to expand the question.
3. Local FlashRank scores candidate evidence. Table/field boundaries, source scope, original sentence windows and a context budget determine the material supplied to the answer path.
4. Direct record/table cases can produce deterministic output. Other supported routes use the existing local Qwen3-4B-Instruct-2507 Q4_K_M model and retained evidence/answer contracts. This release does not add a generative question planner, a source-selection agent, or automatic answer resynthesis.
5. A request-local evidence registry associates supplied blocks with original source spans where exact matching is available. Unresolved spans remain marked as fragments. Citation identity checks and shadow diagnostics do not establish semantic support for each claim.
6. The API returns the answer and source information. The UI groups sources and displays note tabs/evidence entries, and can offer source-bound choices when the question needs clarification. Opening a reference navigates to the corresponding source and highlights supported locations.

The local model session is shared under a lock. The previous default ten-minute idle unload is disabled; manual unloading remains available. Retrieval preparation, especially for long notes, can still dominate elapsed time. The current UI is not a guarantee of true model-token streaming.

For numeric, date, authorization, policy, quality, safety, or other high-impact facts, users should open the original source. Retrieval can omit material, generation can omit or add claims, and a valid citation number can still point to evidence that does not support the claim.

## Reading and editing

Notes open in reading mode. Edit/Done controls expose formatting only while editing. Saves are serialized by the body tab's identity; the UI reports actual save status and retains a failed draft in the current page for retry. Switching libraries clears the previous editing state. Draft retention is in page memory: refreshing or closing the page is not a durable draft-recovery mechanism. Existing themes, animated hover effects and keyboard question submission are preserved.

## Extensibility boundaries

Potential extensions include additional file parsers, better table/OCR structure extraction, improved chunking and reranking, user-owned model choices, and stronger evaluation tooling. Remote sharing would require a separate architecture for authentication, authorization, encryption, auditing, synchronization, and conflict handling; it is out of scope for this local application.
