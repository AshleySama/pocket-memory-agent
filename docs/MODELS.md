# Model Setup and Licenses

## Repository policy

This source repository does not ship model weights. Model files are large, have their own licenses, and must be obtained from a trusted upstream publisher. Keep model directories out of Git.

## Components

| Capability | Typical component | Notes |
| --- | --- | --- |
| Local Q&A and automatic organization | Qwen3 GGUF via `llama-cpp-python` | The default implementation has been tested with a Qwen3-4B-Instruct-2507 Q4_K_M-compatible GGUF. Compatibility with another GGUF is not a quality guarantee. |
| Semantic retrieval | BAAI `bge-small-zh-v1.5` ONNX assets | Used to create local vector representations. Keyword/full-text retrieval remains available if it is absent. |
| Image OCR | RapidOCR assets | Important numbers and names extracted by OCR must be verified against the original image. |
| Retained-path reranking | FlashRank 0.2.10 with `ms-marco-MultiBERT-L-12` | Local scorer; the model, original vocabulary and manifest are required. |
| Sentence/lexical resources | NLTK `punkt_tab` and `stopwords` | Install before using retained mode; no startup download is required. |

## 1.2.0-beta.1 asset layout

This Beta keeps the existing **Qwen3-4B-Instruct-2507 Q4_K_M** model configuration. Installing `requirements.txt` installs libraries, not weights. In source mode, place assets under the repository's `models/` directory:

```text
models/
  Qwen3-4B/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf
  bge-small-zh-v1.5/tokenizer.json
  bge-small-zh-v1.5/onnx/model_quantized.onnx
  bge-small-zh-v1.5/onnx/model_quantized.onnx_data
  rapidocr/ch_PP-OCRv4_det_mobile.onnx
  rapidocr/ch_PP-OCRv4_rec_mobile.onnx
  rapidocr/ch_ppocr_mobile_v2.0_cls_mobile.onnx
  rapidocr/ppocr_keys_v1.txt
  reranker-models/ms-marco-MultiBERT-L-12/
    flashrank-MultiBERT-L12_Q.onnx
    config.json
    tokenizer.json
    tokenizer_config.json
    special_tokens_map.json
    vocab.txt
  reranker-manifest.json
  nltk-data/tokenizers/punkt_tab/
  nltk-data/corpora/stopwords/
```

The full Windows package carries the release-selected assets in its bundled runtime layout. Keep that layout intact. The BGE ONNX graph requires its adjacent `model_quantized.onnx_data` external weight file (approximately 90 MB); copying the `.onnx` file alone is incomplete. OCR explicitly loads the four files under `models/rapidocr/` shown above, rather than relying on default model files from the RapidOCR installation. Retain the RapidOCR package configuration resources and all applicable notices as well.

