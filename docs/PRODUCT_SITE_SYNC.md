# 宣传页同步

GitHub Pages 的 `site/index.html` 从当前 `PocketMemoryWebsite/templates/index.html` 导出。CSS、JS、截图和报告样例按实际引用复制到 `site/static/`，保留公司站的内容与交互；资源使用相对路径，适用于 `/pocket-memory-agent/`。

修改宣传页后，在已有 Flask/Jinja 环境运行：

```powershell
python scripts/sync_product_site.py --source-dir "完整路径/PocketMemoryWebsite"
```

脚本只读取 config.py 中版本、大小、下载地址、视频地址四个公开值，不执行或复制公司配置、认证参数。`site/site-sync.json` 记录模板及输出资源的 SHA256。检查完整页面、窄屏、倍速菜单、报告样例和下载链接后，提交 site 的变化；main 分支上的 site 变化会由现有 Pages 工作流发布。

不要单独维护一份旧 Pages 文案，也不要将应用程序或模型打包作为网页更新的步骤。
