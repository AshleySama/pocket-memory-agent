# Pocket Memory

Windows 本地资料工作台：把笔记、文件、问答、待办和资料汇编放在同一个工作空间。当前源代码版本为 **1.0.14**。已有笔记停笔后自动保存，保存状态可见，草稿用于异常恢复；新笔记仍需首次保存。

## 产品展示

### [查看在线产品介绍（GitHub Pages）→](https://ashleysama.github.io/pocket-memory-agent/)

<p align="center">
  <img src="website/pocket-memory-workbench-demo.png?v=20261118" alt="Pocket Memory 当前工作台：分类、笔记列表与多页签编辑区" width="760">
</p>

<p align="center"><i>在同一个工作台中管理分类与笔记，用多页签整理内容。</i></p>

<p align="center">
  <img src="website/pocket-memory-project-review.png?v=20261118" alt="Pocket Memory 智能问答：原文高亮、回答与来源同屏核对" width="760">
</p>

<p align="center"><i>左侧原文高亮，右侧展示回答与来源，可逐项核对支持结论的证据。</i></p>

## 功能

- 多页签笔记、分类标签、收藏/置顶、历史版本；编辑草稿自动保存与恢复。
- TXT、DOCX、XLSX、PDF、图片导入；OCR、失败重试、重复原件识别；未提交原件可暂存当前浏览器。
- 全文检索、中文向量/BM25混合检索、重排与带出处的本地问答。保留字段/表格/OCR范围校验和确定性直答。
- 资料汇编：搜索并选择笔记与文件，按内容范围组织资料、保留原文条件与精确引用；支持全文编辑、HTML导出、批量删除、清空与撤销。复杂培训和综合分析仍需人工核对。
- 待办提取与文字加工：根据当前笔记或粘贴内容生成可核对、可编辑的结果；确认后保存。独立Excel分析工作台已下线，普通Excel文件导入保留。
- 完整离线首次安装；后续程序更新复用现有模型，保护资料及配置，启动检查失败回退。
- 桌面关闭后可从通知区域双击恢复，托盘右键退出结束程序；使用 Windows 原生窗口选择资料目录。

默认使用现有 Qwen3-4B-Instruct-2507 Q4_K_M GGUF。资料和推理留在本机；自动更新/模型补充下载是可选网络行为，不是云端生成。复杂跨文档、条件推理、碎裂OCR仍需人工核对；正式版本号不等于答案准确率保证。T14s等纯CPU办公电脑的长问答可能需要几十秒。

## 从源码运行

Windows 10/11 x64，Python 3.11；桌面窗口建议安装Edge WebView2。

```powershell
git clone https://github.com/AshleySama/pocket-memory-agent.git
cd pocket-memory-agent
.\setup_venv.bat
.\run_dev.bat
```

按 [模型说明](docs/MODELS.md) 准备本地模型。GitHub不包含模型、DLL/EXE、安装包、私人资料或开发期间的原始评测记录。完整离线包面向无需配置Python的新用户；腾讯云下载文件另行发布，本提交不会自动上线下载。

## 构建与验证

1.0.14改善资料汇编断行与段落衔接，精简正文引用标记并保留来源核对，调整草稿入口和汇编界面。保留4B与1.0.13更新恢复能力，验证范围见[1.0.14验收记录](docs/VALIDATION_1.0.14.md)。

1.0.10修复打包后的资料汇编生成及更新交接问题，验证范围和旧客户端恢复办法见[热修复记录](docs/HOTFIX_1.0.10.md)。

见 [1.0.14发布说明](docs/RELEASE_1.0.14.md) 和 [离线交付流程](docs/OFFLINE_BUILD.md)。功能代码和当前保护测试在同一仓库；`scripts/release_policy.py`统一剔除视频FFmpeg DLL、未用的下载加速/推理后端、第三方测试样例和废弃设计素材。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m unittest discover -s tests/release_current -v
node tests/frontend_current/run.cjs
```

自动测试验证工程约束，不代表人工事实准确率。[评测与限制](docs/RAG_LIMITATIONS_AND_EVALUATION.md)保留历史口径；发布时不把历史成绩冒充新模型实测。

## 开源组件与许可

使用 llama-cpp-python / llama.cpp、LlamaIndex core/BM25、FlashRank、BGE、RapidOCR、ONNX Runtime、PyMuPDF、python-docx、openpyxl 等。借鉴来源管理思想不代表完整采用某个开源产品。详见 [NOTICE](NOTICE)、[第三方声明](THIRD_PARTY_NOTICES.md)、[源码分发说明](SOURCE_DISTRIBUTION.md) 与 `licenses/`。模型和依赖分别遵守其许可证；许可证是分发必要文件，保留在离线包中。
