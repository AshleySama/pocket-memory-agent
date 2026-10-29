# Third-party notices — 2026-10-29 local Beta

Pocket Memory source attribution: Copyright 2026 AshleySama / Pocket Memory contributors. Original project: <https://github.com/AshleySama/pocket-memory-agent>. The original project [LICENSE](LICENSE) and [NOTICE](NOTICE) are retained. Components below retain their own copyright and license terms. No upstream author is represented as endorsing this application.

## Runtime libraries

| Component | Installed version | Use and license |
| --- | --- | --- |
| LlamaIndex core | 0.14.25 | Local retrieval, splitting, prompt/context helpers; MIT, copyright Jerry Liu. |
| LlamaIndex BM25 retriever | 0.8.0 | Local lexical retrieval; MIT. |
| LlamaIndex workflows / instrumentation | 2.26.1 / 0.6.0 | Transitive runtime components; MIT. |
| bm25s | 0.3.15 | BM25 scoring; MIT. |
| PyStemmer | 2.2.0.3 | Stemming; its supplied notice covers the MIT wrapper and BSD Snowball components. |
| FlashRank | 0.2.10 | Local reranker library; Apache-2.0. This license is distinct from the model repository license below. |
| tokenizers | 0.23.1 | Local BGE tokenization and MultiBERT WordPiece construction; Apache-2.0. |
| llama-cpp-python / llama.cpp | 0.3.35 / native build bundled by that wheel | Local GGUF inference; MIT. Andrei Betlen and the llama.cpp / ggml authors retain their respective notices. |
| RapidOCR | 3.8.1 | OCR inference implementation; Apache-2.0. |
| ONNX Runtime | 1.26.0 | BGE/OCR/reranker execution; MIT with additional bundled third-party notices. |
| PyMuPDF / MuPDF | 1.28.2 | Document parsing/rendering; AGPLv3 or a separate Artifex commercial license. No commercial entitlement was found in the reviewed files. See [SOURCE_DISTRIBUTION.md](SOURCE_DISTRIBUTION.md). |
| NLTK | 3.10.4 | NLP software; Apache-2.0. Its data resources have separate terms, discussed below. |

Pillow, NumPy, pywebview, python-docx, openpyxl and other dependencies retain the notices captured under `licenses/python-packages/`. That collection includes nested binary-library notices, for example NumPy's numerical libraries and Shapely's LGPLv2.1 GEOS notice. It also contains build-tool notices when present in the environment. Consult the inventory for exact installed versions and paths. A package with multiple license texts is not reduced to a single permissive license by the table above. Where a wheel omits license text, upstream supplements are retained in `licenses/supplements/` or the named top-level license file. Texts obtained from a moving branch are identified by retrieval URL and content hash, not represented as version-pinned wheel contents.

