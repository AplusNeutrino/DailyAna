# R2 历史新闻归档状态

旧 DailyAna GitHub Pages 历史搜索台已经停止日常构建。当前公开入口为：

```text
https://neutriverse.uk/ravenis/
```

R2 仍是最近 30 天历史的私有事实源；浏览器只接收经过字段白名单和 SHA-256 校验的公开发布包。旧地址保留一次性参数转发页，不再读取 SQLite 或全量单条对象。

当前发布结构、必需 Secrets、首次上线顺序和故障回退策略见：

```text
docs/ravenis-neutriverse-deployment.md
```

远程旧对象清理仍必须等待迁移计数与哈希核对完成，并满足 30 天保留期；发布工作流本身不执行远程删除。
