# Ravenis Core Intelligence Push and R2 Archive

## 开关

默认配置文件已开启：

```yaml
intelligence_push:
  enabled: true
```

GitHub Secrets 可覆盖：

```text
INTELLIGENCE_PUSH_ENABLED=false
```

关闭后，企业微信会回到原来的 `message_plan` / 旧拆分逻辑。

## 必要 R2 Secrets

```text
S3_BUCKET_NAME
S3_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY
S3_ENDPOINT_URL
S3_REGION
```

Cloudflare R2 的 `S3_REGION` 通常填 `auto`。

## 推送格式

企业微信会按逻辑发送三段，不再在每段前添加 `[第 x/y 批次]`：

```text
0. 量子祈坛
1. 量子知识之塔
2. 量子试炼之塔
```

微信文本不显示 URL。原 URL、来源、分类、标签、分数和事件簇关系会写入 R2。

## 编号规则

原始新闻：

```text
YYYYMMDD + SLOT + 3位序号
20260619A001
```

事件簇：

```text
YYYYMMDD + SLOT + C + 2位序号
20260619AC01
```

微信里显示短编号：

```text
A001
AC01
```

## R2 路径

```text
news/2026/06/19/A/20260619A001.json
clusters/2026/06/19/A/20260619AC01.json
reports/2026/06/19/A/wechat_message_0.txt
reports/2026/06/19/A/wechat_message_1.txt
reports/2026/06/19/A/wechat_message_2.txt
```

旧的每日 SQLite 仍会保留：

```text
news/2026-06-19.db
rss/2026-06-19.db
```

## 历史搜索

`tools/build_history_index.py` 现在会同时读取：

- 旧 SQLite：`news/YYYY-MM-DD.db`、`rss/YYYY-MM-DD.db`
- 新 JSON：`news/YYYY/MM/DD/SLOT/*.json`、`clusters/YYYY/MM/DD/SLOT/*.json`

GitHub Pages 搜索页会显示编号、分类和分数，并支持按 `编号新闻` / `事件簇` 过滤。

## Dry Run

恢复 Python 环境后可以运行：

```bash
python tools/dry_run_intelligence_push.py
```

它会检查：

- 45 条输入能生成 45 个 raw item ID
- `20260619A001` 这类 ID 正确
- 微信文本不包含 URL
- 每条消息不超过 4000 bytes

## 回退行为

- R2 上传失败：只打印日志，不影响微信发送。
- 新推送渲染失败：自动回退到原 `message_plan` 或旧拆分逻辑。
- AI 聚合失败：规则分类、评分、事件簇和普通列表仍可工作。
