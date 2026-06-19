# Ravenis Core 使用手册

## 1. 启动配置台

双击桌面快捷方式：

```text
Ravenis Core Config UI.lnk
```

或在项目目录运行：

```powershell
cd DailyAna
.\start-config-ui.bat
```

启动后打开：

```text
http://127.0.0.1:8765/
```

配置台运行时需要保留黑色窗口。关闭窗口会停止配置台。

## 2. 开关层级

配置台现在有三层控制：

1. 总开关：位于“信息源”页面顶部。
   - 允许发送推送通知：对应 `notification.enabled`。
   - 启用热榜平台抓取：对应 `platforms.enabled`。
   - 启用 RSS 抓取：对应 `rss.enabled`。

2. 推送方案开关：位于“推送方案”页面。
   - `work` 和 `relax` 各自有独立推送开关。
   - 关闭某个方案后，该方案对应 workflow 仍可运行抓取和归档，但不会发送通知。

3. 内容显示开关：位于“推送方案”页面。
   - 控制热榜、RSS、AI 分析、新增热点、独立展示区是否出现在推送中。

总开关优先级最高：总推送关闭后，work/relax 即使开启也不会发通知。

## 3. 内容分类

当前有三类：

- `frontier` / 前沿：AI、数据、科技、产业与前沿公司。
- `leisure` / 休闲：游戏、影视、泛娱乐、轻内容。
- `current_events` / 时事：国内外公共事件、宏观动态、综合新闻。

每个热榜平台、RSS 源、关键词组都可以归入一个或多个分类。

每个推送方案只需要勾选要启用的分类：

- `work` 默认启用：前沿 + 时事。
- `relax` 默认启用：休闲 + 时事。

如果你希望休闲推送完全不含时事，在 `relax` 方案里取消“时事”即可。

## 4. 添加 RSS 源

RSS 是最推荐添加的订阅源类型。

在“信息源”页面填写：

- id：英文、数字、短横线组成的唯一标识，例如 `nytimes-world`。
- 显示名：你自己看的名称，例如 `纽约时报 World`。
- RSS URL：RSS/Atom 地址，例如 `https://example.com/feed.xml`。
- max_age_days：可空。空值表示跟随全局设置；填 `0` 表示不过滤旧文章。
- 启用：是否启用这个 RSS。
- 所属内容分类：选择前沿、休闲、时事中的一个或多个。

保存后，RSS 会进入 `config/config.yaml` 的 `rss.feeds`，分类关系会进入 `config/content_categories.yaml`。

## 5. 普通网页能不能直接加入

不一定。

可以直接添加的情况：

- 页面提供 RSS/Atom。
- 页面提供公开 API。
- 页面属于 NewsNow 已支持的热榜平台。

不能直接添加的情况：

- 只是普通新闻列表网页，没有 RSS。
- 需要登录、滑动、点击加载、反爬很重。
- 内容只在前端 JavaScript 动态生成，且没有公开 feed/API。

常见判断方法：

- 找页面上的 RSS 图标。
- 尝试地址后缀：`/rss`、`/feed`、`/feed.xml`、`/atom.xml`。
- 查看网页源码，搜索 `application/rss+xml` 或 `application/atom+xml`。
- 搜索“网站名 RSS”或“网站名 feed”。

如果没有 RSS，但你很想接入，有三种路线：

- 找第三方 RSS 生成服务，例如 RSSHub。
- 找该网站是否有开放 API。
- 后续为这个网站写一个专门爬虫适配器。

目前配置台里的 RSS 添加框只能添加 RSS/Atom URL，不能把任意网页直接变成可爬源。

## 6. 热榜平台源

热榜平台来自 NewsNow 兼容接口。

字段含义：

- id：平台在 NewsNow API 中的标识，不是随便起的。
- 显示名：推送里展示的名字，可以自定义。
- expected_domain：安全校验域名，用来过滤异常链接。
- 启用：是否启用该热榜源。

新增热榜平台前，需要确认 NewsNow API 支持该 id。普通网页不要填到热榜平台里。

## 7. 关键词语法

关键词位于 `config/frequency_words.txt`，配置台可以编辑关键词组。

常用写法：

```text
[组名]
关键词
另一个关键词
```

含义：标题包含任意一个关键词就匹配，推送时显示为“组名”。

更多语法：

- `关键词`：标题包含该词即匹配。
- `/正则/ => 名称`：用正则表达式匹配，并显示为指定名称。
- `[组名]`：给整个词组命名。
- `+关键词`：必须包含该词。
- `!关键词`：匹配该词则排除。
- `@5`：该组最多显示 5 条。

全局过滤词位于 `[GLOBAL_FILTER]`，例如当前有：

```text
震惊
```

任何标题包含该词都会被排除。

## 8. 当前关键词含义

当前 33 个实际关键词组如下：

