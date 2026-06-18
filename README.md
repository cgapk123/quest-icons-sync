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

## 评论同步（sync_reviews.py）

自动拉取 Meta Quest 商店**公开评论**，**无需** `reviews.5698452.xyz` 密钥。

### 原理

| 步骤 | 来源 |
|------|------|
| 包名 → App ID | MetaMetadata `oculus_public` / QLoader / OculusDB |
| 评论内容 | Meta 官方 `ocapi/graphql`（公开 persisted query，无 OAuth） |

yaas-nightly 使用的 `reviews.5698452.xyz` 现已返回 **403**，本工具改走 Meta 商店同款 GraphQL。

### 本地测试

```bash
pip install requests

# 单应用测试（推荐先跑）
python sync_reviews.py --package com.beatgames.beatsaber

# 拉取 100 条评论
python sync_reviews.py --package com.beatgames.beatsaber --max-reviews 100

# 拉取全部评论（Beat Saber 约 9000+ 条，耗时长）
python sync_reviews.py --package com.beatgames.beatsaber --max-reviews 0

# 按最新排序
python sync_reviews.py --package com.beatgames.beatsaber --sort newest --max-reviews 50

# 批量（每个应用默认最多 100 条）
python sync_reviews.py --max-apps 10 --max-reviews 100
```

输出：

```
reviews/
  com.beatgames.beatsaber.json
  manifest.json
```

JSON 字段与 yaas 类似：`reviews[]`、`rating_average`、`review_helpful_count`、`developer_response` 等。

### GitHub Actions

仓库内已有 `.github/workflows/sync-reviews.yml`，可手动 Run workflow 或等定时任务。

默认定时任务配置：

| 环境变量 | 值 | 说明 |
|----------|-----|------|
| `FROM_ICONS` | 1 | 只同步 `icons/` 里已有图标的应用 |
| `ONLY_MISSING` | 1 | 跳过已有 JSON，每次追平新应用 |
| `MAX_APPS` | 150 | 每轮最多新同步 150 个应用 |
| `MAX_REVIEWS` | 100 | 每个应用最多 100 条评论 |
| `MAX_WORKERS` | 1 | 串行请求，避免 Meta 429 |
| `META_MIN_INTERVAL` | 1.5 | 两次 GraphQL 请求最小间隔（秒） |
| `HTTP_RETRIES` | 10 | 429/5xx 自动退避重试 |
| `RETRY_COOLDOWN` | 120 | 批次失败后整轮冷却再重试 |

因此 **评论 JSON 数量会远少于图标**：图标一次全量同步约 2 万个；评论受 Meta API 速率限制，每轮只新增约 150 个应用的 JSON，需多轮 run 才能追平。

调用示例：

```
https://raw.githubusercontent.com/cgapk123/quest-icons-sync/main/reviews/com.beatgames.beatsaber.json
```

### 限制说明

| 参数 | 默认 | 说明 |
|------|------|------|
| `--page-size` | 20 | 每次 API 请求条数 |
| `--max-reviews` | 0 | 每应用评论上限，0=全部 |
| `--max-apps` | 0 | 每轮处理应用数，0=不限制 |
| `--only-missing` | CI 默认开 | 只拉尚无 JSON 的应用 |
| `--from-icons` | CI 默认开 | 与 `icons/` 目录对齐 |
| `--sort` | helpful | `helpful` 最有帮助 / `newest` 最新 |

- 全量拉取（如 Beat Saber 9000+ 条）耗时长，建议批量任务设 `--max-reviews 100`
- Meta 会返回 **429 Too Many Requests**，脚本已内置全局限速 + 指数退避；请勿把 `MAX_WORKERS` 设大于 1
- 请控制 `--max-apps` 和 `--meta-min-interval`，避免 Meta 限流
- [OculusDB API](https://oculusdb.rui2015.me/api/docs) 无评论正文

