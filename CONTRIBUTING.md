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