Upstream references: [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507), [BGE original model](https://huggingface.co/BAAI/bge-small-zh-v1.5), [BGE ONNX conversion](https://huggingface.co/onnx-community/bge-small-zh-v1.5-ONNX), [RapidOCR](https://github.com/RapidAI/RapidOCR), [FlashRank](https://github.com/PrithivirajDamodaran/FlashRank), and [NLTK data](https://github.com/nltk/nltk_data). The Qwen original repository is not itself the exact GGUF conversion; retain the chosen conversion's provenance and release checksum.

### Pinned reranker and manifest

Obtain the [pinned MultiBERT archive](https://huggingface.co/prithivida/flashrank/resolve/858a1ac046a05663a35367eac852d7f76feeefdd/ms-marco-MultiBERT-L-12.zip) from revision `858a1ac046a05663a35367eac852d7f76feeefdd`. Its recorded archive SHA-256 is `2c8ae5542f1538e355e9f295d349fc030b5cf7d398c8728c362f0e5129776971`. Verify the downloaded archive before extraction, then place its files at the exact reranker directory shown above. Do not download a different revision and silently reuse these hashes.

Create `models/reranker-manifest.json` as UTF-8 JSON using the following provenance record. `dependencies` maps paths relative to `models/` to SHA-256 values. Verify those file hashes during provisioning; this runtime does not automatically verify all manifest hashes at startup.

```json
{
  "component": "FlashRank",
  "version": "0.2.10",
  "model": "ms-marco-MultiBERT-L-12",
  "source": "https://huggingface.co/prithivida/flashrank/resolve/858a1ac046a05663a35367eac852d7f76feeefdd/ms-marco-MultiBERT-L-12.zip",
  "revision": "858a1ac046a05663a35367eac852d7f76feeefdd",
  "archive_sha256": "2c8ae5542f1538e355e9f295d349fc030b5cf7d398c8728c362f0e5129776971",
  "dependencies": {
    "reranker-models/ms-marco-MultiBERT-L-12/config.json": "b32201ee1c654b59f4c97b2139286edcae9cce525217c6d2131425fa50161946",
    "reranker-models/ms-marco-MultiBERT-L-12/flashrank-MultiBERT-L12_Q.onnx": "ff24c4abf96a982ce24b021bab80d9d8a1c3c2ba6926fb41e372a9be1d8cba9b",
    "reranker-models/ms-marco-MultiBERT-L-12/special_tokens_map.json": "303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3",
    "reranker-models/ms-marco-MultiBERT-L-12/tokenizer.json": "ce64fce797c24f68df90b40a3f74f579b336a493db14bd583fd520ea0d8c9a98",
    "reranker-models/ms-marco-MultiBERT-L-12/tokenizer_config.json": "eaf913a687e8eed3010b6479d0d84631038212aa50c14d9b812e08cbc891d925",
    "reranker-models/ms-marco-MultiBERT-L-12/vocab.txt": "87b44292b452f6c05afa49b2e488e7eedf79ea4f4c39db6f2f4b37764228ef3f"
  }
}
```

The runtime reads the manifest's UTF-8 text into the rerank-cache key alongside the question, evidence and tokenizer protocol. Keep a stable manifest with the installed assets; a changed manifest invalidates cache identity. The record above deliberately includes model assets only and requires no private development directories. It is a provisioning record, not an answer-quality certificate.

The upstream archive's `tokenizer.json` contains 30,522 token IDs, while this model and its original `vocab.txt` use 105,879. Pocket Memory constructs its in-memory WordPiece tokenizer from the original vocabulary and configuration and validates the IDs, using protocol `original-vocab-bert-wordpiece-v1`. Keep the original files unchanged; do not substitute the unrelated tokenizer vocabulary. FlashRank library code and model assets have different licenses: see [Third-party notices](../THIRD_PARTY_NOTICES.md) and the archive publisher's [model card](https://huggingface.co/prithivida/flashrank/blob/858a1ac046a05663a35367eac852d7f76feeefdd/README.md).

### NLTK resources

During source setup, with network access available, install resources explicitly:

```powershell
.\.venv\Scripts\python.exe -m nltk.downloader -d models/nltk-data punkt_tab stopwords
```

The packaged Beta includes the English resources used by the retained pipeline. Source setup may install all languages supplied by those NLTK packages. Retain their individual license/readme files. The runtime defaults `NLTK_DATA` to its local asset directory; if you set that environment variable yourself, it must point to a compatible prepared resource tree.

The upstream [NLTK data license overview](https://github.com/nltk/nltk_data/blob/gh-pages/LICENSE-OVERVIEW.md) and [dataset license table](https://github.com/nltk/nltk_data/blob/gh-pages/DATASET-LICENSES.md) mark `punkt_tab` and `stopwords` dataset licenses as unclarified/unknown. NLTK software's Apache-2.0 license does not resolve the data licenses. The Beta notices retain the source READMEs and record this unresolved status; they do not certify public redistribution clearance. See [Third-party notices](../THIRD_PARTY_NOTICES.md).

If retained assets are unavailable, basic notes and keyword search remain usable. An explicit `"rag_backend": "legacy"` setting in the active configuration followed by a restart selects the compatibility backend; it does not make missing GGUF, embedding or OCR models available and is not an equivalent-quality claim.

## Model choice

Changing the selected GGUF file does not automatically prove the result is equivalent. Model instruction following, context behavior, tokenizer assumptions, output speed, memory use, and Chinese retrieval-grounded answer quality can change. Test a candidate model against your own de-identified acceptance set before relying on it.

Use the application model selector only for compatible GGUF files. Other model families may load but may produce degraded answers or fail because their chat template and tokenizer behavior differ.

## License and redistribution checks

- Qwen3 upstream model files use Apache-2.0 according to the upstream repository. A GGUF conversion may add its own metadata and distribution terms.
- BGE model use is subject to the model card and FlagEmbedding license.
- RapidOCR code and OCR model assets have separate licensing considerations.
- FlashRank code is Apache-2.0; the pinned quantized model repository identifies its model/vocabulary under CC-BY-SA-4.0. Preserve attribution, source/version and adaptation notices.
- NLTK package resources have separate data terms; the upstream license status of the included `punkt_tab` and `stopwords` datasets remains unclarified/unknown. The Python package license does not resolve it.

Before sharing a packaged build with models included, verify each exact artifact's license, source, checksum, attribution, and redistribution conditions. Keep a release-specific software bill of materials and license inventory.