Official library sources: [LlamaIndex](https://github.com/run-llama/llama_index), [LlamaIndex Workflows](https://github.com/run-llama/workflows-py), [FlashRank](https://github.com/PrithivirajDamodaran/FlashRank), [bm25s](https://github.com/xhluca/bm25s), [PyStemmer](https://github.com/snowballstem/pystemmer), [tokenizers](https://github.com/huggingface/tokenizers), [llama-cpp-python](https://github.com/abetlen/llama-cpp-python), [llama.cpp](https://github.com/ggml-org/llama.cpp), [ONNX Runtime](https://github.com/microsoft/onnxruntime).

`proxy_tools` 0.1.0 has inconsistent packaging metadata: its wheel declares MIT, while its installed source header and upstream `LICENSE.txt` specify BSD and credit Armin Ronacher (2013) and Jonathan Tushman (2014). The complete upstream BSD notice is retained in `licenses/supplements/proxy_tools-LICENSE.txt`; the inventory preserves the original metadata and records this discrepancy.

## Model assets

**Qwen3-4B-Instruct-2507, Q4_K_M GGUF.** Upstream model by Qwen / Alibaba Cloud, Apache-2.0. The shipped filename is `models/Qwen3-4B/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf`. Its own GGUF header identifies this model and points to the [official Qwen license](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/blob/main/LICENSE); the complete text is `licenses/Qwen3-4B-Instruct-2507-LICENSE.txt`. The header identifies no converter. The application's configured Voconly ModelScope download source is a fallback configuration, not proof of this existing file's origin. Conversion/quantization publisher and original download revision remain unverified. Pocket Memory reuses the existing GGUF and does not fine-tune it in this release. Legacy 0.6B/1.7B models and `.part` fragments are not selected by the reviewed release spec.

**BGE-small-zh-v1.5.** Upstream by Beijing Academy of Artificial Intelligence / FlagEmbedding contributors. [The official model card declares MIT](https://huggingface.co/BAAI/bge-small-zh-v1.5). The official FlagEmbedding MIT text, copyright 2022 staoxiao, is in `licenses/FlagEmbedding-MIT-LICENSE.txt`. Local files are `tokenizer.json`, `onnx/model_quantized.onnx`, and `onnx/model_quantized.onnx_data`. The two ONNX hashes in the retained 2026-10-12 source manifest match publisher metadata for [onnx-community/bge-small-zh-v1.5-ONNX](https://huggingface.co/onnx-community/bge-small-zh-v1.5-ONNX), revision `9507db33464b5da99a532ac26b2a251767cbc62b`; credit the ONNX Community conversion publisher. This match uses historical hashes, with current-file confirmation delegated to the release model fingerprint. No weights are modified by this notice update.

**RapidOCR / PaddleOCR models.** Local assets are `ch_PP-OCRv4_det_mobile.onnx`, `ch_PP-OCRv4_rec_mobile.onnx`, `ch_ppocr_mobile_v2.0_cls_mobile.onnx`, and `ppocr_keys_v1.txt`. Credit RapidAI / RapidOCR Authors for conversion and engineering, and Baidu / PaddleOCR rights holders for upstream model weights. [RapidOCR's model notice](https://github.com/RapidAI/RapidOCR/blob/main/README.md#-license) distinguishes these rights and states that applicable official PaddleOCR models and ONNX conversions use Apache-2.0. Complete RapidOCR and PaddleOCR license texts are retained separately. The installed RapidOCR `default_models.yaml` supplies named official ModelScope URLs and hashes for these model families; the release's model manifest is the source of current-file identity, not this attribution document.

**FlashRank MultiBERT.** Quantized `ms-marco-MultiBERT-L-12` assets are published by Prithivi Damodaran at [prithivida/flashrank](https://huggingface.co/prithivida/flashrank), revision `858a1ac046a05663a35367eac852d7f76feeefdd`. That model repository declares CC-BY-SA-4.0. Full terms are in `licenses/CC-BY-SA-4.0.txt`. See [RERANKER_NOTICE.md](RERANKER_NOTICE.md) for changes and exact asset provenance. The original model license and attribution remain applicable; model files are not relicensed under the application's Apache-2.0 license.

## NLTK language data

Only the local English stopword list and English `punkt_tab` files are selected. The Punkt README credits Jan Strunk and Tibor Kiss; the stopword README describes its Snowball/PostgreSQL origins and English-list additions. These READMEs are retained under `licenses/nltk-data/`.

The upstream [license overview](https://github.com/nltk/nltk_data/blob/gh-pages/LICENSE-OVERVIEW.md) and [dataset license table](https://github.com/nltk/nltk_data/blob/gh-pages/DATASET-LICENSES.md) explicitly identify `punkt_tab` and `stopwords` as unclarified/unknown. Both retrieved package XML files omit a license attribute. NLTK software's Apache-2.0 license does not resolve that dataset status. This local Beta retains the accepted runtime inputs, and no claim of settled redistribution permission for these data is made.

## Design attribution

The request-local source identity registry is an independent Python implementation inspired by Tencent WeKnora's `CitationSources` design. It is not a bundled WeKnora server, dependency, or port. Reference: [WeKnora source at commit 256a3f2966866337f7dc3c5fcc10037f2ae18d52](https://github.com/Tencent/WeKnora/blob/256a3f2966866337f7dc3c5fcc10037f2ae18d52/internal/application/service/chat_pipeline/citation_sources.go). The upstream MIT notice, copyright 2026 Tencent, and its accompanying dependency notices are preserved intact in `licenses/WeKnora-MIT-LICENSE.txt` as attribution reference; those listed WeKnora dependencies are not thereby claimed to be shipped in Pocket Memory.

## Scope

This is a bounded release attribution review based on the current spec, local assets, installed package metadata, and primary upstream notices. It does not certify legal compliance or verify all historical asset origins. Local Beta preparation and any later public binary distribution are separate events; the latter must use an actual complete corresponding-source arrangement and resolve the stated data/model provenance limitations.
