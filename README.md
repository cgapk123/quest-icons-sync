# Quest Icons Sync

自动从 [MetaMetadata](https://github.com/threethan/MetaMetadata) 同步 Quest 游戏图标，输出 `com.package.name.jpg`，供其他程序直接 HTTP 调用。

## 输出格式

```
icons/
  com.beatgames.beatsaber.jpg
  com.meta.horizon.jpg
  manifest.json          # 包名 -> URL/大小/更新时间
```

其他程序调用示例：

```
https://你的域名/icons/com.beatgames.beatsaber.jpg
```

或 GitHub raw：

```
https://raw.githubusercontent.com/你的用户名/quest-icons-sync/main/icons/com.beatgames.beatsaber.jpg
```

## 本地测试

```bash
cd quest-icons-sync
pip install -r requirements.txt

# 推荐：稀疏克隆 MetaMetadata（需安装 git）
python sync_icons.py --mode git

# 无 git：纯 HTTP 拉 JSON（较慢）
python sync_icons.py --mode remote

# 已有本地 MetaMetadata data 目录
python sync_icons.py --mode local --metadata-dir D:/MetaMetadata
```

## 部署方案

### 方案 A：GitHub Actions（推荐，零服务器维护）

1. 把 `quest-icons-sync` 推送到你的 GitHub 新仓库
2. Actions 会每天自动运行 `.github/workflows/sync-icons.yml`
3. 图标 commit 到 `icons/` 目录
4. 程序通过 `raw.githubusercontent.com` 或 GitHub Pages 访问

**优点**：全自动、免费、有版本历史  
**缺点**：首次全量图标较多，仓库会变大（可后续加 Git LFS）

### 方案 B：自己的服务器 + cron

```bash
git clone <你的仓库> /opt/quest-icons-sync
chmod +x /opt/quest-icons-sync/run.sh

# 编辑 run.sh 里的 DEPLOY_PATH
crontab -e
# 0 3 * * * /opt/quest-icons-sync/run.sh >> /var/log/quest-icons-sync.log 2>&1
```

nginx 示例：

```nginx
location /icons/ {
    alias /var/www/html/icons/;
    expires 7d;
    add_header Access-Control-Allow-Origin *;
}
```

### 方案 C：GitHub Actions 抓图标 + SFTP 上传服务器

在 GitHub 仓库 Settings → Secrets 添加：

- `SFTP_HOST` / `SFTP_USER` / `SFTP_PASSWORD` / `SFTP_REMOTE_DIR`

然后取消 `sync-icons.yml` 里 SFTP 步骤的注释。

## 增量更新

- 已有 `icons/com.xxx.jpg` 且 URL 未变 → 跳过
- URL 变化 → 重新下载
- `--force` 强制全量重下

## 与你原脚本的区别

| 原 `test/提取图标.py` | 本工具 |
|----------------------|--------|
| 需手动下载 JSON | 自动从 MetaMetadata 拉取 |
| 扫描 5 个 data 子目录 | 只用 `data/common`（已合并最佳字段） |
| 本地跑完手动上传 | 支持 SFTP / 复制到 web 目录 / GitHub 托管 |

`data/common` 已包含 icon/landscape/square 等字段，无需再扫 oculus/oculusdb 等原始 JSON。
