# Public Test Fixtures

This directory contains reusable synthetic examples, blank templates and public-fact regression material. Keep real-user feedback, personal libraries and generated execution reports outside the repository. Derived regression examples must be rewritten as independent synthetic fixtures with explicit provenance before submission.

- `rag_eval_constructed_100.json`: constructed Chinese retrieval regression set.
- `rag_eval_business.json`: synthetic business-themed retrieval examples.
- `rag_eval_realistic_office_100.json`: fictional office-style acceptance corpus and questions.
- `rag_eval_general_knowledge_100.json`: public-fact benchmark with source URLs.
- `rag_eval_cases.json`: small adversarial retrieval examples using public literary content.
- `release_eval_real_template.json`: blank template for a private, de-identified real-world acceptance set. Do not fill it with raw user data and commit it.

These files validate engineering regressions only. Factual-accuracy claims require a separate, appropriately scoped review of permitted real-world material, including source-opening and evidence checks. Store that private evidence and generated results outside Git; fixture pass counts are not a product accuracy score.

Product descriptions and performance figures inside synthetic corpus passages are fictional question material, not measurements of Pocket Memory. Do not reuse those numbers as product benchmark claims.
