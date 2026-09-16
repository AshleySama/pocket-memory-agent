# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
    StringStruct, VarFileInfo, VarStruct,
)
from PyInstaller.utils.hooks import collect_all

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

# 模型可作为可选资源：-WithoutModels 生成最小包；
# -OnlineModelBootstrap 则保留 BGE/OCR，将 4B 留给首次联网下载。
# 瘦身策略：
# 1. 只打包实际使用的模型子目录，排除历史 qwen3-0.6b/1.7b
# 2. 不打包 runtime/llama.cpp（项目用 llama-cpp-python 包，不调用 runtime 里的 exe/dll）
# 3. 不收集 rapidocr 包内置模型数据（ocr.py 显式指定 models/rapidocr/ 下的文件）
# 4. 排除未使用的重型依赖：pytest/unittest 测试框架、llama_index/haystack（仅 scripts 实验脚本用）
datas = [('frontend', 'frontend')]
if os.environ.get('POCKET_MEMORY_NO_MODELS') != '1':
    datas.extend([
        ('models/bge-small-zh-v1.5', 'models/bge-small-zh-v1.5'),
        ('models/rapidocr', 'models/rapidocr'),
    ])
    if os.environ.get('POCKET_MEMORY_NO_LLM') != '1':
        datas.append(('models/Qwen3-4B', 'models/Qwen3-4B'))

# 排除主应用运行时不需要的包（减少体积，不影响功能）
# - 测试框架：仅 tests/ 目录用，打包不含 tests
# - llama_index/haystack：仅 scripts/compare_framework_rag.py 在独立 venv 中用
# - matplotlib：仅 scripts/evaluate_rag.py 画图用
# 注意：不排除 distutils/lib2to3 等，PyInstaller 自身钩子依赖它们会崩溃
excludes = [
    'pytest', 'unittest', 'doctest',
    'llama_index', 'haystack', 'transformers', 'torch', 'tensorflow',
    'matplotlib', 'pandas', 'scipy', 'sklearn',
    'turtledemo', 'tkinter.test',
]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=llama_dlls[1] + rapidocr_datas[1] + docx_bundle[1] + openpyxl_bundle[1] + pymupdf_bundle[1],
    datas=datas + llama_dlls[0] + rapidocr_datas[0] + docx_bundle[0] + openpyxl_bundle[0] + pymupdf_bundle[0],
    hiddenimports=llama_dlls[2] + rapidocr_datas[2] + docx_bundle[2] + openpyxl_bundle[2] + pymupdf_bundle[2],
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
        filevers=(1, 1, 1, 0),
        prodvers=(1, 1, 1, 0),
        mask=0x3f,
        flags=0x0,
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
                StringStruct('FileVersion', '1.1.1'),
                StringStruct('InternalName', 'PocketMemory'),
                StringStruct('OriginalFilename', 'PocketMemory.exe'),
                StringStruct('ProductName', 'Pocket Memory'),
                StringStruct('ProductVersion', '1.1.1'),
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
