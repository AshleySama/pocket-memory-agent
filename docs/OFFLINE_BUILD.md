# 构建完整离线包和小程序更新包

Windows x64、Python 3.11，安装 `requirements-dev.txt`。准备已核验的模型目录（含GGUF、BGE、OCR、MultiBERT和NLTK数据），不要把个人资料目录当模型目录。先完成代码回归，再构建一次；布局/ZIP检查失败时只重做受影响步骤。

```powershell
$env:POCKET_MEMORY_NO_MODELS = '1'
.venv\Scripts\python.exe -m PyInstaller --noconfirm PocketMemory.spec
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --noconsole --name PocketMemoryUpdateWorker --paths . scripts/update_worker.py
.venv\Scripts\python.exe scripts/prepare_offline_delivery.py --built-app dist/PocketMemory --model-source D:/verified-models --destination release/PocketMemory-1.0.0 --version 1.0.0 --mode full --updater-exe dist/PocketMemoryUpdateWorker.exe --schema-id legacy-unversioned
```

在built-app内补入当前 `scripts/PACKAGE_README.txt`（命名README.txt）、LICENSE、NOTICE、THIRD_PARTY_NOTICES.md、RERANKER_NOTICE.md、SOURCE_DISTRIBUTION.md及licenses目录，再运行布局脚本。实际运行该完整布局的EXE验证OCR/文件解析/问答/报告/备份；测试使用独立配置及临时资料，不能把测试后的数据目录重新压入包。

将 `--mode full` 改为 `--mode update` 并选择新输出目录，可生成仅程序payload与校验清单；该目录不是新用户安装包。现有schema无迁移时使用上述标识，不得用同一标识掩盖未来不兼容迁移。

`scripts/release_policy.py` 与 spec、交付脚本共同执行排除规则。不得删 `cv2.pyd`、ONNX/NumPy、文档解析库或运行时元数据来冒充无损精简。测试源文件留Git以供维护；第三方测试数据和本地原始运行结果不打包。

模型应位于EXE同级models目录，仅完整首次包带模型。校验模型文件逐项SHA256与既有模型清单一致。最终ZIP以版本化目录为根，检查CRC、文件白名单及版本后生成SHA256。保留旧包；不自动覆盖 `latest` 或上传云端。应用更新须在可信发布通道实际验证后上线。
