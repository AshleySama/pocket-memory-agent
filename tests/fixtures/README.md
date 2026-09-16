# Public Test Fixtures

All fixtures in this directory are synthetic, de-identified templates, or public-fact regression material. They do not contain an exported personal library, private work documents, or real-user feedback.

- `rag_eval_constructed_100.json`: constructed Chinese retrieval regression set.
- `rag_eval_realistic_office_100.json`: fictional office-style acceptance corpus and questions.
- `rag_eval_general_knowledge_100.json`: public-fact benchmark with source URLs.
- `rag_eval_cases.json`: small adversarial retrieval examples using public literary content.
- `release_eval_real_template.json`: blank template for a private, de-identified real-world acceptance set. Do not fill it with raw user data and commit it.

These files validate engineering regressions only. A maintainable release decision needs a separate private evaluation library of at least 100 de-identified real-world questions, including source-opening and evidence checks.
