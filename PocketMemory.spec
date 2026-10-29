# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

# -*- mode: python ; coding: utf-8 -*-
import os
import re
import runpy
from pathlib import Path

release_version = runpy.run_path(str(Path(SPECPATH) / 'pocket_memory' / 'version.py'))['APP_VERSION']
version_numbers = tuple(int(n) for n in re.match(r'(\d+)\.(\d+)\.(\d+)', release_version).groups()) + (0,)

from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
    StringStruct, VarFileInfo, VarStruct,
)
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

# collect_all returns (datas, binaries, hiddenimports). Keep that ordering so
# package data such as RapidOCR's default_models.yaml is present at runtime.
# llama-cpp-python's DLLs live under llama_cpp/lib and must be explicit too.
llama_dlls = collect_all('llama_cpp')

# rapidocr 包内置 default_models.yaml 等配置文件，PyInstaller 默认不收集数据文件，
# 不收集会导致打包后 OCR 初始化报 FileNotFoundError
rapidocr_datas = collect_all('rapidocr')

# 文档导入器在运行时按格式加载；显式收集其解析器和 PyMuPDF 二进制组件，
# 避免 PyInstaller 忽略方法体内的动态 import。
docx_bundle = collect_all('docx')
openpyxl_bundle = collect_all('openpyxl')
pymupdf_bundle = collect_all('pymupdf')

# The accepted RAG candidate uses local LlamaIndex core/BM25 and FlashRank.
# Keep collection narrow: never collect the llama_index namespace wholesale,
# which would also discover optional cloud LLM/embedding integrations.
rag_hiddenimports = [
    'llama_index.core',
    'llama_index.core.schema',
    'llama_index.core.node_parser',
    'llama_index.core.embeddings',
    'llama_index.core.vector_stores.simple',
    'llama_index.core.storage.storage_context',
    'llama_index.retrievers.bm25',
    'bm25s', 'Stemmer', 'flashrank', 'flashrank.Config', 'flashrank.Ranker',
    'nltk', 'tiktoken', 'tiktoken_ext.openai_public',
]
rag_datas = []
for distribution in (
    'llama-index-core', 'llama-index-retrievers-bm25',
    'llama-index-workflows', 'llama-index-instrumentation',
    'bm25s', 'PyStemmer', 'FlashRank', 'nltk', 'tiktoken', 'banks', 'pydantic',
):
    rag_datas.extend(copy_metadata(distribution))
# Core's tokenizer fallback and tiktoken's dynamically discovered plugin must
# work offline too. NLTK's minimal data set is shipped under models/nltk-data.
rag_datas.extend(collect_data_files(
    'llama_index.core', includes=['_static/tiktoken_cache/*'],
))

# 模型可作为可选资源：-WithoutModels 生成最小包；
# -OnlineModelBootstrap 则保留 BGE/OCR，将 4B 留给首次联网下载。
# 瘦身策略：
# 1. 只打包实际使用的模型子目录，排除历史 qwen3-0.6b/1.7b
# 2. 不打包 runtime/llama.cpp（项目用 llama-cpp-python 包，不调用 runtime 里的 exe/dll）
# 3. 不收集 rapidocr 包内置模型数据（ocr.py 显式指定 models/rapidocr/ 下的文件）
# 4. 排除未使用的重型依赖：测试框架、haystack、torch/transformers 等
datas = [('frontend', 'frontend')]
if os.environ.get('POCKET_MEMORY_NO_MODELS') != '1':
    datas.extend([
        ('models/bge-small-zh-v1.5', 'models/bge-small-zh-v1.5'),
        ('models/rapidocr', 'models/rapidocr'),
        ('models/reranker-models/ms-marco-MultiBERT-L-12', 'models/reranker-models/ms-marco-MultiBERT-L-12'),
        ('models/reranker-models/NOTICE.md', 'models/reranker-models'),
        ('models/reranker-manifest.json', 'models'),
        ('models/nltk-data', 'models/nltk-data'),
    ])
    if os.environ.get('POCKET_MEMORY_NO_LLM') != '1':
        # Ship complete weights, never leftover download fragments (*.gguf.part).
        datas.append(('models/Qwen3-4B/*.gguf', 'models/Qwen3-4B'))

# 排除主应用运行时不需要的包（减少体积，不影响功能）
# - 测试框架：仅 tests/ 目录用，打包不含 tests
# - haystack：仅独立实验使用；LlamaIndex core/BM25 已是本地候选运行依赖
# - matplotlib：仅 scripts/evaluate_rag.py 画图用
# 注意：不排除 distutils/lib2to3 等，PyInstaller 自身钩子依赖它们会崩溃
excludes = [
    'pytest', 'unittest', 'doctest',
    'haystack', 'transformers', 'torch', 'tensorflow',
    'llama_index.llms.openai', 'llama_index.embeddings.openai',
    'matplotlib', 'pandas', 'scipy', 'sklearn',
    'turtledemo', 'tkinter.test',
]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=llama_dlls[1] + rapidocr_datas[1] + docx_bundle[1] + openpyxl_bundle[1] + pymupdf_bundle[1],
    datas=datas + llama_dlls[0] + rapidocr_datas[0] + docx_bundle[0] + openpyxl_bundle[0] + pymupdf_bundle[0] + rag_datas,
    hiddenimports=llama_dlls[2] + rapidocr_datas[2] + docx_bundle[2] + openpyxl_bundle[2] + pymupdf_bundle[2] + rag_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# 版本信息资源：让 exe 在 Windows 属性中显示为正规软件，降低杀软启发式风险
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=version_numbers,
        prodvers=version_numbers,
        mask=0x3f,
        flags=0x2 if '-' in release_version else 0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable('040904B0', [
                StringStruct('CompanyName', 'PocketMemory'),
                StringStruct('FileDescription', 'Pocket Memory - Local Note & Knowledge App'),
                StringStruct('FileVersion', release_version),
                StringStruct('InternalName', 'PocketMemory'),
                StringStruct('OriginalFilename', 'PocketMemory.exe'),
                StringStruct('ProductName', 'Pocket Memory'),
                StringStruct('ProductVersion', release_version),
            ])
        ]),
        VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
    ]
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PocketMemory',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
    icon='frontend/app-icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PocketMemory',
)
