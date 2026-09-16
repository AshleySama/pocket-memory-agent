"""版本信息与更新检查配置。

部署到腾讯云服务器后，将 UPDATE_CHECK_URL 改为你的域名。
version.json 格式：
{
  "version": "1.1.1",
  "download_url": "https://yourdomain.com/downloads/PocketMemory-1.1.1.zip",
  "release_notes": "1. 新增XXX\n2. 修复XXX",
  "min_required": "1.0.0"
}
"""
from __future__ import annotations

APP_VERSION = "1.1.1"

# 发布站点就绪后再配置该地址。空值表示关闭网络更新检查，保持本地优先。
UPDATE_CHECK_URL = ""
