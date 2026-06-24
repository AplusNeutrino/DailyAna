-- Ravenis Core RSS 数据库表结构
-- 用于存储 RSS/Atom 订阅源数据

-- ============================================
-- RSS 源配置表
-- 存储订阅源的基本信息
-- ============================================
CREATE TABLE IF NOT EXISTS rss_feeds (
    id TEXT PRIMARY KEY,                      -- 源 ID（如 "hacker-news"）
    name TEXT NOT NULL,                       -- 显示名称（如 "Hacker News"）
    feed_url TEXT DEFAULT '',                 -- RSS/Atom URL（可选，配置文件中已有）
    is_active INTEGER DEFAULT 1,              -- 是否启用
    last_fetch_time TEXT,                     -- 最后抓取时间
    last_fetch_status TEXT,                   -- 最后抓取状态（success/failed）
    item_count INTEGER DEFAULT 0,             -- 当日条目数
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- RSS 条目表
-- 以 URL + feed_id 为唯一标识，支持去重存储
-- ============================================
CREATE TABLE IF NOT EXISTS rss_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,                      -- 标题
    feed_id TEXT NOT NULL,                    -- 所属 RSS 源
    url TEXT NOT NULL,                        -- 文章链接
    guid TEXT DEFAULT '',                     -- GUID/ID（RSS guid 或 Atom id）
    published_at TEXT,                        -- RSS 发布时间（ISO 格式）
    summary TEXT,                             -- 摘要/描述
    author TEXT,                              -- 作者
    first_crawl_time TEXT NOT NULL,           -- 首次抓取时间
    last_crawl_time TEXT NOT NULL,            -- 最后抓取时间
    crawl_count INTEGER DEFAULT 1,            -- 抓取次数
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (feed_id) REFERENCES rss_feeds(id)
);

-- ============================================
-- 抓取记录表
-- 记录每次抓取的时间和数量
-- ============================================
CREATE TABLE IF NOT EXISTS rss_crawl_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_time TEXT NOT NULL UNIQUE,          -- 抓取时间（HH:MM）
    total_items INTEGER DEFAULT 0,            -- 总条目数
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- 抓取来源状态表
-- 记录每次抓取各 RSS 源的成功/失败状态
-- ============================================
CREATE TABLE IF NOT EXISTS rss_crawl_status (
    crawl_record_id INTEGER NOT NULL,
    feed_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success', 'failed')),
    error_message TEXT,                       -- 失败时的错误信息
    PRIMARY KEY (crawl_record_id, feed_id),
    FOREIGN KEY (crawl_record_id) REFERENCES rss_crawl_records(id),
    FOREIGN KEY (feed_id) REFERENCES rss_feeds(id)
);

-- ============================================
-- 推送记录表
-- 用于 push_window once_per_day 功能
-- 以及 ai_analysis analysis_window once_per_day 功能
-- ============================================
CREATE TABLE IF NOT EXISTS rss_push_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,                -- 日期（YYYY-MM-DD）
    pushed INTEGER DEFAULT 0,                 -- 是否已推送
    push_time TEXT,                           -- 推送时间
    ai_analyzed INTEGER DEFAULT 0,            -- 是否已进行 AI 分析
    ai_analysis_time TEXT,                    -- AI 分析时间
    ai_analysis_mode TEXT,                    -- AI 分析模式
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- 索引定义
-- ============================================

-- RSS 源索引
-- ============================================
-- AI Digest full archive tables
-- ============================================
CREATE TABLE IF NOT EXISTS ai_digest_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    digest_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    page_url TEXT NOT NULL,
    primary_url TEXT DEFAULT '',
    published_at TEXT DEFAULT '',
    sentence TEXT DEFAULT '',
    link_text TEXT DEFAULT '',
    playbook TEXT DEFAULT '',
    significance TEXT DEFAULT '',
    use_cases_json TEXT DEFAULT '[]',
    source_urls_json TEXT DEFAULT '[]',
    clean_html TEXT DEFAULT '',
    full_text TEXT DEFAULT '',
    content_hash TEXT NOT NULL,
    first_crawl_time TEXT NOT NULL,
    last_crawl_time TEXT NOT NULL,
    crawl_count INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_digest_item_analysis (
    digest_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success', 'failed', 'skipped')),
    summary TEXT DEFAULT '',
    key_points_json TEXT DEFAULT '[]',
    category TEXT DEFAULT '',
    tags_json TEXT DEFAULT '[]',
    entities_json TEXT DEFAULT '[]',
    retrieval_keywords_json TEXT DEFAULT '[]',
    model TEXT DEFAULT '',
    error TEXT DEFAULT '',
    raw_response TEXT DEFAULT '',
    analyzed_at TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (digest_id) REFERENCES ai_digest_items(digest_id)
);

CREATE TABLE IF NOT EXISTS ai_digest_daily_analysis (
    date TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('success', 'failed', 'skipped')),
    summary TEXT DEFAULT '',
    theme_clusters_json TEXT DEFAULT '[]',
    notable_items_json TEXT DEFAULT '[]',
    overall_observation TEXT DEFAULT '',
    model TEXT DEFAULT '',
    error TEXT DEFAULT '',
    raw_response TEXT DEFAULT '',
    analyzed_at TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rss_feed ON rss_items(feed_id);

-- 发布时间索引（用于按时间排序）
CREATE INDEX IF NOT EXISTS idx_rss_published ON rss_items(published_at DESC);

-- 抓取时间索引（用于查询最新数据）
CREATE INDEX IF NOT EXISTS idx_rss_crawl_time ON rss_items(last_crawl_time);

-- 标题索引（用于标题搜索）
CREATE INDEX IF NOT EXISTS idx_rss_title ON rss_items(title);

-- URL + feed_id 唯一索引（实现去重）
CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_url_feed
    ON rss_items(url, feed_id);

-- GUID + feed_id 部分唯一索引（guid 非空时优先用 guid 去重）
CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_guid_feed
    ON rss_items(guid, feed_id) WHERE guid != '';

-- 抓取状态索引
CREATE INDEX IF NOT EXISTS idx_rss_crawl_status_record ON rss_crawl_status(crawl_record_id);

CREATE INDEX IF NOT EXISTS idx_ai_digest_date ON ai_digest_items(date);
CREATE INDEX IF NOT EXISTS idx_ai_digest_digest_id ON ai_digest_items(digest_id);
CREATE INDEX IF NOT EXISTS idx_ai_digest_content_hash ON ai_digest_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_ai_digest_published ON ai_digest_items(published_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS ai_digest_items_fts USING fts5(
    digest_id UNINDEXED,
    title,
    sentence,
    link_text,
    playbook,
    significance,
    use_cases,
    source_urls,
    full_text,
    analysis_summary,
    analysis_tags,
    analysis_keywords,
    tokenize='unicode61'
);
