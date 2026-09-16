# Model Setup and Licenses

## Repository policy

This source repository does not ship model weights. Model files are large, have their own licenses, and must be obtained from a trusted upstream publisher. Keep model directories out of Git.

## Components

| Capability | Typical component | Notes |
| --- | --- | --- |
| Local Q&A and automatic organization | Qwen3 GGUF via `llama-cpp-python` | The default implementation has been tested with a Qwen3-4B-Instruct-2507 Q4_K_M-compatible GGUF. Compatibility with another GGUF is not a quality guarantee. |
| Semantic retrieval | BAAI `bge-small-zh-v1.5` ONNX assets | Used to create local vector representations. Keyword/full-text retrieval remains available if it is absent. |
| Image OCR | RapidOCR assets | Important numbers and names extracted by OCR must be verified against the original image. |

## Model choice

Changing the selected GGUF file does not automatically prove the result is equivalent. Model instruction following, context behavior, tokenizer assumptions, output speed, memory use, and Chinese retrieval-grounded answer quality can change. Test a candidate model against your own de-identified acceptance set before relying on it.

Use the application model selector only for compatible GGUF files. Other model families may load but may produce degraded answers or fail because their chat template and tokenizer behavior differ.

## License and redistribution checks

- Qwen3 upstream model files use Apache-2.0 according to the upstream repository. A GGUF conversion may add its own metadata and distribution terms.
- BGE model use is subject to the model card and FlagEmbedding license.
- RapidOCR code and OCR model assets have separate licensing considerations.

Before sharing a packaged build with models included, verify each exact artifact's license, source, checksum, attribution, and redistribution conditions. Keep a release-specific software bill of materials and license inventory.
