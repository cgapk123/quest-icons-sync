#!/bin/bash
# 服务器 cron 部署示例
# crontab -e 添加:
# 0 3 * * * /opt/quest-icons-sync/run.sh >> /var/log/quest-icons-sync.log 2>&1

set -euo pipefail
cd "$(dirname "$0")"

# ---- 按需修改 ----
export SYNC_MODE=git          # git | remote | local
export ICON_DIR=/opt/quest-icons/icons
export METADATA_DIR=/opt/quest-icons/.cache/metmetadata
export MAX_WORKERS=8

# 上传到 nginx 静态目录
export UPLOAD_METHOD=copy
export DEPLOY_PATH=/var/www/html/icons

# 或使用 SFTP 传到另一台机器
# export UPLOAD_METHOD=sftp
# export SFTP_HOST=your.server.com
# export SFTP_USER=deploy
# export SFTP_PASSWORD=xxx
# export SFTP_REMOTE_DIR=/var/www/icons

# ---- 执行 ----
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -q -r requirements.txt
python sync_icons.py

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] sync done"
