#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_site.py — 把 news/YYYY/MM/*.json 解析成静态新闻网站。

设计要点
--------
* 只用标准库：CI 里不需要额外安装任何依赖就能跑。
* 渲染层只消费 scripts/normalize.py 的输出，绝不直接读原始 JSON 字段。
  原因：不同期次的 JSON 存在三代字段漂移，直接读原始字段会让整块内容
  静默消失（线上已真实发生过置顶五条、对华影响、观察事项全部丢失）。
* 缺字段安静跳过，页面上不出现 "None" 或 Python 字典字面量。
* --strict / --check-only 作为发布闸门，校验的是内容完整性而非仅语法。
* 站内链接统一输出 Cloudflare Pages 的美食 URL（不带 .html）。

用法
----
    python scripts/build_site.py                       # 读 news/ 写 dist/
    python scripts/build_site.py --check-only          # 只校验不产出
    python scripts/build_site.py --check-only --strict # CI 闸门（更严格）
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import normalize as N  # noqa: E402

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
SITE_TITLE = "每日国际与中国热点新闻早报"
SITE_DESC = (
    "每天八分钟，掌握国际与中国最重要的新闻，"
    "并理解全球变化对中国经济、科技、金融与普通居民生活的实际影响。"
)
REPO_URL = "https://github.com/tyuecode/daily-news"

INTL_SECTIONS = [
    ("politics_and_diplomacy", "国际政治与外交"),
    ("economy_and_markets", "全球经济与金融市场"),
    ("technology_and_ai", "国际科技与人工智能"),
    ("major_social_events", "国际重大社会事件"),
]
CN_SECTIONS = [
    ("policy_and_government", "中国政策与政务"),
    ("economy_and_markets", "中国经济与金融市场"),
    ("technology_and_ai", "中国科技与人工智能"),
    ("society_and_livelihood", "中国社会与民生"),
]

WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 内容闸门阈值
MAX_TOTAL_ITEMS = 24
MIN_TOP5 = 3
MAX_TOP5 = 5


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def parse_date(name: str) -> Optional[datetime]:
    try:
        return datetime.strptime(name, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def fmt_date_cn(dt: datetime, with_weekday: bool = False) -> str:
    base = f"{dt.year}年{dt.month}月{dt.day}日"
    return base + "　" + WEEKDAY_CN[dt.weekday()] if with_weekday else base


def fmt_duration(ms: int) -> str:
    if not ms or ms <= 0:
        return ""
    total = round(ms / 1000)
    m, s = divmod(total, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}小时{m}分"
    return f"{m}分{s:02d}秒"


def score_class(score: Any) -> str:
    try:
        v = float(score)
    except (TypeError, ValueError):
        return "chip"
    return "chip chip-high" if v >= 8 else ("chip chip-mid" if v >= 6 else "chip chip-low")


def truncate_cn(text: str, limit: int) -> str:
    """按字符截断，尽量停在标点处，避免断在词中间。"""
    text = (text or "").strip()
    if not text or len(text) <= limit:
        return text
    window = text[:limit]
    for i in range(len(window) - 1, max(len(window) // 2, 0), -1):
        if window[i] in "，。；、：！？,;:!?":
            return window[: i + 1] + "…"
    return window + "…"


URL_RE = re.compile(r"https?://[^\s，；、）)】\]\"'<>]+")


def linkify(text: str) -> str:
    if not text:
        return ""
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out.append(esc(text[pos : m.start()]))
        url = m.group(0)
        out.append(
            f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(url)}</a>'
        )
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


# 站内链接：Cloudflare Pages 会把 /x.html 以 308 跳到 /x，
# 因此直接输出最终地址，canonical 与 sitemap 也用它。
def news_url(date_str: str) -> str:
    return f"/news/{date_str}"


def archive_url() -> str:
    return "/archive"


# --------------------------------------------------------------------------
# 数据加载
# --------------------------------------------------------------------------
def load_briefings(news_dir: Path, strict: bool = False, issues: list = None) -> list[dict]:
    """扫描 news/ 下的期次 JSON，返回规范化后的期次列表（按日期倒序）。"""
    issues = issues if issues is not None else []
    briefings: list[dict] = []
    if not news_dir.exists():
        issues.append({"level": "error", "msg": f"新闻目录不存在：{news_dir}"})
        return briefings

    for path in sorted(news_dir.rglob("*.json")):
        dt = parse_date(path.stem)
        if dt is None:
            issues.append({"level": "warn", "msg": f"跳过非日期文件：{path.name}"})
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            issues.append({"level": "error", "msg": f"JSON 解析失败 {path.name}：{exc}"})
            continue
        if not isinstance(raw, dict):
            issues.append({"level": "error", "msg": f"JSON 顶层不是对象：{path.name}"})
            continue

        # 目录/文件日期一致性
        parts = path.parts
        if len(parts) >= 3:
            expect_dir = f"{dt.year:04d}/{dt.month:02d}"
            if f"{parts[-3]}/{parts[-2]}" != expect_dir:
                issues.append(
                    {
                        "level": "error",
                        "msg": f"{path.name} 所在目录与日期不一致（应为 {expect_dir}）",
                    }
                )

        b = N.normalize_briefing(raw, path.stem)
        b["date"] = dt
        b["path"] = path
        briefings.append(b)

    briefings.sort(key=lambda x: x["date"], reverse=True)
    return briefings


def section_items(briefing: dict, region: str, key: str) -> list[dict]:
    """按 region + 分类取新闻；分类判定同时兼容 category 字段。"""
    out = []
    for item in briefing["items"]:
        if item["region"] != region:
            continue
        cat = item["category"]
        if cat == key:
            out.append(item)
    return out


def collect_by_region(briefing: dict, region: str) -> list[dict]:
    return [i for i in briefing["items"] if i["region"] == region]


# --------------------------------------------------------------------------
# 组件渲染
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 静态资源版本化
#
# Cloudflare 的边缘缓存对 /style.css 这类固定文件名会长时间命中旧版本
# （实测线上返回 Age 3000+ 与旧内容），即使重新部署也可能短期内看不到更新。
# 解决方式不是依赖 _headers 的 TTL，而是给文件名带上内容哈希：
# 内容一变 URL 就变，缓存自然失效。
# --------------------------------------------------------------------------
ASSET_VERSIONS: dict[str, str] = {}


def asset_url(name: str) -> str:
    """返回带内容哈希的资源地址，例如 /style.<hash>.css"""
    version = ASSET_VERSIONS.get(name)
    if not version:
        return f"/{name}"
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        return f"/{stem}.{version}.{ext}"
    return f"/{name}?v={version}"


def build_asset_versions(static_dir: Path) -> None:
    """为 site/ 下的静态资源计算内容哈希。"""
    import hashlib

    for name in ("style.css", "site.js", "favicon.svg"):
        path = static_dir / name
        if path.exists():
            ASSET_VERSIONS[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def render_head(title: str, description: str, canonical: str, base_url: str) -> str:
    full = base_url.rstrip("/") + canonical
    css = asset_url("style.css")
    js = asset_url("site.js")
    ico = asset_url("favicon.svg")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="stylesheet" href="{esc(css)}">
<link rel="icon" href="{esc(ico)}" type="image/svg+xml">
<link rel="apple-touch-icon" href="{esc(ico)}">
<link rel="canonical" href="{esc(full)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(full)}">
<meta name="color-scheme" content="light dark">
<script src="{esc(js)}" defer></script>
</head>
<body>
<div class="progress-bar" id="progress" aria-hidden="true"></div>
<header class="site-header">
  <div class="wrap">
    <a class="brand" href="/"><span class="dot"></span>{esc(SITE_TITLE)}</a>
    <nav class="nav-links">
      <a href="/">今日</a>
      <a href="{esc(archive_url())}">归档</a>
      <a href="{esc(REPO_URL)}" target="_blank" rel="noopener noreferrer">源码</a>
    </nav>
  </div>
</header>
<main>
  <div class="wrap">
"""


def render_foot(generated_at: str = "") -> str:
    stamp = f"<p>本期生成时间：{esc(generated_at)}</p>" if generated_at else ""
    return f"""  </div>
</main>
<footer class="footer">
  <div class="wrap">
    {stamp}
    <p>{esc(SITE_TITLE)}｜每日北京时间 08:00 采集，覆盖前一日 08:00 至当日 08:00。</p>
    <p>内容来自公开信息源，已标注报道机构与原始链接；分析判断与客观事实分开陈述，不构成投资建议。</p>
    <p><a href="{esc(REPO_URL)}" target="_blank" rel="noopener noreferrer">GitHub 仓库</a>
       ｜ <a href="{esc(archive_url())}">全部归档</a></p>
  </div>
</footer>
<button type="button" class="to-top" id="toTop" aria-label="回到顶部" hidden>↑</button>
</body>
</html>
"""


def render_404_page(base_url: str) -> str:
    return "".join(
        [
            render_head(f"页面不存在｜{SITE_TITLE}", "请求的页面不存在。", "/404", base_url),
            '      <p class="page-kicker">404</p>\n',
            '      <h1 class="page-title">这个页面不存在</h1>\n',
            '      <p class="reading-time">可能是链接过期，或者这一期还没有发布。</p>\n',
            '      <div class="card">\n',
            "        <p>可以回到首页看今天的最新简报，或者去归档页按日期查找。</p>\n",
            '        <div class="hero-actions">\n',
            '          <a class="btn btn-primary" href="/">回首页</a>\n',
            f'          <a class="btn" href="{esc(archive_url())}">全部归档</a>\n',
            "        </div>\n",
            "      </div>\n",
            render_foot(),
        ]
    )


def render_audio_box(date_str: str, audio: dict, script_text: str = "") -> str:
    """
    语音播报播放器。

    audio 由 load_audio_index 产出：
      ok       音频存在且可用
      voices   [{key,label,desc,src,duration_ms}, …] 多音色
      src      默认音色的 URL（带内容哈希）
      stale    稿件已更新但音频未重新合成

    交互（少量原生 JS，见 site.js）：
      * 音色切换：换 <audio> 的 src，记住选择
      * 倍速：playbackRate，记住选择
      * 展开播报稿：<details>，纯 CSS，无需 JS
    """
    if not audio or not audio.get("ok"):
        return ""

    voices = audio.get("voices") or []
    default_src = audio.get("src") or f"/audio/{date_str}.mp3"
    dur = fmt_duration(int(audio.get("duration_ms") or 0))
    dur_txt = f"，约 {dur}" if dur else ""

    warn = ""
    if audio.get("stale"):
        warn = (
            '<p class="audio-warn">本期稿件已更新，语音播报尚未重新合成；'
            "以下音频对应上一版稿件，内容可能与文字不完全一致。</p>"
        )

    # --- 音色按钮 ---
    voice_html = ""
    if len(voices) > 1:
        btns = []
        for v in voices:
            active = " is-active" if v.get("src") == default_src else ""
            btns.append(
                f'            <button type="button" class="voice-btn{active}"'
                f' data-src="{esc(v.get("src"))}"'
                f' data-voice="{esc(v.get("key"))}"'
                f' title="{esc(v.get("desc") or v.get("label"))}">'
                f'{esc(v.get("label"))}</button>'
            )
        voice_html = (
            '        <div class="audio-controls">\n'
            '          <span class="ctrl-label">音色</span>\n'
            '          <div class="voice-group" role="group" aria-label="选择播报音色">\n'
            + "\n".join(btns)
            + "\n          </div>\n"
            "        </div>\n"
        )

    # --- 倍速按钮 ---
    rate_btns = []
    for r in ("0.75", "1", "1.25", "1.5", "2"):
        active = " is-active" if r == "1" else ""
        label = "1×" if r == "1" else f"{r}×"
        rate_btns.append(
            f'            <button type="button" class="rate-btn{active}"'
            f' data-rate="{r}">{label}</button>'
        )
    rate_html = (
        '        <div class="audio-controls">\n'
        '          <span class="ctrl-label">倍速</span>\n'
        '          <div class="rate-group" role="group" aria-label="播放速度">\n'
        + "\n".join(rate_btns)
        + "\n          </div>\n"
        "        </div>\n"
    )

    # --- 播报稿展开 ---
    script_html = ""
    if script_text.strip():
        paragraphs = [
            p.strip() for p in re.split(r"\n\s*\n", script_text.strip()) if p.strip()
        ]
        body = "\n".join(f"            <p>{esc(p)}</p>" for p in paragraphs)
        script_html = (
            '        <details class="script-details">\n'
            "          <summary>展开播报稿全文</summary>\n"
            '          <div class="script-body">\n'
            f"{body}\n"
            "          </div>\n"
            "        </details>\n"
        )

    return f"""      <div class="audio-box" data-audio-box>
        <p class="audio-label">语音播报{dur_txt}</p>
        <audio controls preload="none" src="{esc(default_src)}" data-audio>
          你的浏览器不支持音频播放，可<a href="{esc(default_src)}">直接下载</a>。
        </audio>
{voice_html}{rate_html}        {warn}
        <p class="audio-note">中文播报稿由 edge-tts 合成，可直接收听或下载。</p>
{script_html}      </div>
"""


def render_breaking(items: list[dict]) -> str:
    """扫描全部新闻的结构化突发标记，不再依赖置顶里的文字。"""
    hits = [i for i in items if i["breaking"]["is_breaking"]]
    if not hits:
        return ""
    hits.sort(key=lambda i: i["importance_score"] or 0, reverse=True)
    rows = []
    for item in hits[:4]:
        status = item["breaking"]["window_status"] or "窗口后更新"
        rows.append(
            "        <p>"
            f"<strong>{esc(item['title'])}</strong>"
            f' <span class="src-time">{esc(status)}</span>'
            + (f"<br>{esc(item['summary'])}" if item["summary"] else "")
            + "</p>"
        )
    return (
        '      <div class="breaking">\n'
        '        <span class="tag-breaking">突发重大消息</span>\n'
        + "\n".join(rows)
        + "\n      </div>\n"
    )


def render_overview(overview: dict) -> str:
    if not overview:
        return ""
    intl = overview.get("international_overview", "")
    cn = overview.get("china_overview", "")
    takeaways = overview.get("key_takeaways") or []
    if not intl and not cn and not takeaways:
        return ""
    parts = ['      <div class="card" id="overview">\n        <h2>当天概览</h2>\n']
    if intl:
        parts.append(f"        <h3>国际</h3>\n        <p>{esc(intl)}</p>\n")
    if cn:
        parts.append(f"        <h3>中国</h3>\n        <p>{esc(cn)}</p>\n")
    if takeaways:
        parts.append('        <h3>关键要点</h3>\n        <ul class="key-facts">\n')
        for t in takeaways:
            parts.append(f"          <li>{esc(t)}</li>\n")
        parts.append("        </ul>\n")
    parts.append("      </div>\n")
    return "".join(parts)


def render_top5(top5: list[dict]) -> str:
    if not top5:
        return ""
    out = [
        '      <div class="card" id="top5">\n',
        "        <h2>今日最重要的五件事</h2>\n",
        '        <ol class="top-list">\n',
    ]
    for t in top5:
        title = t.get("title") or t.get("id") or "（该条目在正文中缺失）"
        score = t.get("score")
        score_html = (
            f' <span class="{score_class(score)}">{esc(score)} 分</span>'
            if score is not None
            else ""
        )
        out.append("          <li>\n")
        out.append(f'            <p class="top-title">{esc(title)}{score_html}</p>\n')
        if t.get("reason"):
            out.append(f'            <p class="top-reason">{esc(t["reason"])}</p>\n')
        out.append("          </li>\n")
    out.append("        </ol>\n      </div>\n")
    return "".join(out)


def render_channels(channels: dict) -> str:
    if not channels:
        return ""
    rows = []
    for key, label in N.CHANNEL_ORDER:
        text = channels.get(key)
        if text:
            rows.append(f"            <tr><th>{esc(label)}</th><td>{esc(text)}</td></tr>\n")
    # 未知渠道也保留，避免丢信息
    for key, text in channels.items():
        if key not in dict(N.CHANNEL_ORDER) and text:
            rows.append(f"            <tr><th>{esc(key)}</th><td>{esc(text)}</td></tr>\n")
    if not rows:
        return ""
    return '        <table class="channels">\n' + "".join(rows) + "        </table>\n"


def render_news_item(item: dict) -> str:
    title = item.get("title")
    if not title:
        return ""

    score = item.get("importance_score")
    score_html = f'<span class="{score_class(score)}">{esc(score)}</span>' if score is not None else ""

    meta = []
    for value, label in (
        (item.get("location"), "地点"),
        (item.get("event_time"), "事件时间"),
        (item.get("publish_time"), "报道时间"),
    ):
        if value:
            meta.append(f"<span>{esc(label)}：{esc(value)}</span>")

    out = ['      <article class="news-item">\n', '        <div class="news-head">\n']
    out.append(f"          <h3>{esc(title)}</h3>\n")
    if score_html:
        out.append(f"          {score_html}\n")
    out.append("        </div>\n")
    if meta:
        out.append('        <p class="news-meta">' + "".join(meta) + "</p>\n")

    chips = []
    impact = item.get("impact") or {}
    if impact.get("degree"):
        chips.append(f'<span class="{score_class(8 if impact["degree"] in ("高", "高至中") else 6)}">影响 {esc(impact["degree"])}</span>')
    if impact.get("horizon"):
        chips.append(f'<span class="chip chip-tag">{esc(impact["horizon"])}</span>')
    if item["breaking"]["is_breaking"]:
        chips.append('<span class="chip chip-high">突发</span>')
    for tag in item.get("tags") or []:
        chips.append(f'<span class="chip chip-tag">{esc(tag)}</span>')
    if chips:
        out.append('        <div class="chips">' + "".join(chips) + "</div>\n")

    if item.get("summary"):
        out.append(f'        <p class="news-summary">{esc(item["summary"])}</p>\n')

    facts = item.get("key_facts") or []
    if facts:
        out.append('        <ul class="key-facts">\n')
        for f in facts:
            out.append(f"          <li>{esc(f)}</li>\n")
        out.append("        </ul>\n")

    # ---- 折叠区 ----
    body: list[str] = []

    cia = item.get("china_impact") or {}
    if cia.get("channels") or cia.get("degree"):
        body.append("          <h4>对中国的潜在影响</h4>\n")
        head = []
        for k, label in (("degree", "程度"), ("horizon", "周期"), ("confidence", "可信度")):
            if cia.get(k):
                head.append(f"{label}：{cia[k]}")
        if head:
            body.append(f"          <p>{esc('　｜　'.join(head))}</p>\n")
        ch = render_channels(cia.get("channels") or {})
        if ch:
            body.append(ch)

    if impact.get("details"):
        body.append("          <h4>影响评估</h4>\n")
        line = []
        for k, label in (("degree", "程度"), ("horizon", "周期"), ("confidence", "可信度")):
            if impact.get(k):
                line.append(f"{label}：{impact[k]}")
        if line:
            body.append(f"          <p>{esc('　｜　'.join(line))}</p>\n")
        body.append(f"          <p>{esc(impact['details'])}</p>\n")

    if item.get("verification_status"):
        body.append("          <h4>核实状态</h4>\n")
        body.append(f"          <p>{esc(item['verification_status'])}</p>\n")

    if item.get("developing_details"):
        body.append("          <h4>仍在发展</h4>\n          <ul class=\"key-facts\">\n")
        for d in item["developing_details"]:
            body.append(f"            <li>{esc(d)}</li>\n")
        body.append("          </ul>\n")

    if item.get("follow_up"):
        body.append("          <h4>后续关注</h4>\n          <ul class=\"key-facts\">\n")
        for d in item["follow_up"]:
            body.append(f"            <li>{esc(d)}</li>\n")
        body.append("          </ul>\n")

    sources = item.get("sources") or []
    if sources:
        body.append("          <h4>信息来源</h4>\n          <ul class=\"sources-list\">\n")
        for s in sources:
            org = s.get("organization") or "来源"
            line = f'            <li><span class="src-org">{esc(org)}</span>'
            if s.get("title"):
                if s.get("url"):
                    line += (
                        f'：<a href="{esc(s["url"])}" target="_blank" rel="noopener noreferrer">'
                        f'{esc(s["title"])}</a>'
                    )
                else:
                    line += f'：{esc(s["title"])}'
            elif s.get("url"):
                line += (
                    f'：<a href="{esc(s["url"])}" target="_blank" rel="noopener noreferrer">原文链接</a>'
                )
            if s.get("publish_time"):
                line += f' <span class="src-time">（{esc(s["publish_time"])}）</span>'
            body.append(line + "</li>\n")
        body.append("          </ul>\n")

    if body:
        out.append('        <details class="more">\n')
        out.append("          <summary>影响分析 · 核实 · 来源</summary>\n")
        out.append('          <div class="more-body">\n')
        out.extend(body)
        out.append("          </div>\n        </details>\n")

    out.append("      </article>\n")
    return "".join(out)


def render_section(title: str, items: list[dict], anchor: str = "") -> str:
    rows = [r for r in (render_news_item(i) for i in items) if r]
    if not rows:
        return ""
    id_attr = f' id="{esc(anchor)}"' if anchor else ""
    return (
        f'      <div class="section-head"{id_attr}>\n'
        f"        <h2>{esc(title)}</h2>"
        f'<span class="count">{len(rows)} 条</span>\n'
        "      </div>\n" + "".join(rows)
    )


def render_dashboard(dashboard: dict, overall: str, confidence: str) -> str:
    rows = []
    channels = dashboard.get("channels") or {}
    for key, label in N.CHANNEL_ORDER:
        if channels.get(key):
            rows.append(f"            <tr><th>{esc(label)}</th><td>{esc(channels[key])}</td></tr>\n")
    if not rows and not overall and not confidence:
        return ""
    out = ['      <div class="card" id="impact">\n', "        <h2>对中国的综合影响</h2>\n"]
    if rows:
        out.append('        <table class="channels">\n' + "".join(rows) + "        </table>\n")
    if overall:
        out.append(f"        <h3>总体展望</h3>\n        <p>{esc(overall)}</p>\n")
    if confidence:
        out.append(f"        <h3>判断可信度</h3>\n        <p>{esc(confidence)}</p>\n")
    out.append("      </div>\n")
    return "".join(out)


def render_watchlist(watch: dict) -> str:
    if not watch or not any(watch.values()):
        return ""
    out = ['      <div class="card" id="watch">\n', "        <h2>未来 24 小时重点观察</h2>\n"]
    rendered = False

    for key, label in N.WATCH_SECTIONS:
        rows = watch.get(key) or []
        if not rows:
            continue
        rendered = True
        out.append(f"        <h3>{esc(label)}</h3>\n")
        out.append('        <ul class="watch-list">\n')
        for e in rows:
            parts = [f'<span class="watch-name">{esc(e["name"])}</span>']
            if e.get("beijing_time"):
                parts.append(f'<span class="src-time">北京时间 {esc(e["beijing_time"])}</span>')
            if e.get("priority"):
                parts.append(f'<span class="chip chip-mid">{esc(e["priority"])}</span>')
            out.append("          <li>" + " ".join(parts))
            if e.get("status"):
                out.append(f'<br><span class="src-time">日程状态：{esc(e["status"])}</span>')
            if e.get("why_watch"):
                out.append(f"<br>关注原因：{esc(e['why_watch'])}")
            if e.get("potential_impact"):
                out.append(f"<br>潜在影响：{esc(e['potential_impact'])}")
            if e.get("china_link"):
                out.append(f"<br>对华关联：{esc(e['china_link'])}")
            if e.get("sources"):
                links = " ".join(linkify(u) for u in e["sources"])
                out.append(f'<br><span class="src-time">来源：{links}</span>')
            out.append("</li>\n")
        out.append("        </ul>\n")

    out.append("      </div>\n")
    return "".join(out) if rendered else ""


def render_methodology(briefing: dict) -> str:
    """
    编辑说明 + 质控状态。
    质量控制为 false 时必须显式告知读者，而不是把标签隐藏起来。
    """
    config = briefing["config"]
    qc = briefing["quality"]
    period = config.get("coverage_period")
    tz = config.get("timezone") or "Asia/Shanghai"

    passed, failed = [], []
    labels = {
        "duplicate_check": "重复条目已合并",
        "major_news_cross_verified": "重大新闻已交叉核实",
        "unverified_claims_marked": "未证实说法已标注",
        "source_urls_checked": "来源链接已核对",
    }
    for key, label in labels.items():
        if qc.get(key):
            passed.append(label)
        elif qc.get("present"):
            failed.append(label.replace("已", "未"))
        else:
            failed.append(label.replace("已", "未"))

    if not period and not qc.get("present"):
        return ""

    out = ['      <div class="notice" id="method">\n', "        <strong>编辑说明</strong><br>\n"]
    if period:
        out.append(f"        采集窗口：{esc(period)}<br>\n")
    out.append(
        f"        时区：{esc(tz)}；本简报为每日一次的信息整理，不代表全天候实时监测。<br>\n"
    )
    if passed:
        out.append("        已完成：" + "；".join(esc(p) for p in passed) + "。<br>\n")
    if failed:
        out.append(
            '        <span class="notice-warn">未完成：'
            + "；".join(esc(f) for f in failed)
            + "。请以来源原文与后续官方通报为准。</span><br>\n"
        )
    out.append(
        "        客观事实与媒体分析分开陈述；市场判断存在时间边界，不构成投资建议。\n"
    )
    out.append("      </div>\n")
    return "".join(out)


# --------------------------------------------------------------------------
# 整页渲染
# --------------------------------------------------------------------------
def render_briefing_page(briefing: dict, audio: dict, base_url: str) -> str:
    date_str = briefing["date_str"]
    dt = briefing["date"]
    overview = briefing["overview"]
    items = briefing["items"]

    headline = overview.get("headline") or (items[0]["title"] if items else SITE_TITLE)
    cjk = sum(len(re.findall(r"[\u4e00-\u9fff]", i.get("summary", ""))) for i in items)
    cjk += sum(
        len(re.findall(r"[\u4e00-\u9fff]", f))
        for i in items
        for f in (i.get("key_facts") or [])
    )
    read_min = max(1, round(cjk / 400))

    page = [
        render_head(
            f"{fmt_date_cn(dt)}｜{SITE_TITLE}", headline, news_url(date_str), base_url
        ),
        '      <p class="page-kicker">每日早报</p>\n',
        f'      <h1 class="page-title">{esc(fmt_date_cn(dt, True))}</h1>\n',
        '      <p class="reading-time">'
        f"<span>{len(items)} 条新闻</span>"
        f"<span>约 {read_min} 分钟阅读</span>"
        f"<span>生成于 {esc(briefing['config'].get('generated_at') or '—')}</span>"
        "</p>\n",
        render_audio_box(date_str, audio, audio.get("script", "")),
        render_breaking(items),
        render_overview(overview),
        render_top5(briefing["top5"]),
    ]

    toc_entries: list[tuple[str, str]] = []
    if overview.get("international_overview") or overview.get("china_overview"):
        toc_entries.append(("#overview", "当天概览"))
    if briefing["top5"]:
        toc_entries.append(("#top5", "最重要的五件事"))

    for region, title, sections in (
        ("international", "国际新闻", INTL_SECTIONS),
        ("china", "中国新闻", CN_SECTIONS),
    ):
        region_items = collect_by_region(briefing, region)
        if not region_items:
            continue
        anchor = "intl" if region == "international" else "cn"
        toc_entries.append((f"#{anchor}", title))
        page.append(
            f'      <div class="section-head" id="{anchor}">'
            f"<h2>{esc(title)}</h2></div>\n"
        )
        for key, label in sections:
            selected = [i for i in region_items if i["category"] == key]
            page.append(render_section(label, selected, anchor=key))
        known = {k for k, _ in sections}
        others = [i for i in region_items if i["category"] not in known]
        if others:
            page.append(render_section("其他", others))

    if (briefing["dashboard"].get("channels") or briefing.get("dashboard_overall")):
        toc_entries.append(("#impact", "对中国的综合影响"))
    if any(briefing["watch"].values()):
        toc_entries.append(("#watch", "未来 24 小时"))
    toc_entries.append(("#method", "编辑说明"))

    page.append(
        render_dashboard(
            briefing["dashboard"],
            briefing.get("dashboard_overall", ""),
            briefing.get("dashboard_confidence", ""),
        )
    )
    page.append(render_watchlist(briefing["watch"]))
    page.append(render_methodology(briefing))

    # 目录：放在概览之前不合适（尚未渲染），因此插到音频框之后
    if len(toc_entries) >= 3:
        page.insert(
            6,
            '      <nav class="toc" aria-label="本页目录">\n'
            '        <span class="toc-label">目录</span>\n'
            + "\n".join(
                f'        <a href="{esc(href)}">{esc(label)}</a>'
                for href, label in toc_entries
            )
            + "\n      </nav>\n",
        )
    page.append(
        '      <p class="reading-time" style="margin-top:26px">'
        f'<a href="{esc(archive_url())}">← 查看全部归档</a></p>\n'
    )
    page.append(render_foot(briefing["config"].get("generated_at", "")))
    return "".join(page)


def render_index_page(briefings: list[dict], audio_map: dict, base_url: str) -> str:
    if not briefings:
        return "".join(
            [
                render_head(SITE_TITLE, SITE_DESC, "/", base_url),
                '      <div class="empty-state">\n        <p>还没有任何简报。</p>\n',
                "        <p>把生成的 JSON 放到 <code>news/YYYY/MM/</code> 下并重新构建即可。</p>\n",
                "      </div>\n",
                render_foot(),
            ]
        )

    latest = briefings[0]
    overview = latest["overview"]
    items = latest["items"]
    headline = overview.get("headline") or SITE_TITLE

    page = [render_head(SITE_TITLE, SITE_DESC, "/", base_url)]
    page.append('      <div class="hero">\n')
    page.append(
        f'        <p class="hero-date">{esc(fmt_date_cn(latest["date"], True))}　最新一期</p>\n'
    )
    page.append(f"        <h1>{esc(headline)}</h1>\n")
    page.append('        <div class="hero-actions">\n')
    page.append(
        f'          <a class="btn btn-primary" href="{esc(news_url(latest["date_str"]))}">阅读本期全文</a>\n'
    )
    latest_audio = audio_map.get(latest["date_str"], {})
    if latest_audio.get("ok"):
        audio_href = latest_audio.get("src") or f"/audio/{latest['date_str']}.mp3"
        page.append(
            f'          <a class="btn" href="{esc(audio_href)}">收听语音播报</a>\n'
        )
    page.append(f'          <a class="btn" href="{esc(archive_url())}">全部归档</a>\n')
    page.append("        </div>\n      </div>\n")

    page.append(render_audio_box(latest["date_str"], audio_map.get(latest["date_str"], {})))
    page.append(render_breaking(items))
    page.append(render_overview(overview))
    page.append(render_top5(latest["top5"]))

    # 置顶之外的条目，按 ID 精确排除（修复 v3 字符串置顶导致“全部条目重复出现”）
    top_ids = {t["id"] for t in latest["top5"] if t.get("id")}
    rest = [i for i in items if i.get("id") not in top_ids]
    if rest:
        page.append(
            '      <div class="card">\n        <h2>本期其他要闻</h2>\n'
            '        <ul class="key-facts">\n'
        )
        for i in rest:
            sc = i.get("importance_score")
            sc_html = f' <span class="{score_class(sc)}">{esc(sc)}</span>' if sc is not None else ""
            page.append(f"          <li>{esc(i['title'])}{sc_html}</li>\n")
        page.append("        </ul>\n")
        page.append(
            f'        <p style="margin-top:14px"><a href="{esc(news_url(latest["date_str"]))}">'
            "查看全部详情（含影响分析与信息来源）→</a></p>\n"
        )
        page.append("      </div>\n")

    if len(briefings) > 1:
        page.append(
            '      <div class="card">\n        <h2>往期回顾</h2>\n'
            '        <ul class="archive-list">\n'
        )
        for b in briefings[1:6]:
            head = truncate_cn(b["overview"].get("headline", ""), 46) or SITE_TITLE
            page.append(
                f'          <li><a href="{esc(news_url(b["date_str"]))}">'
                f'<span class="a-date">{esc(b["date_str"])}</span>'
                f'<span class="a-title">{esc(head)}</span></a></li>\n'
            )
        page.append("        </ul>\n")
        page.append(
            f'        <p style="margin-top:14px"><a href="{esc(archive_url())}">'
            f"全部 {len(briefings)} 期 →</a></p>\n"
        )
        page.append("      </div>\n")

    page.append(render_foot(latest["config"].get("generated_at", "")))
    return "".join(page)


def render_archive_page(briefings: list[dict], audio_map: dict, base_url: str) -> str:
    page = [
        render_head(f"全部归档｜{SITE_TITLE}", SITE_DESC, archive_url(), base_url),
        '      <p class="page-kicker">归档</p>\n',
        '      <h1 class="page-title">全部归档</h1>\n',
        f'      <p class="reading-time"><span>共 {len(briefings)} 期</span></p>\n',
    ]
    if not briefings:
        page.append('      <div class="empty-state"><p>还没有任何简报。</p></div>\n')
        page.append(render_foot())
        return "".join(page)

    groups: dict[tuple[int, int], list[dict]] = {}
    for b in briefings:
        groups.setdefault((b["date"].year, b["date"].month), []).append(b)

    for (year, month), rows in groups.items():
        page.append('      <div class="month-group">\n')
        page.append(f"        <h3>{year}年{month}月</h3>\n")
        page.append('        <ul class="archive-list">\n')
        for b in rows:
            head = truncate_cn(b["overview"].get("headline", ""), 52) or SITE_TITLE
            top_score = ""
            for i in b["items"]:
                sc = i.get("importance_score")
                if sc is not None and sc >= 9:
                    top_score = f"{sc} 分"
                    break
            audio_tag = " · 含语音" if audio_map.get(b["date_str"], {}).get("ok") else ""
            page.append(
                f'          <li><a href="{esc(news_url(b["date_str"]))}">'
                f'<span class="a-date">{esc(fmt_date_cn(b["date"], True))}</span>'
                f'<span class="a-title">{esc(head)}{esc(audio_tag)}</span>'
                f'<span class="a-score">{esc(top_score)}</span></a></li>\n'
            )
        page.append("        </ul>\n      </div>\n")

    page.append(render_foot())
    return "".join(page)


# --------------------------------------------------------------------------
# 内容闸门
# --------------------------------------------------------------------------
def validate_briefing(b: dict) -> list[dict]:
    """
    内容级校验：只查会影响读者理解的关键项。
    返回 issue 列表，level=error 会导致构建失败（strict 模式）。
    """
    issues: list[dict] = []
    day = b["date_str"]

    def err(msg: str) -> None:
        issues.append({"level": "error", "msg": f"[{day}] {msg}"})

    def warn(msg: str) -> None:
        issues.append({"level": "warn", "msg": f"[{day}] {msg}"})

    items = b["items"]
    if not items:
        err("没有任何有效新闻条目")
        return issues

    # 日期与配置一致性
    nd = b["config"].get("news_date")
    if nd and nd != day:
        err(f"briefing_config.news_date（{nd}）与文件名日期（{day}）不一致")

    # ID 唯一
    ids = [i["id"] for i in items if i["id"]]
    if len(ids) != len(set(ids)):
        err("存在重复的新闻 ID")

    # 置顶引用必须全部可解析
    for t in b["top5"]:
        if not t.get("resolved"):
            err(f"top_5 引用了不存在的新闻 ID：{t.get('id')}")
        elif not t.get("title"):
            err(f"top_5 条目缺少标题：{t.get('id')}")
    n5 = len(b["top5"])
    if n5 < MIN_TOP5:
        warn(f"置顶条目只有 {n5} 条，建议 {MIN_TOP5}–{MAX_TOP5} 条")
    if n5 > MAX_TOP5:
        err(f"置顶条目 {n5} 条，超过上限 {MAX_TOP5}")

    if len(items) > MAX_TOTAL_ITEMS:
        warn(f"本期 {len(items)} 条新闻，超过建议上限 {MAX_TOTAL_ITEMS}，重点可能被稀释")

    # 关键内容完整性
    no_summary = [i["id"] for i in items if not i.get("summary")]
    if no_summary:
        err(f"缺少 summary：{', '.join(no_summary[:6])}")

    no_source = [i["id"] for i in items if not i.get("sources")]
    if no_source:
        err(f"缺少信息来源：{', '.join(no_source[:6])}")

    bad_url = [
        i["id"]
        for i in items
        for s in i.get("sources", [])
        if s.get("url") and not s["url"].startswith(("http://", "https://"))
    ]
    if bad_url:
        err(f"来源链接不是 http(s)：{', '.join(sorted(set(bad_url))[:6])}")

    # 对华影响：这是本产品核心价值
    no_china = [i["id"] for i in items if not (i.get("china_impact") or {}).get("channels")]
    if no_china:
        warn(f"缺少对华影响分析：{len(no_china)} 条（{', '.join(no_china[:6])}）")

    # 窗口后事件必须显式标注
    for i in items:
        if i["breaking"]["is_breaking"] and not i["breaking"].get("window_status"):
            warn(f"{i['id']} 疑似窗口后事件但未结构化标注 window_status")

    # 空分类需要说明
    for region, sections, label in (
        ("international", INTL_SECTIONS, "国际"),
        ("china", CN_SECTIONS, "中国"),
    ):
        for key, name in sections:
            if not any(i["category"] == key and i["region"] == region for i in items):
                warn(f"{label}「{name}」分类为空")

    return issues


# --------------------------------------------------------------------------
# 音频可用性
# --------------------------------------------------------------------------
def load_audio_index(audio_dir: Path, briefings: list[dict], issues: list) -> dict:
    """
    判定每期音频是否可用，产出多音色信息与带内容哈希的 URL。

    ok=True 需满足：至少一个音色的 mp3 存在且非空、元数据可读。
    稿件哈希与元数据不一致时标记 stale（页面会显式提示）。
    每个 mp3 复制/重命名为 <date>.<voice>.<hash>.mp3，页面引用该地址：
      * 稿件不变 → URL 不变，可正常复用缓存
      * 稿件变更 → URL 变化，用户不会继续拿到旧音频
    """
    import hashlib
    import shutil as _shutil

    index: dict[str, dict] = {}
    for b in briefings:
        day = b["date_str"]
        meta_path = audio_dir / f"{day}.json"

        # 当前稿件哈希
        script_path = (
            Path("audio-script") / f"{b['date'].year:04d}" / f"{b['date'].month:02d}" / f"{day}.txt"
        )
        script_hash = ""
        script_text = ""
        if script_path.exists():
            script_text = script_path.read_text(encoding="utf-8", errors="replace")
            script_hash = hashlib.sha256(script_text.encode("utf-8")).hexdigest()

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}

        meta_voices = meta.get("voices") if isinstance(meta.get("voices"), dict) else {}
        audio_hash = str(meta.get("source_sha256") or "").strip()
        stale = bool(script_hash and audio_hash and script_hash != audio_hash)
        if stale:
            issues.append(
                {"level": "warn", "msg": f"[{day}] 稿件已变更但音频未重新合成（陈旧音频）"}
            )

        voices_out: list[dict] = []
        for key, vm in meta_voices.items():
            if not isinstance(vm, dict):
                continue
            # 元数据里记的是「未版本化」文件名（构建时会被改名），
            # 因此先在原始名找，找不到再按版本化名找，否则重复构建会丢音频。
            base_name = vm.get("file") or f"{day}.{key}.mp3"
            mp3 = audio_dir / base_name
            version = (vm.get("source_sha256") or audio_hash or "")[:8]
            if not version and mp3.exists():
                version = hashlib.sha256(mp3.read_bytes()).hexdigest()[:8]
            versioned = audio_dir / f"{day}.{key}.{version}.mp3" if version else mp3

            if not mp3.exists():
                # 已被上次构建改名：直接用版本化文件
                if versioned.exists() and versioned.stat().st_size >= 1024:
                    mp3 = versioned
                else:
                    continue
            if mp3.stat().st_size < 1024:
                continue

            if mp3 != versioned and not versioned.exists():
                try:
                    mp3.rename(versioned)
                except OSError:
                    try:
                        _shutil.copy2(mp3, versioned)
                    except OSError:
                        versioned = mp3
            if mp3 == versioned:
                versioned = mp3
            if versioned != mp3 and mp3.exists():
                try:
                    mp3.unlink()
                except OSError:
                    pass
            voices_out.append(
                {
                    "key": key,
                    "label": vm.get("label") or key,
                    "desc": vm.get("desc") or "",
                    "src": f"/audio/{versioned.name}",
                    "duration_ms": int(vm.get("duration_ms") or 0),
                }
            )

        # 兼容旧版单音色元数据（无 voices 字段）
        if not voices_out:
            legacy = audio_dir / f"{day}.mp3"
            if legacy.exists() and legacy.stat().st_size >= 1024:
                version = (audio_hash or hashlib.sha256(legacy.read_bytes()).hexdigest())[:8]
                versioned = audio_dir / f"{day}.{version}.mp3"
                if not versioned.exists():
                    try:
                        legacy.rename(versioned)
                    except OSError:
                        versioned = legacy
                voices_out.append(
                    {
                        "key": "default",
                        "label": "默认",
                        "desc": "",
                        "src": f"/audio/{versioned.name}",
                        "duration_ms": int(meta.get("duration_ms") or 0),
                    }
                )

        if not voices_out:
            continue

        default_key = meta.get("default") or voices_out[0]["key"]
        chosen = next((v for v in voices_out if v["key"] == default_key), voices_out[0])

        index[day] = {
            "ok": True,
            "stale": stale,
            "src": chosen["src"],
            "duration_ms": chosen["duration_ms"],
            "voices": voices_out,
            "script": script_text,
        }
    return index


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="把新闻 JSON 构建成静态网站")
    parser.add_argument("--news-dir", default="news")
    parser.add_argument("--audio-dir", default="dist/audio")
    parser.add_argument("--out-dir", default="dist")
    parser.add_argument("--base-url", default="https://daily-news.cc.cd")
    parser.add_argument("--static-dir", default="site")
    parser.add_argument("--strict", action="store_true", help="保留参数以兼容旧调用；现在默认即拦截错误")
    parser.add_argument(
        "--allow-errors",
        action="store_true",
        help="即使内容校验有错误也强制产出（默认拒绝，用于本地排查）",
    )
    parser.add_argument("--check-only", action="store_true", help="只校验不产出")
    args = parser.parse_args()

    news_dir = Path(args.news_dir)
    out_dir = Path(args.out_dir)
    audio_dir = Path(args.audio_dir)
    static_dir = Path(args.static_dir)

    issues: list[dict] = []
    print(f"读取 JSON：{news_dir}")
    briefings = load_briefings(news_dir, strict=args.strict, issues=issues)
    print(f"  找到 {len(briefings)} 期简报")

    if not briefings:
        issues.append({"level": "error", "msg": "没有可构建的简报"})

    for b in briefings:
        issues.extend(validate_briefing(b))

    errors = [i for i in issues if i["level"] == "error"]
    warns = [i for i in issues if i["level"] == "warn"]

    for i in warns:
        print(f"  ! {i['msg']}", file=sys.stderr)
    for i in errors:
        print(f"  !! {i['msg']}", file=sys.stderr)

    print(f"\n校验：{len(errors)} 个错误，{len(warns)} 个警告")

    if args.check_only:
        if errors:
            print("校验未通过。", file=sys.stderr)
            return 1
        print(f"校验通过：{len(briefings)} 期。")
        return 0

    # 默认即拦截：绝不把「内容已知不完整」的结果发布出去。
    # 历史上正是因为放行了这类构建，才出现线上整块空白。
    if errors and not args.allow_errors:
        print(
            "构建中止：存在内容错误。确需强制产出请显式加 --allow-errors。",
            file=sys.stderr,
        )
        return 1
    if errors:
        print("警告：已使用 --allow-errors，将产出内容不完整的站点。", file=sys.stderr)

    audio_map = load_audio_index(audio_dir, briefings, issues)
    print(f"  可用音频：{len(audio_map)} 期")

    # 音频相关告警在 validate 之后才产生，需重新汇总并打印
    errors = [i for i in issues if i["level"] == "error"]
    warns = [i for i in issues if i["level"] == "warn"]
    audio_warns = [i for i in warns if "音频" in i["msg"]]
    for i in audio_warns:
        print(f"  ! {i['msg']}", file=sys.stderr)
    if errors:
        for i in errors:
            print(f"  !! {i['msg']}", file=sys.stderr)
        if args.strict:
            print("构建中止：内容校验未通过。", file=sys.stderr)
            return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    if static_dir.exists():
        # 先算哈希，render_head 需要用它生成带版本的 URL
        build_asset_versions(static_dir)
        for item in static_dir.iterdir():
            dest = out_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
                # 同时产出一份带内容哈希的副本，供 HTML 引用
                version = ASSET_VERSIONS.get(item.name)
                if version and "." in item.name:
                    stem, ext = item.name.rsplit(".", 1)
                    shutil.copy2(item, out_dir / f"{stem}.{version}.{ext}")
        print(f"  复制静态资源：{static_dir} -> {out_dir}")

    news_out = out_dir / "news"
    news_out.mkdir(parents=True, exist_ok=True)
    for b in briefings:
        doc = render_briefing_page(b, audio_map.get(b["date_str"], {}), args.base_url)
        (news_out / f"{b['date_str']}.html").write_text(doc, encoding="utf-8")

    (out_dir / "index.html").write_text(
        render_index_page(briefings, audio_map, args.base_url), encoding="utf-8"
    )
    (out_dir / "archive.html").write_text(
        render_archive_page(briefings, audio_map, args.base_url), encoding="utf-8"
    )
    (out_dir / "404.html").write_text(render_404_page(args.base_url), encoding="utf-8")

    index_data = [
        {
            "date": b["date_str"],
            "headline": b["overview"].get("headline", ""),
            "news_count": len(b["items"]),
            "has_audio": audio_map.get(b["date_str"], {}).get("ok", False),
            "audio_duration_ms": audio_map.get(b["date_str"], {}).get("duration_ms", 0),
            "url": news_url(b["date_str"]),
        }
        for b in briefings
    ]
    (out_dir / "archive.json").write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    base = args.base_url.rstrip("/")
    sitemap = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    def add_url(loc: str, lastmod: str = "", priority: str = "0.6") -> None:
        sitemap.append("  <url>")
        sitemap.append(f"    <loc>{html.escape(base + loc, quote=False)}</loc>")
        if lastmod:
            sitemap.append(f"    <lastmod>{lastmod}</lastmod>")
        sitemap.append(f"    <priority>{priority}</priority>")
        sitemap.append("  </url>")

    add_url("/", briefings[0]["date_str"] if briefings else "", "1.0")
    add_url(archive_url(), briefings[0]["date_str"] if briefings else "", "0.8")
    for b in briefings:
        add_url(news_url(b["date_str"]), b["date_str"], "0.7")
    sitemap.append("</urlset>")
    (out_dir / "sitemap.xml").write_text("\n".join(sitemap) + "\n", encoding="utf-8")

    print(f"\n构建完成：{out_dir}")
    print(f"  首页    {out_dir / 'index.html'}")
    print(f"  归档    {out_dir / 'archive.html'}")
    print(f"  期次页  {len(briefings)} 个 -> {news_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
