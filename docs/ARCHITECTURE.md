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

1. Normalize the current user question.
2. Retrieve candidates from full-text, keyword, and semantic indexes.
3. Prefer structurally relevant chunks and penalize superficial lexical matches.
4. Select a compact evidence sentence from a candidate chunk.
5. Ask the local model to answer only from the selected evidence when the request needs synthesis.
6. Render the answer with source references and an open-source action.

For direct numeric, date, authorization, policy, quality, safety, or other high-impact facts, users should open the source and verify it. The system may refuse when it cannot find direct evidence; a refusal is safer than an unsupported answer.

## Extensibility boundaries

Potential extensions include additional file parsers, better table/OCR structure extraction, improved chunking and reranking, user-owned model choices, and stronger evaluation tooling. Remote sharing would require a separate architecture for authentication, authorization, encryption, auditing, synchronization, and conflict handling; it is out of scope for this local application.
