# Ravenis → Neutriverse 部署说明

Ravenis 界面由 `AplusNeutrino/My_Blog` 的 Chirpy/Jekyll 构建负责；DailyAna 只发布经过字段白名单校验的最近 30 天公开数据。

## 发布链路

1. `history-publish.yml` 从 `public-history/` 读取不超过 120 个投影文件。
2. 发布器生成 `manifest.json`、`search-index.json`、每日分片和周报。
3. 公开文件被打包为 `history/releases/<run_id>.tar.gz` 并计算 SHA-256。
4. 压缩包上传成功后，最后更新 `history/current.json`。
5. DailyAna 使用 `repository_dispatch: ravenis-history-published` 触发博客部署。
6. 博客使用只读 R2 凭据下载并校验数据，然后解压到 `_site/ravenis/data/`。

普通博客提交在 R2 暂时不可用时可使用 Actions 缓存中的最近一次验证成功发布包；由 Ravenis 数据发布触发的构建不允许回退，必须失败并保留线上上一版。

## 必需 Secrets

DailyAna 仓库：

- `S3_BUCKET_NAME`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_ENDPOINT_URL`
- `S3_REGION`（可选，默认 `auto`）
- `NEUTRIVERSE_DISPATCH_TOKEN`：仅用于向 `AplusNeutrino/My_Blog` 发送 `repository_dispatch`；细粒度令牌需授予目标仓库所需的 Contents 写权限。

博客仓库（必须使用独立的 R2 只读令牌）：

- `RAVENIS_R2_BUCKET`
- `RAVENIS_R2_ACCESS_KEY_ID`
- `RAVENIS_R2_SECRET_ACCESS_KEY`
- `RAVENIS_R2_ENDPOINT`
- `RAVENIS_R2_REGION`（可选，默认 `auto`）

## 首次上线顺序

1. 在两个仓库配置上述 Secrets。
2. 合并并部署博客代码，但不要手工向 `_site/ravenis/data/` 提交数据。
3. 手动运行 `Ravenis Core - History Publish`，确认生成不可变发布包、`current.json` 和博客 dispatch。
4. 检查 `https://neutriverse.uk/ravenis/?date=YYYY-MM-DD&slot=B`。
5. 手动运行一次 `Ravenis Core - Legacy History Redirect`，把旧 DailyAna Pages 查询参数转发到新地址；之后不再日常运行该工作流。

如果 R2 缺失、哈希不一致、索引为空或公开字段越界，博客的 Ravenis 触发构建必须失败，不得上传新的 Pages artifact。
