# coding=utf-8
"""
配置加载模块

负责从 YAML 配置文件和环境变量加载配置。
"""

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from trendradar.utils.time import DEFAULT_TIMEZONE

from .config import parse_multi_account_config, validate_paired_configs


def _get_env_bool(key: str) -> Optional[bool]:
    """从环境变量获取布尔值，如果未设置返回 None"""
    value = os.environ.get(key, "").strip().lower()
    if not value:
        return None
    return value in ("true", "1")


def _get_env_int(key: str, default: int = 0) -> int:
    """从环境变量获取整数值"""
    value = os.environ.get(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_env_int_or_none(key: str) -> Optional[int]:
    """从环境变量获取整数值，未设置时返回 None"""
    value = os.environ.get(key, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _get_env_str(key: str, default: str = "") -> str:
    """从环境变量获取字符串值"""
    return os.environ.get(key, "").strip() or default


def _deep_merge_config(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge profile overrides into the main config."""
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if (
            isinstance(value, dict)
            and isinstance(merged.get(key), dict)
        ):
            merged[key] = _deep_merge_config(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _config_search_dirs(config_path: Optional[str] = None) -> list[Path]:
    """Return private-first config directories, followed by the public default."""
    public_file = Path(
        config_path or os.environ.get("CONFIG_PATH", "config/config.yaml")
    ).resolve()
    private_dir_value = os.environ.get("RAVENIS_PRIVATE_CONFIG_DIR", "").strip()
    private_file_value = os.environ.get("RAVENIS_PRIVATE_CONFIG", "").strip()
    candidates = []
    if private_dir_value:
        candidates.append(Path(private_dir_value).resolve())
    elif private_file_value:
        candidates.append(Path(private_file_value).resolve().parent)
    candidates.append(public_file.parent)
    result = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result


def resolve_config_resource(relative_path: str, config_path: Optional[str] = None) -> Path:
    """Resolve a runtime resource using private-first fallback without copying files."""
    requested = Path(relative_path)
    if requested.is_absolute():
        return requested
    for directory in _config_search_dirs(config_path):
        candidate = directory / requested
        if candidate.exists():
            return candidate
    return _config_search_dirs(config_path)[-1] / requested


def _read_yaml_mapping(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def _load_profile_override(config_path: str) -> Dict[str, Any]:
    """Load config/profiles/<DAILYANA_PROFILE>.yaml if requested."""
    profile = _get_env_str("DAILYANA_PROFILE")
    if not profile:
        return {}

    safe_profile = "".join(ch for ch in profile if ch.isalnum() or ch in ("-", "_"))
    if not safe_profile:
        print(f"[配置] 忽略无效 DAILYANA_PROFILE: {profile}")
        return {}

    for directory in _config_search_dirs(config_path):
        profile_path = directory / "profiles" / f"{safe_profile}.yaml"
        if profile_path.exists():
            data = _read_yaml_mapping(profile_path)
            print(f"[config] Loaded profile: {profile_path}")
            return data
    print(f"[config] Profile not found: {safe_profile}; using merged base config")
    return {}


def _load_content_categories(config_path: str) -> Dict[str, Any]:
    """Load config/content_categories.yaml if present."""
    category_path = resolve_config_resource("content_categories.yaml", config_path)
    if not category_path.exists():
        return {}
    with open(category_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("categories", {}) or {}


def _apply_content_category_filter(config_data: Dict[str, Any], config_path: str) -> Dict[str, Any]:
    """Filter platforms, RSS feeds, and keyword groups by selected content categories."""
    content_config = config_data.get("content", {})
    if "selected_categories" not in content_config:
        return config_data

    selected = content_config.get("selected_categories", [])
    selected = [str(x).strip() for x in selected if str(x).strip()]

    categories = _load_content_categories(config_path)
    if not categories:
        print("[分类] content_categories.yaml 未找到或为空，跳过分类过滤")
        return config_data

    allowed_platforms = set()
    allowed_rss = set()
    allowed_keywords = set()
    valid_selected = []
    for key in selected:
        category = categories.get(key)
        if not isinstance(category, dict):
            print(f"[分类] 忽略未知分类: {key}")
            continue
        valid_selected.append(key)
        allowed_platforms.update(category.get("platforms", []) or [])
        allowed_rss.update(category.get("rss_feeds", []) or [])
        allowed_keywords.update(category.get("keyword_groups", []) or [])

    filtered = deepcopy(config_data)
    sources = filtered.get("platforms", {}).get("sources", []) or []
    filtered.setdefault("platforms", {})["sources"] = [
        item for item in sources if item.get("id") in allowed_platforms
    ]
    feeds = filtered.get("rss", {}).get("feeds", []) or []
    filtered.setdefault("rss", {})["feeds"] = [
        item for item in feeds if item.get("id") in allowed_rss
    ]

    filtered["_selected_content_categories"] = valid_selected
    filtered["_selected_keyword_groups"] = sorted(allowed_keywords)
    print(
        "[分类] 已启用分类: "
        + ", ".join(valid_selected)
        + f"；平台 {len(filtered.get('platforms', {}).get('sources', []))} 个，"
        + f"RSS {len(filtered.get('rss', {}).get('feeds', []))} 个，"
        + f"关键词组 {len(allowed_keywords)} 个"
    )
    return filtered


def _load_app_config(config_data: Dict) -> Dict:
    """加载应用配置"""
    app_config = config_data.get("app", {})
    advanced = config_data.get("advanced", {})
    return {
        "VERSION_CHECK_URL": advanced.get("version_check_url", ""),
        "CONFIGS_VERSION_CHECK_URL": advanced.get("configs_version_check_url", ""),
        "SHOW_VERSION_UPDATE": app_config.get("show_version_update", True),
        "TIMEZONE": _get_env_str("TIMEZONE") or app_config.get("timezone", DEFAULT_TIMEZONE),
        "DEBUG": _get_env_bool("DEBUG") if _get_env_bool("DEBUG") is not None else advanced.get("debug", False),
    }


def _load_crawler_config(config_data: Dict) -> Dict:
    """加载爬虫配置"""
    advanced = config_data.get("advanced", {})
    crawler_config = advanced.get("crawler", {})
    platforms_config = config_data.get("platforms", {})
    return {
        "REQUEST_INTERVAL": crawler_config.get("request_interval", 100),
        "USE_PROXY": crawler_config.get("use_proxy", False),
        "DEFAULT_PROXY": crawler_config.get("default_proxy", ""),
        "ENABLE_CRAWLER": platforms_config.get("enabled", True),
        "PLATFORMS_API_URL": _get_env_str("PLATFORMS_API_URL") or platforms_config.get("api_url", ""),
    }


def _load_report_config(config_data: Dict) -> Dict:
    """加载报告配置"""
    report_config = config_data.get("report", {})

    # 环境变量覆盖
    sort_by_position_env = _get_env_bool("SORT_BY_POSITION_FIRST")
    max_news_env = _get_env_int("MAX_NEWS_PER_KEYWORD")

    return {
        "REPORT_MODE": report_config.get("mode", "daily"),
        "DISPLAY_MODE": report_config.get("display_mode", "keyword"),
        "RANK_THRESHOLD": report_config.get("rank_threshold", 10),
        "SORT_BY_POSITION_FIRST": sort_by_position_env if sort_by_position_env is not None else report_config.get("sort_by_position_first", False),
        "MAX_NEWS_PER_KEYWORD": max_news_env or report_config.get("max_news_per_keyword", 0),
    }


def _load_notification_config(config_data: Dict) -> Dict:
    """加载通知配置"""
    notification = config_data.get("notification", {})
    advanced = config_data.get("advanced", {})
    batch_size = advanced.get("batch_size", {})
    intelligence_push = dict(config_data.get("intelligence_push", {}))
    intelligence_enabled_env = _get_env_bool("INTELLIGENCE_PUSH_ENABLED")
    if intelligence_enabled_env is not None:
        intelligence_push["enabled"] = intelligence_enabled_env
    intelligence_push.setdefault("slots", config_data.get("slots", {}))
    intelligence_push.setdefault("categories", config_data.get("categories", []))
    intelligence_push.setdefault("intents", config_data.get("intents", []))
    intelligence_push.setdefault("scoring", config_data.get("scoring", {}))
    intelligence_push.setdefault("rules", config_data.get("intelligence_rules", {}))
    intelligence_storage = dict(config_data.get("storage", {}))
    intelligence_storage.update(intelligence_push.get("storage", {}))
    intelligence_push["storage"] = intelligence_storage

    return {
        "ENABLE_NOTIFICATION": notification.get("enabled", True),
        "MESSAGE_BATCH_SIZE": batch_size.get("default", 4000),
        "DINGTALK_BATCH_SIZE": batch_size.get("dingtalk", 20000),
        "FEISHU_BATCH_SIZE": batch_size.get("feishu", 29000),
        "BARK_BATCH_SIZE": batch_size.get("bark", 3600),
        "SLACK_BATCH_SIZE": batch_size.get("slack", 4000),
        "BATCH_SEND_INTERVAL": advanced.get("batch_send_interval", 1.0),
        "FEISHU_MESSAGE_SEPARATOR": advanced.get("feishu_message_separator", "---"),
        "MAX_ACCOUNTS_PER_CHANNEL": _get_env_int("MAX_ACCOUNTS_PER_CHANNEL") or advanced.get("max_accounts_per_channel", 3),
        "MESSAGE_PLAN": notification.get("message_plan", {}),
        "INTELLIGENCE_PUSH": intelligence_push,
    }


def _load_schedule_config(config_data: Dict) -> Dict:
    """
    加载统一调度配置

    从 config.yaml 的 schedule 段读取，支持环境变量覆盖。
    """
    schedule = config_data.get("schedule", {})

    # 环境变量覆盖
    enabled_env = _get_env_bool("SCHEDULE_ENABLED")
    preset_env = _get_env_str("SCHEDULE_PRESET")

    enabled = enabled_env if enabled_env is not None else schedule.get("enabled", False)
    preset = preset_env or schedule.get("preset", "always_on")

    return {
        "enabled": enabled,
        "preset": preset,
    }


def _load_timeline_data(config_dir: str = "config") -> Dict:
    """
    加载 timeline.yaml

    Args:
        config_dir: 配置目录路径

    Returns:
        timeline.yaml 的完整数据，找不到时返回空模板
    """
    timeline_path = resolve_config_resource(
        "timeline.yaml", str(Path(config_dir) / "config.yaml")
    )
    if not timeline_path.exists():
        print(f"[调度] timeline.yaml 未找到: {timeline_path}，使用空模板")
        return {
            "presets": {},
            "custom": {
                "default": {
                    "collect": True,
                    "analyze": False,
                    "push": False,
                    "report_mode": "current",
                    "ai_mode": "follow_report",
                    "once": {"analyze": False, "push": False},
                },
                "periods": {},
                "day_plans": {"all_day": {"periods": []}},
                "week_map": {i: "all_day" for i in range(1, 8)},
            },
        }

    with open(timeline_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    print(f"[调度] timeline.yaml 加载成功: {timeline_path}")
    return data or {}


def _load_weight_config(config_data: Dict) -> Dict:
    """加载权重配置"""
    advanced = config_data.get("advanced", {})
    weight = advanced.get("weight", {})
    return {
        "RANK_WEIGHT": weight.get("rank", 0.6),
        "FREQUENCY_WEIGHT": weight.get("frequency", 0.3),
        "HOTNESS_WEIGHT": weight.get("hotness", 0.1),
    }


def _load_rss_config(config_data: Dict) -> Dict:
    """加载 RSS 配置"""
    rss = config_data.get("rss", {})
    advanced = config_data.get("advanced", {})
    advanced_rss = advanced.get("rss", {})
    advanced_crawler = advanced.get("crawler", {})

    # RSS 代理配置：优先使用 RSS 专属代理，否则复用 crawler 的 default_proxy
    rss_proxy_url = advanced_rss.get("proxy_url", "") or advanced_crawler.get("default_proxy", "")

    # 新鲜度过滤配置
    freshness_filter = rss.get("freshness_filter", {})

    # 验证并设置 max_age_days 默认值
    raw_max_age = freshness_filter.get("max_age_days", 3)
    try:
        max_age_days = int(raw_max_age)
        if max_age_days < 0:
            print(f"[警告] RSS freshness_filter.max_age_days 为负数 ({max_age_days})，使用默认值 3")
            max_age_days = 3
    except (ValueError, TypeError):
        print(f"[警告] RSS freshness_filter.max_age_days 格式错误 ({raw_max_age})，使用默认值 3")
        max_age_days = 3

    feeds = deepcopy(rss.get("feeds", []))
    rsshub_base_url = (
        os.environ.get("RSSHUB_BASE_URL", "").strip()
        or str(rss.get("rsshub_base_url", "")).strip()
    ).rstrip("/")
    if rsshub_base_url and not rsshub_base_url.startswith(("http://", "https://")):
        print("[警告] RSSHUB_BASE_URL 必须使用 http:// 或 https://，忽略该配置")
        rsshub_base_url = ""
    if rsshub_base_url:
        replaced = 0
        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            url = str(feed.get("url", ""))
            for official_base in ("https://rsshub.app", "http://rsshub.app"):
                if url == official_base or url.startswith(official_base + "/"):
                    feed["url"] = rsshub_base_url + url[len(official_base) :]
                    replaced += 1
                    break
        if replaced:
            print(f"[RSS] 使用自定义 RSSHub 实例，已重写 {replaced} 个订阅地址")

    return {
        "ENABLED": rss.get("enabled", False),
        "REQUEST_INTERVAL": advanced_rss.get("request_interval", 2000),
        "TIMEOUT": advanced_rss.get("timeout", 15),
        "USE_PROXY": advanced_rss.get("use_proxy", False),
        "PROXY_URL": rss_proxy_url,
        "FEEDS": feeds,
        "FRESHNESS_FILTER": {
            "ENABLED": freshness_filter.get("enabled", True),  # 默认启用
            "MAX_AGE_DAYS": max_age_days,
        },
    }


def _load_display_config(config_data: Dict) -> Dict:
    """加载推送内容显示配置"""
    display = config_data.get("display", {})
    regions = display.get("regions", {})
    standalone = display.get("standalone", {})

    # 默认区域顺序
    default_region_order = ["hotlist", "rss", "new_items", "standalone", "ai_analysis"]
    region_order = display.get("region_order", default_region_order)

    # 验证 region_order 中的值是否合法
    valid_regions = {"hotlist", "rss", "new_items", "standalone", "ai_analysis"}
    region_order = [r for r in region_order if r in valid_regions]

    # 如果过滤后为空，使用默认顺序
    if not region_order:
        region_order = default_region_order

    return {
        # 区域显示顺序
        "REGION_ORDER": region_order,
        # 区域开关
        "REGIONS": {
            "HOTLIST": regions.get("hotlist", True),
            "NEW_ITEMS": regions.get("new_items", True),
            "RSS": regions.get("rss", True),
            "STANDALONE": regions.get("standalone", False),
            "AI_ANALYSIS": regions.get("ai_analysis", True),
        },
        # 独立展示区配置
        "STANDALONE": {
            "PLATFORMS": standalone.get("platforms", []),
            "RSS_FEEDS": standalone.get("rss_feeds", []),
            "MAX_ITEMS": standalone.get("max_items", 20),
        },
    }


def _load_source_digest_config(config_data: Dict) -> Dict:
    """加载多源 AI 整合摘要配置。当前默认关闭，供后续推送整合使用。"""
    digest = config_data.get("source_digest", {})
    if digest:
        print(
            "[config] DEPRECATION: source_digest is not implemented and is ignored; "
            "it will be removed after this compatibility release"
        )
    return {
        "ENABLED": digest.get("enabled", False),
        "MAX_ITEMS": int(digest.get("max_items", 30) or 30),
        "DEDUPE": digest.get("dedupe", True),
        "INCLUDE_HOTLIST": digest.get("include_hotlist", True),
        "INCLUDE_RSS": digest.get("include_rss", True),
    }


def _load_ai_config(config_data: Dict) -> Dict:
    """加载 AI 模型配置（LiteLLM 格式）"""
    ai_config = config_data.get("ai", {})

    timeout_env = _get_env_int_or_none("AI_TIMEOUT")

    return {
        # LiteLLM 核心配置
        "MODEL": _get_env_str("AI_MODEL") or ai_config.get("model", ""),
        "API_KEY": _get_env_str("AI_API_KEY") or ai_config.get("api_key", ""),
        "API_BASE": _get_env_str("AI_API_BASE") or ai_config.get("api_base", ""),

        # 生成参数
        "TIMEOUT": timeout_env if timeout_env is not None else ai_config.get("timeout", 120),
        "TEMPERATURE": ai_config.get("temperature", 1.0),
        "MAX_TOKENS": ai_config.get("max_tokens", 5000),

        # LiteLLM 高级选项
        "NUM_RETRIES": ai_config.get("num_retries", 2),
        "FALLBACK_MODELS": ai_config.get("fallback_models", []),
        "EXTRA_PARAMS": ai_config.get("extra_params", {}),
    }


def _load_ai_analysis_config(config_data: Dict) -> Dict:
    """加载 AI 分析配置（功能配置，模型配置见 _load_ai_config）"""
    ai_config = config_data.get("ai_analysis", {})

    enabled_env = _get_env_bool("AI_ANALYSIS_ENABLED")

    return {
        "ENABLED": enabled_env if enabled_env is not None else ai_config.get("enabled", False),
        "LANGUAGE": ai_config.get("language", "Chinese"),
        "PROMPT_FILE": ai_config.get("prompt_file", "ai_analysis_prompt.txt"),
        "MODE": ai_config.get("mode", "follow_report"),
        "MAX_NEWS_FOR_ANALYSIS": ai_config.get("max_news_for_analysis", 50),
        "INCLUDE_RSS": ai_config.get("include_rss", True),
        "INCLUDE_RANK_TIMELINE": ai_config.get("include_rank_timeline", False),
        "INCLUDE_STANDALONE": ai_config.get("include_standalone", False),
    }


def _load_ai_translation_config(config_data: Dict) -> Dict:
    """加载 AI 翻译配置（功能配置，模型配置见 _load_ai_config）"""
    trans_config = config_data.get("ai_translation", {})

    enabled_env = _get_env_bool("AI_TRANSLATION_ENABLED")

    scope = trans_config.get("scope", {})

    return {
        "ENABLED": enabled_env if enabled_env is not None else trans_config.get("enabled", False),
        "LANGUAGE": _get_env_str("AI_TRANSLATION_LANGUAGE") or trans_config.get("language", "English"),
        "PROMPT_FILE": trans_config.get("prompt_file", "ai_translation_prompt.txt"),
        "SCOPE": {
            "HOTLIST": scope.get("hotlist", True),
            "RSS": scope.get("rss", True),
            "STANDALONE": scope.get("standalone", True),
        },
    }


def _load_ai_filter_config(config_data: Dict) -> Dict:
    """加载 AI 智能筛选配置（由 filter.method 控制是否启用）"""
    ai_filter = config_data.get("ai_filter", {})

    return {
        "BATCH_SIZE": ai_filter.get("batch_size", 200),
        "BATCH_INTERVAL": ai_filter.get("batch_interval", 5),
        "INTERESTS_FILE": ai_filter.get("interests_file"),  # None = 使用默认 config/ai_interests.txt
        "PROMPT_FILE": ai_filter.get("prompt_file", "prompt.txt"),
        "EXTRACT_PROMPT_FILE": ai_filter.get("extract_prompt_file", "extract_prompt.txt"),
        "UPDATE_TAGS_PROMPT_FILE": ai_filter.get("update_tags_prompt_file", "update_tags_prompt.txt"),
        "RECLASSIFY_THRESHOLD": ai_filter.get("reclassify_threshold", 0.6),
        "MIN_SCORE": float(ai_filter.get("min_score", 0)),
    }


def _load_filter_config(config_data: Dict) -> Dict:
    """加载筛选策略配置"""
    filter_cfg = config_data.get("filter", {})

    # 环境变量兼容：AI_FILTER_ENABLED=true → method=ai
    env_ai_filter = _get_env_bool("AI_FILTER_ENABLED")

    method = filter_cfg.get("method", "keyword")
    if env_ai_filter is True:
        method = "ai"

    # 兼容旧配置：如果 ai_filter.enabled=true 且未显式设置 filter.method
    if method == "keyword" and not filter_cfg.get("method"):
        ai_filter = config_data.get("ai_filter", {})
        if ai_filter.get("enabled", False):
            method = "ai"

    return {
        "METHOD": method,  # "keyword" | "ai"
        "PRIORITY_SORT_ENABLED": filter_cfg.get("priority_sort_enabled", False),  # AI 模式标签优先级排序开关
    }


def _load_storage_config(config_data: Dict) -> Dict:
    """加载存储配置"""
    storage = config_data.get("storage", {})
    formats = storage.get("formats", {})
    local = storage.get("local", {})
    remote = storage.get("remote", {})
    pull = storage.get("pull", {})

    txt_enabled_env = _get_env_bool("STORAGE_TXT_ENABLED")
    html_enabled_env = _get_env_bool("STORAGE_HTML_ENABLED")
    pull_enabled_env = _get_env_bool("PULL_ENABLED")

    return {
        "BACKEND": _get_env_str("STORAGE_BACKEND") or storage.get("backend", "auto"),
        "FORMATS": {
            "SQLITE": formats.get("sqlite", True),
            "TXT": txt_enabled_env if txt_enabled_env is not None else formats.get("txt", True),
            "HTML": html_enabled_env if html_enabled_env is not None else formats.get("html", True),
        },
        "LOCAL": {
            "DATA_DIR": local.get("data_dir", "output"),
            "RETENTION_DAYS": _get_env_int("LOCAL_RETENTION_DAYS") or local.get("retention_days", 0),
        },
        "REMOTE": {
            "ENDPOINT_URL": _get_env_str("S3_ENDPOINT_URL") or remote.get("endpoint_url", ""),
            "BUCKET_NAME": _get_env_str("S3_BUCKET_NAME") or remote.get("bucket_name", ""),
            "ACCESS_KEY_ID": _get_env_str("S3_ACCESS_KEY_ID") or remote.get("access_key_id", ""),
            "SECRET_ACCESS_KEY": _get_env_str("S3_SECRET_ACCESS_KEY") or remote.get("secret_access_key", ""),
            "REGION": _get_env_str("S3_REGION") or remote.get("region", ""),
            "RETENTION_DAYS": _get_env_int("REMOTE_RETENTION_DAYS") or remote.get("retention_days", 0),
        },
        "PULL": {
            "ENABLED": pull_enabled_env if pull_enabled_env is not None else pull.get("enabled", False),
            "DAYS": _get_env_int("PULL_DAYS") or pull.get("days", 7),
        },
    }


def _load_webhook_config(config_data: Dict) -> Dict:
    """加载 Webhook 配置"""
    notification = config_data.get("notification", {})
    channels = notification.get("channels", {})

    # 各渠道配置
    feishu = channels.get("feishu", {})
    dingtalk = channels.get("dingtalk", {})
    wework = channels.get("wework", {})
    telegram = channels.get("telegram", {})
    email = channels.get("email", {})
    ntfy = channels.get("ntfy", {})
    bark = channels.get("bark", {})
    slack = channels.get("slack", {})
    generic = channels.get("generic_webhook", {})

    return {
        # 飞书
        "FEISHU_WEBHOOK_URL": _get_env_str("FEISHU_WEBHOOK_URL") or feishu.get("webhook_url", ""),
        # 钉钉
        "DINGTALK_WEBHOOK_URL": _get_env_str("DINGTALK_WEBHOOK_URL") or dingtalk.get("webhook_url", ""),
        # 企业微信
        "WEWORK_WEBHOOK_URL": _get_env_str("WEWORK_WEBHOOK_URL") or wework.get("webhook_url", ""),
        "WEWORK_MSG_TYPE": _get_env_str("WEWORK_MSG_TYPE") or wework.get("msg_type", "markdown"),
        # Telegram
        "TELEGRAM_BOT_TOKEN": _get_env_str("TELEGRAM_BOT_TOKEN") or telegram.get("bot_token", ""),
        "TELEGRAM_CHAT_ID": _get_env_str("TELEGRAM_CHAT_ID") or telegram.get("chat_id", ""),
        # 邮件
        "EMAIL_FROM": _get_env_str("EMAIL_FROM") or email.get("from", ""),
        "EMAIL_PASSWORD": _get_env_str("EMAIL_PASSWORD") or email.get("password", ""),
        "EMAIL_TO": _get_env_str("EMAIL_TO") or email.get("to", ""),
        "EMAIL_SMTP_SERVER": _get_env_str("EMAIL_SMTP_SERVER") or email.get("smtp_server", ""),
        "EMAIL_SMTP_PORT": _get_env_str("EMAIL_SMTP_PORT") or email.get("smtp_port", ""),
        # ntfy
        "NTFY_SERVER_URL": _get_env_str("NTFY_SERVER_URL") or ntfy.get("server_url") or "https://ntfy.sh",
        "NTFY_TOPIC": _get_env_str("NTFY_TOPIC") or ntfy.get("topic", ""),
        "NTFY_TOKEN": _get_env_str("NTFY_TOKEN") or ntfy.get("token", ""),
        # Bark
        "BARK_URL": _get_env_str("BARK_URL") or bark.get("url", ""),
        # Slack
        "SLACK_WEBHOOK_URL": _get_env_str("SLACK_WEBHOOK_URL") or slack.get("webhook_url", ""),
        # 通用 Webhook
        "GENERIC_WEBHOOK_URL": _get_env_str("GENERIC_WEBHOOK_URL") or generic.get("webhook_url", ""),
        "GENERIC_WEBHOOK_TEMPLATE": _get_env_str("GENERIC_WEBHOOK_TEMPLATE") or generic.get("payload_template", ""),
    }


def _print_notification_sources(config: Dict) -> None:
    """打印通知渠道配置来源信息"""
    notification_sources = []
    max_accounts = config["MAX_ACCOUNTS_PER_CHANNEL"]

    if config["FEISHU_WEBHOOK_URL"]:
        accounts = parse_multi_account_config(config["FEISHU_WEBHOOK_URL"])
        count = min(len(accounts), max_accounts)
        source = "环境变量" if os.environ.get("FEISHU_WEBHOOK_URL") else "配置文件"
        notification_sources.append(f"飞书({source}, {count}个账号)")

    if config["DINGTALK_WEBHOOK_URL"]:
        accounts = parse_multi_account_config(config["DINGTALK_WEBHOOK_URL"])
        count = min(len(accounts), max_accounts)
        source = "环境变量" if os.environ.get("DINGTALK_WEBHOOK_URL") else "配置文件"
        notification_sources.append(f"钉钉({source}, {count}个账号)")

    if config["WEWORK_WEBHOOK_URL"]:
        accounts = parse_multi_account_config(config["WEWORK_WEBHOOK_URL"])
        count = min(len(accounts), max_accounts)
        source = "环境变量" if os.environ.get("WEWORK_WEBHOOK_URL") else "配置文件"
        notification_sources.append(f"企业微信({source}, {count}个账号)")

    if config["TELEGRAM_BOT_TOKEN"] and config["TELEGRAM_CHAT_ID"]:
        tokens = parse_multi_account_config(config["TELEGRAM_BOT_TOKEN"])
        chat_ids = parse_multi_account_config(config["TELEGRAM_CHAT_ID"])
        valid, count = validate_paired_configs(
            {"bot_token": tokens, "chat_id": chat_ids},
            "Telegram",
            required_keys=["bot_token", "chat_id"]
        )
        if valid and count > 0:
            count = min(count, max_accounts)
            token_source = "环境变量" if os.environ.get("TELEGRAM_BOT_TOKEN") else "配置文件"
            notification_sources.append(f"Telegram({token_source}, {count}个账号)")

    if config["EMAIL_FROM"] and config["EMAIL_PASSWORD"] and config["EMAIL_TO"]:
        from_source = "环境变量" if os.environ.get("EMAIL_FROM") else "配置文件"
        notification_sources.append(f"邮件({from_source})")

    if config["NTFY_SERVER_URL"] and config["NTFY_TOPIC"]:
        topics = parse_multi_account_config(config["NTFY_TOPIC"])
        tokens = parse_multi_account_config(config["NTFY_TOKEN"])
        if tokens:
            valid, count = validate_paired_configs(
                {"topic": topics, "token": tokens},
                "ntfy"
            )
            if valid and count > 0:
                count = min(count, max_accounts)
                server_source = "环境变量" if os.environ.get("NTFY_SERVER_URL") else "配置文件"
                notification_sources.append(f"ntfy({server_source}, {count}个账号)")
        else:
            count = min(len(topics), max_accounts)
            server_source = "环境变量" if os.environ.get("NTFY_SERVER_URL") else "配置文件"
            notification_sources.append(f"ntfy({server_source}, {count}个账号)")

    if config["BARK_URL"]:
        accounts = parse_multi_account_config(config["BARK_URL"])
        count = min(len(accounts), max_accounts)
        bark_source = "环境变量" if os.environ.get("BARK_URL") else "配置文件"
        notification_sources.append(f"Bark({bark_source}, {count}个账号)")

    if config["SLACK_WEBHOOK_URL"]:
        accounts = parse_multi_account_config(config["SLACK_WEBHOOK_URL"])
        count = min(len(accounts), max_accounts)
        slack_source = "环境变量" if os.environ.get("SLACK_WEBHOOK_URL") else "配置文件"
        notification_sources.append(f"Slack({slack_source}, {count}个账号)")

    if config.get("GENERIC_WEBHOOK_URL"):
        accounts = parse_multi_account_config(config["GENERIC_WEBHOOK_URL"])
        count = min(len(accounts), max_accounts)
        source = "环境变量" if os.environ.get("GENERIC_WEBHOOK_URL") else "配置文件"
        notification_sources.append(f"通用Webhook({source}, {count}个账号)")

    if notification_sources:
        print(f"通知渠道配置来源: {', '.join(notification_sources)}")
        print(f"每个渠道最大账号数: {max_accounts}")
    else:
        print("未配置任何通知渠道")


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    加载配置文件

    Args:
        config_path: 配置文件路径，默认从环境变量 CONFIG_PATH 获取或使用 config/config.yaml

    Returns:
        包含所有配置的字典

    Raises:
        FileNotFoundError: 配置文件不存在
    """
    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")

    if not Path(config_path).exists():
        raise FileNotFoundError(f"配置文件 {config_path} 不存在")

    config_data = _read_yaml_mapping(Path(config_path))
    print(f"[config] Loaded public defaults: {Path(config_path).resolve()}")

    private_path_value = os.environ.get("RAVENIS_PRIVATE_CONFIG", "").strip()
    if private_path_value:
        private_path = Path(private_path_value).resolve()
        if not private_path.exists():
            raise FileNotFoundError(f"Private config does not exist: {private_path}")
        if private_path != Path(config_path).resolve():
            config_data = _deep_merge_config(
                config_data, _read_yaml_mapping(private_path)
            )
        print(f"[config] Applied private override: {private_path}")
    profile_override = _load_profile_override(config_path)
    if profile_override:
        config_data = _deep_merge_config(config_data, profile_override)
    config_data = _apply_content_category_filter(config_data, config_path)
    rules_path = resolve_config_resource("intelligence_rules.yaml", config_path)
    if rules_path.exists():
        config_data["intelligence_rules"] = _read_yaml_mapping(rules_path)
        print(f"[config] Loaded intelligence rules: {rules_path}")

    # 合并所有配置
    config = {
        "CONFIG_SOURCES": {
            "public": str(Path(config_path).resolve()),
            "private": str(Path(private_path_value).resolve()) if private_path_value else "",
            "profile": _get_env_str("DAILYANA_PROFILE"),
            "resource_dirs": [str(path) for path in _config_search_dirs(config_path)],
        }
    }

    # 应用配置
    config.update(_load_app_config(config_data))

    # 爬虫配置
    config.update(_load_crawler_config(config_data))

    # 报告配置
    config.update(_load_report_config(config_data))

    # 通知配置
    config.update(_load_notification_config(config_data))

    # 统一调度配置
    config["SCHEDULE"] = _load_schedule_config(config_data)
    config["_TIMELINE_DATA"] = _load_timeline_data(
        str(Path(config_path).parent) if config_path else "config"
    )

    # 权重配置
    config["WEIGHT_CONFIG"] = _load_weight_config(config_data)

    # 平台配置
    platforms_config = config_data.get("platforms", {})
    config["PLATFORMS"] = [p for p in platforms_config.get("sources", []) if p.get("enabled", True)]
    config["SELECTED_CONTENT_CATEGORIES"] = config_data.get("_selected_content_categories", [])
    config["SELECTED_KEYWORD_GROUPS"] = config_data.get("_selected_keyword_groups", [])

    # RSS 配置
    config["RSS"] = _load_rss_config(config_data)

    # AI 模型共享配置
    config["AI"] = _load_ai_config(config_data)

    # AI 分析配置
    config["AI_ANALYSIS"] = _load_ai_analysis_config(config_data)

    # AI 翻译配置
    config["AI_TRANSLATION"] = _load_ai_translation_config(config_data)

    # AI 智能筛选配置
    config["AI_FILTER"] = _load_ai_filter_config(config_data)

    # 筛选策略配置
    config["FILTER"] = _load_filter_config(config_data)

    # 推送内容显示配置
    config["DISPLAY"] = _load_display_config(config_data)

    # 多源 AI 整合摘要配置
    config["SOURCE_DIGEST"] = _load_source_digest_config(config_data)

    # 存储配置
    config["STORAGE"] = _load_storage_config(config_data)

    # Webhook 配置
    config.update(_load_webhook_config(config_data))

    # 打印通知渠道配置来源
    _print_notification_sources(config)

    return config
