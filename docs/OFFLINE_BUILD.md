# 当前1.0.16构建说明

当前完整包和新版程序包默认使用 `--executor main-exe-v1`（schema 2），不需要独立worker；源码中的旧单步打包入口已停止，以spec和prepare_offline_delivery.py为准。

面向原版13/14的程序更新包必须单独使用 `--mode update --executor legacy-bridge`，传入实际构建的双协议worker和 `--bridge-worker-receipt`。凭据绑定主EXE/worker SHA256及真实 `--describe-capabilities` 探针结果；不允许仅修改schema或跳过探针。旧通道不能指向无worker的schema 2 ZIP。

下面保留旧布局说明供参考；示例版本和standalone组件方式不作为1.0.16完整包命令。已知升级限制见[安装与更新说明](INSTALL_WINDOWS.md#software-updates)。

---

# 构建完整离线包和小程序更新包

Windows x64、Python 3.11，安装 `requirements-dev.txt`。准备已核验的模型目录（含GGUF、BGE、OCR、MultiBERT和NLTK数据），不要把个人资料目录当模型目录。先完成代码回归，再构建一次；布局/ZIP检查失败时只重做受影响步骤。

```powershell
$env:POCKET_MEMORY_NO_MODELS = '1'
.venv\Scripts\python.exe -m PyInstaller --noconfirm PocketMemory.spec
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --noconsole --name PocketMemoryUpdateWorker --paths . scripts/update_worker.py
.venv\Scripts\python.exe scripts/prepare_offline_delivery.py --built-app dist/PocketMemory --model-source D:/verified-models --destination release/PocketMemory-1.0.5 --version 1.0.5 --mode full --updater-exe dist/PocketMemoryUpdateWorker.exe --schema-id legacy-unversioned
```

在built-app内补入当前 `scripts/PACKAGE_README.txt`（命名README.txt）、LICENSE、NOTICE、THIRD_PARTY_NOTICES.md、RERANKER_NOTICE.md、SOURCE_DISTRIBUTION.md及licenses目录，再运行布局脚本。实际运行该完整布局的EXE验证OCR/文件解析/问答/报告/备份；测试使用独立配置及临时资料，不能把测试后的数据目录重新压入包。

将 `--mode full` 改为 `--mode update` 并选择新输出目录，可生成仅程序payload与校验清单；该目录不是新用户安装包。现有schema无迁移时使用上述标识，不得用同一标识掩盖未来不兼容迁移。

完整 ZIP 以版本目录为根；程序更新 ZIP 必须直接包含 `update-manifest.json` 和 `payload/`，不能额外套一层目录。更新清单的 URL 指向版本化程序包，`size` 与 `sha256` 取最终 ZIP 的实值；不指向包含模型的 `latest` 完整包。

`scripts/release_policy.py` 与 spec、交付脚本共同执行排除规则。不得删 `cv2.pyd`、ONNX/NumPy、文档解析库或运行时元数据来冒充无损精简。测试源文件留Git以供维护；第三方测试数据和本地原始运行结果不打包。

模型应位于EXE同级models目录，仅完整首次包带模型。校验模型文件逐项SHA256与既有模型清单一致。最终ZIP以版本化目录为根，检查CRC、文件白名单及版本后生成SHA256。保留旧包；不自动覆盖 `latest` 或上传云端。应用更新须在可信发布通道实际验证后上线。
