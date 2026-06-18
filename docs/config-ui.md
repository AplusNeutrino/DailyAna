# DailyAna 配置台

本地启动：

```powershell
cd DailyAna
.\start-config-ui.bat
```

如果你已经有项目虚拟环境，也可以直接运行：

```powershell
python tools/config_ui.py
```

打开：

```text
http://127.0.0.1:8765
```

## 功能

- 添加、查找、编辑、删除热榜平台源。
- 添加、查找、编辑、删除 RSS 源。
- 添加、查找、编辑、删除关键词组。
- 分别调整 work 和 relax 两套推送方案。
- 调整热榜、RSS、AI 分析、新增热点、独立展示区是否显示。
- 调整 AI 分析是否包含 RSS、standalone、排名轨迹。
- 预留历史新闻搜索接口：`/api/history/search?q=关键词`。

## 两套推送方案

GitHub Actions 已经拆成：

- `Get Hot News - Work`，设置 `DAILYANA_PROFILE=work`
- `Get Hot News - Relax`，设置 `DAILYANA_PROFILE=relax`

这两个 profile 不绑定工作日/周末。你可以按一天三次推送的时段混搭，例如：

- 早上、下午使用 `work`
- 晚上使用 `relax`

要调整混搭方式，只需要在两个 workflow 之间移动 cron 行。

配置台保存的 profile 文件位于：

```text
config/profiles/work.yaml
config/profiles/relax.yaml
```

运行时会先读取 `config/config.yaml`，再用对应 profile 覆盖同名字段。

## 数据库接口预留

当前历史搜索接口会返回未接入状态。后续接入数据库后，保持接口路径不变即可：

```text
GET /api/history/search?q=关键词
```

建议返回格式：

```json
{
  "ready": true,
  "query": "DeepSeek",
  "items": [
    {
      "title": "新闻标题",
      "source": "知乎",
      "url": "https://example.com",
      "published_at": "2026-06-18 10:00:00",
      "summary": "可选摘要"
    }
  ]
}
```