- 胖东来：胖东来、于东来相关。
- DeepSeek：深度求索、幻方量化、梁文锋、DeepSeek。
- 华为：华为、任正非、余承东、鸿蒙、海思、昇腾、鲲鹏等。
- 比亚迪：比亚迪、王传福、方程豹、腾势、仰望等。
- 大疆：大疆、汪滔、DJI、RoboMaster、Mavic 等。
- 宇树机器人：宇树、王兴兴、Unitree。
- 智元机器人：智元、稚晖君、彭志辉、AgiBot；同组还包含众擎机器人相关词。
- 黑神话悟空：黑神话、冯骥。
- 影之刃零：影之刃零、梁其伟。
- 三体/流浪地球：三体、流浪地球、刘慈欣、郭帆。
- 申奥：导演申奥相关。
- 京东：京东、刘强东、JD。
- 字节跳动：字节、张一鸣、抖音、TikTok、Lark、CapCut 等。
- 腾讯：腾讯、微信、QQ、天美、阅文、微众银行等。
- 国产开源模型：Qwen、MiniMax、GLM。
- 特斯拉：特斯拉、马斯克、Cybertruck、Model、FSD 等。
- 英伟达：英伟达、黄仁勋、NVIDIA、RTX、CUDA；同组还包含 AMD、苏姿丰、Ryzen 等。
- 微软：微软、Windows、Azure、Copilot；同组还包含谷歌、苹果、安卓、YouTube、Gemini、iPhone、Vision Pro 等。
- OpenAI：OpenAI、ChatGPT、Sora、DALL-E、Sam Altman；同组还包含 Anthropic、Claude。
- 中国：国产、中国。
- 东亚：日本、朝鲜、韩国。
- 北美：美国、加拿大。
- 西欧：法国、英国。
- 俄罗斯：俄罗斯、俄国。
- 印度：印度。
- AI 相关：AI、人工智能。
- 芯片：芯片、光刻机、半导体。
- 水电：水电、雅鲁藏布江；同组还包含光伏、核能、能源。
- 自动驾驶：自动驾驶、无人驾驶、智驾。
- 机器人：机器人、机械狗、四足、具身智能。
- 航天：月球、登月、火星、宇宙、飞船、航天、空间站、卫星。
- 量子：量子、脑机、基因等前沿科技词。
- 生产力：生产力、产业政策相关。

注意：由于当前文件里有些关键词没有用空行拆开，所以它们属于同一个实际组。例如 AMD 现在属于“英伟达”组，Claude 属于“OpenAI”组。

## 9. AI 分析与多源整合摘要

现有 AI 分析：

- `ai_analysis.enabled` 控制是否启用 AI 分析。
- `include_rss` 控制 RSS 是否参与 AI 分析。
- `max_news_for_analysis` 控制最多送入 AI 的新闻数量。

新增的“AI 整合摘要”配置：

- 默认关闭。
- 位于每个推送方案中。
- 用于后续将勾选分类里的热榜/RSS 信息先做去重，再让 AI 按固定数量输出整合摘要。

字段含义：

- 启用多源 AI 整合摘要：是否打开该能力。
- 合并相似/重复信息：是否去重相似标题。
- 纳入热榜源：是否把热榜内容送入整合。
- 纳入 RSS 源：是否把 RSS 内容送入整合。
- 整合后最多输出条数：最终输出多少条摘要。

当前开关默认关闭，不改变现有推送格式。建议等 RSS 源数量明显增多后，再打开并调试输出数量。

## 10. 推荐添加流程

添加一个 RSS：

1. 找到 RSS/Atom URL。
2. 在“信息源”页面填写 id、显示名、URL。
3. 勾选内容分类。
4. 保存 RSS 源。
5. 到“内容分类”页面确认它出现在正确分类中。
6. 到“推送方案”页面确认 work/relax 是否勾选了该分类。

添加一组关键词：

1. 到“关键词”页面新增关键词组。
2. 用 `[组名]` 写清楚展示名。
3. 写入关键词或正则。
4. 勾选内容分类。
5. 保存。

临时停推：

1. 只停 work：到“推送方案”选择 work，关闭“启用当前方案推送”。
2. 只停 relax：选择 relax，关闭“启用当前方案推送”。
3. 全部停推：到“信息源”关闭“允许发送推送通知”。

## 11. 文件位置

- 主配置：`config/config.yaml`
- 内容分类：`config/content_categories.yaml`
- work 方案：`config/profiles/work.yaml`
- relax 方案：`config/profiles/relax.yaml`
- 关键词：`config/frequency_words.txt`
- 配置台：`tools/config_ui.py`

## 12. 历史新闻搜索与 R2

GitHub Pages 上的历史搜索台位于：

```text
https://<你的 GitHub 用户名>.github.io/<仓库名>/history/
```

如果仓库名仍是 `DailyAna`，默认地址类似：

```text
https://AplusNeutrino.github.io/DailyAna/history/
```

历史搜索台是纯静态页面，不保存 R2 密钥。它读取由 GitHub Actions 生成的公开轻量索引：

```text
docs/history/history-index.json
```

索引字段只包含：

- 日期
- 标题
- 来源平台/来源 RSS
- 来源 URL
- 类型：热榜或 RSS
- 检索用文本

要让 Actions 生成真实历史索引，需要在 GitHub Secrets 中配置：

```text
S3_BUCKET_NAME
S3_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY
S3_ENDPOINT_URL
S3_REGION
```

Cloudflare R2 的 `S3_REGION` 可以填：

```text
auto
```

每次 work/relax workflow 跑完后，会执行：

```text
tools/build_history_index.py
```

它会：

- 读取 R2 中最近 30 天的 `news/YYYY-MM-DD.db` 和 `rss/YYYY-MM-DD.db`
- 生成 `docs/history/history-index.json`
- 上传一份到 R2 的 `history/history-index.json`
- 删除 R2 中超过 30 天的每日 SQLite 文件
- 部署 `docs/` 到 GitHub Pages

如果 R2 secrets 还没配置，搜索台仍会部署，但显示空索引。
