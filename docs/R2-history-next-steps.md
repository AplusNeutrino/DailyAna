# R2 历史新闻归档后续计划

这部分暂缓实现，后续继续处理时从这里接上。

## 当前已完成

- GitHub Pages 历史搜索台已部署：`https://aplusneutrino.github.io/DailyAna/history/`
- 新增 `tools/build_history_index.py`
- Work / Relax workflow 已在爬取后生成历史索引并部署 Pages
- Pages 搜索台读取 `docs/history/history-index.json`
- R2 未配置时会显示空索引，不影响 Pages 部署

## 后续需要用户准备

在 GitHub Repository Secrets 中配置：

```text
S3_BUCKET_NAME
S3_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY
S3_ENDPOINT_URL
S3_REGION
```

Cloudflare R2 的 `S3_REGION` 填 `auto`。

## 后续要验证

- R2 中是否生成 `news/YYYY-MM-DD.db`
- R2 中是否生成 `rss/YYYY-MM-DD.db`
- R2 中是否生成 `history/history-index.json`
- Pages 搜索台是否能检索真实历史新闻
- 超过 30 天的 `news/*.db` 和 `rss/*.db` 是否会自动删除

## 设计约束

- GitHub Pages 是公开静态页面，不保存 R2 密钥
- Pages 只读取公开轻量索引
- 索引只包含日期、标题、来源、URL、类型、检索文本
- 不保存正文，不保存私密配置，不保存 AI 长摘要
