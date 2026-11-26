# 1.0.16一次性恢复控制器

信任文件绑定本批官方 1.0.13、1.0.14 EXE 及特定 1.0.16 程序 ZIP。冻结模式读取内嵌信任身份。

冻结时使用仓库根作为模块搜索路径：

```powershell
.venv/Scripts/python.exe -m PyInstaller --noconfirm --onefile --noconsole --noupx --name PocketMemoryRepairUpgrade --paths . --add-data "scripts/recovery_1_0_16/recovery-release.json;." scripts/recovery_1_0_16/recovery_tool.py
```

交付目录中EXE旁放置信任文件指定的Program.zip，程序ZIP不解压。单元检查中的进程健康是模拟，不能替代最终EXE的成功升级/失败回滚验证。已知企业安全软件限制见[安装与更新说明](../../docs/INSTALL_WINDOWS.md#software-updates)。重新构建的EXE可能哈希不同；不能用历史哈希替代新构建验收，也不能用修复工具绕过隔离。
