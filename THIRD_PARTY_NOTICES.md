# Third-Party Notices

Pocket Memory depends on third-party packages and may optionally use third-party model assets. This file is a source-level notice, not a complete legal audit. Before redistributing a packaged build, regenerate a dependency license inventory for the exact build environment and review all bundled files.

## Python packages

The project currently declares dependencies including `llama-cpp-python`, `RapidOCR`, `onnxruntime`, `Pillow`, `tokenizers`, `numpy`, `pywebview`, `python-docx`, `openpyxl`, and `PyMuPDF`. Each package retains its own license and notices. The exact version constraints are in `requirements.txt` and `requirements-dev.txt`.

`llama-cpp-python` is MIT licensed. Its native dependencies and any distributed binaries may have additional notices.

## Optional model assets

- Qwen3-4B-Instruct-2507 is published under Apache-2.0 by its upstream publisher. GGUF conversions can have different publishers and packaging metadata; obtain the intended file from a trusted source and retain the applicable upstream license and notice.
- BAAI `bge-small-zh-v1.5` is distributed through the FlagEmbedding project under MIT terms according to its model card.
- RapidOCR project code is Apache-2.0. RapidOCR notes that the OCR model copyrights are held separately by Baidu; verify model-asset redistribution permissions before bundling them in an installer.

No model weights are included in this repository. Downloading, converting, bundling, or redistributing a model is a separate licensing decision.

## Project assets

Brand and UI assets in `frontend/` and `website/` must only be redistributed after the repository maintainer has confirmed that they are original, properly licensed, or otherwise authorized for public distribution. Third-party icon files retain their original notices where applicable.
