#!/usr/bin/env python3
# coding=utf-8
"""
Local Ravenis Core configuration console.

Run from the repository root:
    python tools/config_ui.py

Then open:
    http://127.0.0.1:8765
"""

from __future__ import annotations

import html
import json
import re
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config.yaml"
KEYWORDS_PATH = ROOT / "config" / "frequency_words.txt"
PROFILE_DIR = ROOT / "config" / "profiles"
PROFILE_NAMES = ("work", "relax")


def read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=1000)


def deep_merge(base: dict, override: dict) -> dict:
    merged = deepcopy(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_profile(name: str) -> dict:
    if name not in PROFILE_NAMES:
        raise ValueError("unknown profile")
    return read_yaml(PROFILE_DIR / f"{name}.yaml")


def save_profile(name: str, data: dict) -> None:
    if name not in PROFILE_NAMES:
        raise ValueError("unknown profile")
    write_yaml(PROFILE_DIR / f"{name}.yaml", data)


def load_keywords_text() -> str:
    return KEYWORDS_PATH.read_text(encoding="utf-8")


def save_keywords_text(text: str) -> None:
    KEYWORDS_PATH.write_text(text, encoding="utf-8", newline="\n")


def split_keyword_blocks() -> tuple[str, list[dict]]:
    text = load_keywords_text()
    lines = text.splitlines()
    marker_index = None
    for i, line in enumerate(lines):
        if line.strip() == "[WORD_GROUPS]":
            marker_index = i
            break

    if marker_index is None:
        return text.rstrip() + "\n\n[WORD_GROUPS]\n", []

    preamble = "\n".join(lines[: marker_index + 1]).rstrip() + "\n\n"
    body_lines = lines[marker_index + 1 :]

    blocks = []
    current: list[str] = []
    for line in body_lines:
        if line.strip():
            current.append(line)
        else:
            if current:
                blocks.append(make_keyword_block(len(blocks), "\n".join(current)))
                current = []
    if current:
        blocks.append(make_keyword_block(len(blocks), "\n".join(current)))
    return preamble, blocks


def make_keyword_block(index: int, text: str) -> dict:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    visible_lines = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    is_comment = not visible_lines
    title = "注释"
    if visible_lines:
        first = visible_lines[0].strip()
        alias = re.search(r"=>\s*(.+)$", first)
        if first.startswith("[") and "]" in first:
            title = first[1 : first.find("]")]
        elif alias:
            title = alias.group(1).strip()
        else:
            title = first
    return {
        "id": index,
        "type": "comment" if is_comment else "group",
        "title": title,
        "text": "\n".join(lines).strip(),
    }


def get_global_filters() -> list[str]:
    text = load_keywords_text()
    lines = text.splitlines()
    in_filter = False
    filters = []
    for line in lines:
        stripped = line.strip()
        if stripped == "[GLOBAL_FILTER]":
            in_filter = True
            continue
        if stripped == "[WORD_GROUPS]":
            break
        if in_filter and stripped and not stripped.startswith("#"):
            filters.append(stripped)
    return filters


def write_keyword_blocks(preamble: str, blocks: list[dict]) -> None:
    parts = [preamble.rstrip(), ""]
    for block in blocks:
        text = (block.get("text") or "").strip()
        if text:
            parts.append(text)
            parts.append("")
    save_keywords_text("\n".join(parts).rstrip() + "\n")


def get_keyword_groups() -> list[dict]:
    _, blocks = split_keyword_blocks()
    return [b for b in blocks if b["type"] == "group"]


def upsert_keyword(block_id: int | None, text: str) -> dict:
    clean = text.strip()
    if not clean:
        raise ValueError("keyword block cannot be empty")
    preamble, blocks = split_keyword_blocks()
    if block_id is None:
        blocks.append(make_keyword_block(len(blocks), clean))
    else:
        found = False
        for i, block in enumerate(blocks):
            if block["id"] == block_id and block["type"] == "group":
                blocks[i] = make_keyword_block(block_id, clean)
                found = True
                break
        if not found:
            raise ValueError("keyword block not found")
    write_keyword_blocks(preamble, blocks)
    return {"groups": get_keyword_groups()}


def delete_keyword(block_id: int) -> dict:
    preamble, blocks = split_keyword_blocks()
    blocks = [b for b in blocks if not (b["id"] == block_id and b["type"] == "group")]
    for i, block in enumerate(blocks):
        block["id"] = i
    write_keyword_blocks(preamble, blocks)
    return {"groups": get_keyword_groups()}


def normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


def platform_payload(data: dict) -> dict:
    item = {
        "id": (data.get("id") or "").strip(),
        "name": (data.get("name") or "").strip(),
        "expected_domain": (data.get("expected_domain") or "").strip(),
    }
    if "enabled" in data:
        item["enabled"] = normalize_bool(data.get("enabled"))
    if not item["id"] or not item["name"]:
        raise ValueError("platform id and name are required")
    return item


def rss_payload(data: dict) -> dict:
    item = {
        "id": (data.get("id") or "").strip(),
        "name": (data.get("name") or "").strip(),
        "url": (data.get("url") or "").strip(),
    }
    if "enabled" in data:
        item["enabled"] = normalize_bool(data.get("enabled"))
    max_age = data.get("max_age_days")
    if max_age not in (None, ""):
        item["max_age_days"] = int(max_age)
    if not item["id"] or not item["name"] or not item["url"]:
        raise ValueError("rss id, name and url are required")
    return item


def upsert_list_item(section: str, list_key: str, item: dict) -> dict:
    config = read_yaml(CONFIG_PATH)
    section_data = config.setdefault(section, {})
    items = section_data.setdefault(list_key, [])
    replaced = False
    for i, current in enumerate(items):
        if current.get("id") == item["id"]:
            items[i] = item
            replaced = True
            break
    if not replaced:
        items.append(item)
    write_yaml(CONFIG_PATH, config)
    return state_payload()


def delete_list_item(section: str, list_key: str, item_id: str) -> dict:
    config = read_yaml(CONFIG_PATH)
    items = config.setdefault(section, {}).setdefault(list_key, [])
    config[section][list_key] = [item for item in items if item.get("id") != item_id]
    write_yaml(CONFIG_PATH, config)
    return state_payload()


def save_profile_settings(data: dict) -> dict:
    profile = data.get("profile")
    if profile not in PROFILE_NAMES:
        raise ValueError("profile must be work or relax")

    current = load_profile(profile)
    current.setdefault("display", {})
    current.setdefault("ai_analysis", {})

    display = data.get("display", {})
    if "region_order" in display:
        allowed = {"new_items", "hotlist", "rss", "standalone", "ai_analysis"}
        order = [x for x in display.get("region_order", []) if x in allowed]
        if order:
            current["display"]["region_order"] = order
    if "regions" in display:
        current["display"].setdefault("regions", {})
        for key in ("hotlist", "new_items", "rss", "standalone", "ai_analysis"):
            if key in display["regions"]:
                current["display"]["regions"][key] = normalize_bool(display["regions"][key])

    ai = data.get("ai_analysis", {})
    for key in ("enabled", "include_rss", "include_standalone", "include_rank_timeline"):
        if key in ai:
            current["ai_analysis"][key] = normalize_bool(ai[key])
    if "max_news_for_analysis" in ai and ai["max_news_for_analysis"] not in ("", None):
        current["ai_analysis"]["max_news_for_analysis"] = int(ai["max_news_for_analysis"])

    standalone = display.get("standalone")
    if isinstance(standalone, dict):
        current["display"]["standalone"] = {
            "platforms": standalone.get("platforms", []),
            "rss_feeds": standalone.get("rss_feeds", []),
            "max_items": int(standalone.get("max_items", 20) or 20),
        }

    save_profile(profile, current)
    return state_payload()


def state_payload() -> dict:
    config = read_yaml(CONFIG_PATH)
    profiles = {}
    effective = {}
    for name in PROFILE_NAMES:
        profile = load_profile(name)
        profiles[name] = profile
        effective[name] = deep_merge(config, profile)
    return {
        "platforms": config.get("platforms", {}),
        "rss": config.get("rss", {}),
        "display": config.get("display", {}),
        "ai_analysis": config.get("ai_analysis", {}),
        "profiles": profiles,
        "effective_profiles": effective,
        "keywords": get_keyword_groups(),
        "global_filters": get_global_filters(),
        "history": {
            "ready": False,
            "endpoint": "/api/history/search?q=keyword",
            "message": "数据库归档接口已预留，接入后可在这里搜索历史新闻。",
        },
    }


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ravenis Core 配置台</title>
  <style>
    :root { color-scheme: light; --bg:#f5f7fb; --panel:#fff; --line:#d9e0ea; --text:#172033; --muted:#68758a; --accent:#155eef; --danger:#d92d20; --ok:#067647; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; }
    header { height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 22px; border-bottom:1px solid var(--line); background:var(--panel); position:sticky; top:0; z-index:10; }
    h1 { font-size:18px; margin:0; }
    main { display:grid; grid-template-columns: 220px 1fr; min-height:calc(100vh - 56px); }
    nav { border-right:1px solid var(--line); padding:16px; background:#f9fbff; }
    nav button { width:100%; text-align:left; border:0; background:transparent; padding:10px 12px; border-radius:8px; color:var(--text); cursor:pointer; margin-bottom:4px; }
    nav button.active { background:#e8efff; color:#003eb3; font-weight:700; }
    section { display:none; padding:22px; }
    section.active { display:block; }
    .toolbar { display:flex; gap:10px; align-items:center; margin:0 0 16px; flex-wrap:wrap; }
    input, textarea, select { width:100%; border:1px solid var(--line); border-radius:8px; padding:9px 10px; background:white; color:var(--text); font:inherit; }
    textarea { min-height:120px; font-family: ui-monospace, Consolas, monospace; }
    .search { max-width:360px; }
    .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:14px; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }
    .card h3 { margin:0 0 10px; font-size:15px; }
    .muted { color:var(--muted); }
    .row { display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-bottom:10px; }
    .row3 { display:grid; grid-template-columns: 1fr 1fr 1fr; gap:10px; margin-bottom:10px; }
    .list { display:grid; gap:10px; }
    .item { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
    .item-head { display:flex; gap:10px; justify-content:space-between; align-items:flex-start; }
    .pill { display:inline-flex; align-items:center; gap:4px; padding:2px 8px; border-radius:999px; background:#edf2f7; color:#42526b; font-size:12px; }
    .pill.on { background:#dcfae6; color:var(--ok); }
    .pill.off { background:#fee4e2; color:var(--danger); }
    .actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
    button.primary, button.ghost, button.danger { border:1px solid var(--line); border-radius:8px; padding:8px 12px; cursor:pointer; background:white; }
    button.primary { background:var(--accent); color:white; border-color:var(--accent); }
    button.danger { color:var(--danger); }
    label.check { display:flex; align-items:center; gap:8px; margin:6px 0; }
    label.check input { width:auto; }
    .profile-tabs { display:flex; gap:8px; margin-bottom:14px; }
    .profile-tabs button.active { background:var(--accent); color:white; border-color:var(--accent); }
    .toast { color:var(--ok); min-height:20px; }
    code { background:#eef2f7; padding:2px 5px; border-radius:5px; }
    @media (max-width: 760px) { main { grid-template-columns:1fr; } nav { display:flex; overflow:auto; border-right:0; border-bottom:1px solid var(--line); } nav button { white-space:nowrap; } .row,.row3 { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Ravenis Core 配置台</h1>
    <div><span id="status" class="toast"></span></div>
  </header>
  <main>
    <nav>
      <button class="active" data-tab="sources">信息源</button>
      <button data-tab="keywords">关键词</button>
      <button data-tab="profiles">推送方案</button>
      <button data-tab="history">历史新闻</button>
    </nav>

    <section id="sources" class="active">
      <div class="toolbar">
        <input class="search" id="sourceSearch" placeholder="搜索平台/RSS id、名称、域名" />
        <button class="primary" onclick="savePlatform()">保存热榜源</button>
        <button class="primary" onclick="saveRss()">保存 RSS 源</button>
      </div>
      <div class="grid">
        <div class="card">
          <h3>添加/编辑热榜源</h3>
          <div class="row"><input id="platId" placeholder="id，如 zhihu" /><input id="platName" placeholder="显示名" /></div>
          <div class="row"><input id="platDomain" placeholder="校验域名，如 zhihu.com" /><select id="platEnabled"><option value="true">启用</option><option value="false">禁用</option></select></div>
          <p class="muted">id 相同会覆盖；删除会从 <code>platforms.sources</code> 移除。</p>
        </div>
        <div class="card">
          <h3>添加/编辑 RSS 源</h3>
          <div class="row"><input id="rssId" placeholder="id，如 hacker-news" /><input id="rssName" placeholder="显示名" /></div>
          <input id="rssUrl" placeholder="RSS URL" style="margin-bottom:10px" />
          <div class="row"><input id="rssMaxAge" placeholder="max_age_days 可空" /><select id="rssEnabled"><option value="true">启用</option><option value="false">禁用</option></select></div>
        </div>
      </div>
      <h3>热榜平台</h3><div id="platformList" class="list"></div>
      <h3>RSS 源</h3><div id="rssList" class="list"></div>
    </section>

    <section id="keywords">
      <div class="toolbar">
        <input class="search" id="keywordSearch" placeholder="搜索关键词组" />
        <button class="primary" onclick="newKeyword()">新增关键词组</button>
      </div>
      <div class="card" id="keywordEditor" style="display:none">
        <h3 id="keywordEditorTitle">关键词组</h3>
        <textarea id="keywordText" placeholder="[组名]\n关键词\n/正则/ => 别名"></textarea>
        <div class="actions">
          <button class="primary" onclick="saveKeyword()">保存</button>
          <button class="ghost" onclick="closeKeywordEditor()">取消</button>
        </div>
      </div>
      <div id="keywordList" class="list"></div>
    </section>

    <section id="profiles">
      <div class="profile-tabs">
        <button class="primary active" data-profile="work" onclick="selectProfile('work')">方案 1：工作内容</button>
        <button class="ghost" data-profile="relax" onclick="selectProfile('relax')">方案 2：休闲内容</button>
      </div>
      <div class="grid">
        <div class="card">
          <h3>推送显示区域</h3>
          <label class="check"><input type="checkbox" id="regHotlist" /> 热榜 hotlist</label>
          <label class="check"><input type="checkbox" id="regRss" /> RSS rss</label>
          <label class="check"><input type="checkbox" id="regAi" /> AI 完整分析 ai_analysis</label>
          <label class="check"><input type="checkbox" id="regNew" /> 新增热点 new_items</label>
          <label class="check"><input type="checkbox" id="regStandalone" /> 独立展示 standalone</label>
          <label>区域顺序，逗号分隔</label>
          <input id="regionOrder" />
        </div>
        <div class="card">
          <h3>AI 分析</h3>
          <label class="check"><input type="checkbox" id="aiEnabled" /> 启用 AI 分析</label>
          <label class="check"><input type="checkbox" id="aiRss" /> AI 分析包含 RSS</label>
          <label class="check"><input type="checkbox" id="aiStandalone" /> AI 分析包含 standalone</label>
          <label class="check"><input type="checkbox" id="aiTimeline" /> 包含排名轨迹</label>
          <label>最多分析新闻数</label>
          <input id="aiMaxNews" type="number" min="1" />
        </div>
        <div class="card">
          <h3>独立展示源</h3>
          <label>热榜平台 id，逗号分隔</label>
          <input id="standalonePlatforms" placeholder="zhihu, wallstreetcn-hot" />
          <label>RSS id，逗号分隔</label>
          <input id="standaloneRss" />
          <label>每源最多条数</label>
          <input id="standaloneMax" type="number" min="0" />
        </div>
      </div>
      <div class="actions"><button class="primary" onclick="saveProfile()">保存当前方案</button></div>
      <p class="muted">具体哪次推送使用哪套内容，由 workflow 里的 <code>DAILYANA_PROFILE</code> 和 cron 决定。</p>
    </section>

    <section id="history">
      <div class="toolbar">
        <input class="search" id="historySearch" placeholder="搜索历史新闻，接口已预留" />
        <button class="primary" onclick="searchHistory()">搜索</button>
      </div>
      <div class="card">
        <h3>数据库接口占位</h3>
        <p id="historyMessage" class="muted"></p>
        <pre id="historyResult"></pre>
      </div>
    </section>
  </main>

  <script>
    let state = null;
    let currentProfile = 'work';
    let editingKeywordId = null;

    async function api(path, options={}) {
      const res = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'request failed');
      return data;
    }
    function toast(msg) { document.getElementById('status').textContent = msg; setTimeout(()=>document.getElementById('status').textContent='', 2500); }
    function qs(id) { return document.getElementById(id); }
    function csv(v) { return (v || '').split(',').map(x=>x.trim()).filter(Boolean); }
    function esc(v) {
      return String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }

    async function load() {
      state = await api('/api/state');
      renderSources(); renderKeywords(); renderProfile(); renderHistory();
    }

    document.querySelectorAll('nav button').forEach(btn => btn.addEventListener('click', () => {
      document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));
      document.querySelectorAll('section').forEach(s=>s.classList.remove('active'));
      btn.classList.add('active'); qs(btn.dataset.tab).classList.add('active');
    }));
    qs('sourceSearch').addEventListener('input', renderSources);
    qs('keywordSearch').addEventListener('input', renderKeywords);

    function renderSources() {
      const q = qs('sourceSearch').value.toLowerCase();
      const platforms = state.platforms.sources || [];
      qs('platformList').innerHTML = platforms.filter(x => JSON.stringify(x).toLowerCase().includes(q)).map((x, idx) => `
        <div class="item"><div class="item-head"><div><b>${esc(x.name)}</b> <span class="muted">${esc(x.id)}</span><br><span class="muted">${esc(x.expected_domain || '')}</span></div><span class="pill ${x.enabled === false ? 'off':'on'}">${x.enabled === false ? '禁用':'启用'}</span></div>
        <div class="actions"><button class="ghost" onclick="editPlatformByIndex(${idx})">编辑</button><button class="danger" onclick="deletePlatform('${esc(x.id)}')">删除</button></div></div>
      `).join('');
      const feeds = state.rss.feeds || [];
      qs('rssList').innerHTML = feeds.filter(x => JSON.stringify(x).toLowerCase().includes(q)).map((x, idx) => `
        <div class="item"><div class="item-head"><div><b>${esc(x.name)}</b> <span class="muted">${esc(x.id)}</span><br><span class="muted">${esc(x.url)}</span></div><span class="pill ${x.enabled === false ? 'off':'on'}">${x.enabled === false ? '禁用':'启用'}</span></div>
        <div class="actions"><button class="ghost" onclick="editRssByIndex(${idx})">编辑</button><button class="danger" onclick="deleteRss('${esc(x.id)}')">删除</button></div></div>
      `).join('');
    }
    function editPlatformByIndex(idx){ editPlatform((state.platforms.sources || [])[idx]); }
    function editRssByIndex(idx){ editRss((state.rss.feeds || [])[idx]); }
    function editPlatform(x){ qs('platId').value=x.id; qs('platName').value=x.name; qs('platDomain').value=x.expected_domain||''; qs('platEnabled').value=String(x.enabled!==false); }
    function editRss(x){ qs('rssId').value=x.id; qs('rssName').value=x.name; qs('rssUrl').value=x.url; qs('rssEnabled').value=String(x.enabled!==false); qs('rssMaxAge').value=x.max_age_days ?? ''; }
    async function savePlatform(){ state = await api('/api/platforms',{method:'POST',body:JSON.stringify({id:qs('platId').value,name:qs('platName').value,expected_domain:qs('platDomain').value,enabled:qs('platEnabled').value})}); renderSources(); toast('热榜源已保存'); }
    async function saveRss(){ state = await api('/api/rss',{method:'POST',body:JSON.stringify({id:qs('rssId').value,name:qs('rssName').value,url:qs('rssUrl').value,enabled:qs('rssEnabled').value,max_age_days:qs('rssMaxAge').value})}); renderSources(); toast('RSS 源已保存'); }
    async function deletePlatform(id){ if(confirm('删除热榜源 '+id+'?')){ state = await api('/api/platforms/'+encodeURIComponent(id),{method:'DELETE'}); renderSources(); } }
    async function deleteRss(id){ if(confirm('删除 RSS 源 '+id+'?')){ state = await api('/api/rss/'+encodeURIComponent(id),{method:'DELETE'}); renderSources(); } }

    function renderKeywords() {
      const q = qs('keywordSearch').value.toLowerCase();
      const filterHtml = (state.global_filters || []).length ? `<div class="item"><b>全局过滤</b><pre>${esc((state.global_filters || []).join('\\n'))}</pre><p class="muted">全局过滤暂只展示，编辑请直接改 frequency_words.txt。</p></div>` : '';
      qs('keywordList').innerHTML = state.keywords.filter(x => (x.title + x.text).toLowerCase().includes(q)).map(x => `
        <div class="item"><div class="item-head"><div><b>${esc(x.title)}</b><pre>${esc(x.text)}</pre></div></div>
        <div class="actions"><button class="ghost" onclick="editKeyword(${x.id})">编辑</button><button class="danger" onclick="deleteKeyword(${x.id})">删除</button></div></div>
      `).join('') + filterHtml;
    }
    function newKeyword(){ editingKeywordId=null; qs('keywordEditorTitle').textContent='新增关键词组'; qs('keywordText').value='[新组名]\\n关键词'; qs('keywordEditor').style.display='block'; }
    function editKeyword(id){ const item = (state.keywords || []).find(x => x.id === id); if (!item) return; editingKeywordId=id; qs('keywordEditorTitle').textContent='编辑关键词组'; qs('keywordText').value=item.text; qs('keywordEditor').style.display='block'; }
    function closeKeywordEditor(){ qs('keywordEditor').style.display='none'; }
    async function saveKeyword(){ const method = editingKeywordId === null ? 'POST':'PUT'; const path = editingKeywordId === null ? '/api/keywords':'/api/keywords/'+editingKeywordId; const data = await api(path,{method,body:JSON.stringify({text:qs('keywordText').value})}); state.keywords=data.groups; closeKeywordEditor(); renderKeywords(); toast('关键词已保存'); }
    async function deleteKeyword(id){ if(confirm('删除这个关键词组?')){ const data = await api('/api/keywords/'+id,{method:'DELETE'}); state.keywords=data.groups; renderKeywords(); } }

    function selectProfile(name) {
      currentProfile = name;
      document.querySelectorAll('[data-profile]').forEach(b=>b.classList.toggle('active', b.dataset.profile===name));
      renderProfile();
    }
    function renderProfile() {
      const eff = state.effective_profiles[currentProfile] || {};
      const display = eff.display || {};
      const regions = display.regions || {};
      qs('regHotlist').checked = !!regions.hotlist;
      qs('regRss').checked = !!regions.rss;
      qs('regAi').checked = !!regions.ai_analysis;
      qs('regNew').checked = !!regions.new_items;
      qs('regStandalone').checked = !!regions.standalone;
      qs('regionOrder').value = (display.region_order || []).join(', ');
      const ai = eff.ai_analysis || {};
      qs('aiEnabled').checked = !!ai.enabled;
      qs('aiRss').checked = !!ai.include_rss;
      qs('aiStandalone').checked = !!ai.include_standalone;
      qs('aiTimeline').checked = !!ai.include_rank_timeline;
      qs('aiMaxNews').value = ai.max_news_for_analysis || 150;
      const sa = display.standalone || {};
      qs('standalonePlatforms').value = (sa.platforms || []).join(', ');
      qs('standaloneRss').value = (sa.rss_feeds || []).join(', ');
      qs('standaloneMax').value = sa.max_items ?? 20;
    }
    async function saveProfile() {
      state = await api('/api/profiles',{method:'PUT',body:JSON.stringify({
        profile: currentProfile,
        display: {
          region_order: csv(qs('regionOrder').value),
          regions: {hotlist:qs('regHotlist').checked,rss:qs('regRss').checked,ai_analysis:qs('regAi').checked,new_items:qs('regNew').checked,standalone:qs('regStandalone').checked},
          standalone: {platforms:csv(qs('standalonePlatforms').value), rss_feeds:csv(qs('standaloneRss').value), max_items:qs('standaloneMax').value}
        },
        ai_analysis: {enabled:qs('aiEnabled').checked, include_rss:qs('aiRss').checked, include_standalone:qs('aiStandalone').checked, include_rank_timeline:qs('aiTimeline').checked, max_news_for_analysis:qs('aiMaxNews').value}
      })});
      renderProfile(); toast('推送方案已保存');
    }
    function renderHistory(){ qs('historyMessage').textContent = state.history.message; }
    async function searchHistory(){ const data = await api('/api/history/search?q='+encodeURIComponent(qs('historySearch').value)); qs('historyResult').textContent = JSON.stringify(data,null,2); }

    load().catch(err => alert(err.message));
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, data, status=HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self) -> None:
        body = HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_html()
            elif parsed.path == "/api/state":
                self.send_json(state_payload())
            elif parsed.path == "/api/history/search":
                q = parse_qs(parsed.query).get("q", [""])[0]
                self.send_json({
                    "ready": False,
                    "query": q,
                    "items": [],
                    "message": "历史新闻数据库尚未接入；接口已预留。",
                })
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
            if parsed.path == "/api/platforms":
                self.send_json(upsert_list_item("platforms", "sources", platform_payload(data)))
            elif parsed.path == "/api/rss":
                self.send_json(upsert_list_item("rss", "feeds", rss_payload(data)))
            elif parsed.path == "/api/keywords":
                self.send_json(upsert_keyword(None, data.get("text", "")))
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
            if parsed.path == "/api/profiles":
                self.send_json(save_profile_settings(data))
            elif parsed.path.startswith("/api/keywords/"):
                block_id = int(parsed.path.rsplit("/", 1)[1])
                self.send_json(upsert_keyword(block_id, data.get("text", "")))
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/platforms/"):
                item_id = unquote(parsed.path.rsplit("/", 1)[1])
                self.send_json(delete_list_item("platforms", "sources", item_id))
            elif parsed.path.startswith("/api/rss/"):
                item_id = unquote(parsed.path.rsplit("/", 1)[1])
                self.send_json(delete_list_item("rss", "feeds", item_id))
            elif parsed.path.startswith("/api/keywords/"):
                block_id = int(parsed.path.rsplit("/", 1)[1])
                self.send_json(delete_keyword(block_id))
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    host = "127.0.0.1"
    port = 8765
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Ravenis Core 配置台已启动: http://{host}:{port}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止配置台...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
