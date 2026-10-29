# 1.2.0-beta.1 release boundary

This is an evaluation Beta with the existing local 4B model, not a new RAG
optimization round or a general factual-accuracy guarantee. Source and packaged
code share the reviewed runtime; private data and model weights are not in Git.

567 distinct Python checks and 36 frontend checks passed locally. Historical
RAG evidence is reused for unchanged input paths; replay equality is not accuracy.
The packaged runtime successfully loaded its local models, OCR and XLSX parser.
One disclosed quality limitation is that a correct table answer can show source
evidence containing an adjacent row. The strict evidence-scope check remains
failed; it was not converted to a quality pass. Full package mechanics results
and final hashes are in the local release receipt, not private evaluation logs.

Use Start-Beta.bat for isolated data-beta and beta-profile; stop with
Stop-Beta.bat. Retain old stable program files and a separate data backup.
Target-machine performance, business factual review and refreshed-page draft
recovery are not certified. Open cross-document, conditional and semantic
citation limitations remain. See THIRD_PARTY_NOTICES.md for component terms,
including AGPL-covered dependencies and unresolved NLTK data provenance.
