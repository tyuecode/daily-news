#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_site.py — 把 news/YYYY/MM/*.json 解析成静态新闻网站。

设计要点
--------
* 只用标准库：CI 里不需要额外安装任何依赖就能跑。
* 容错解析：JSON 里缺字段、字段类型不对、值为 null 都不应让构建崩掉，
  缺失的部分安静跳过，页面上不出现 "None" 或 "undefined"。
* 纯相对/根相对路径：产物是可直接丢到 Cloudflare Pages 的 dist/。
* 日期来自文件名，不依赖 JSON 内部的 news_date（两者不一致时以文件名为准，
  但会在页面脚注提示）。

用法
----
    python scripts/build_site.py                 # 读 news/ 写 dist/
    python scripts/build_site.py --base-url https://daily-news.cc.cd
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
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
SITE_TITLE = "每日国际与中国热点新闻早报"
SITE_DESC = "每天八分钟，掌握国际与中国最重要的新闻，并理解全球变化对中国经济、科技、金融与普通居民生活的实际影响。"

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

CHANNEL_LABELS = {
    "trade": "外贸",
    "finance": "金融",
    "technology": "科技",
    "energy": "能源",
    "industry": "产业",
    "livelihood": "民生",
    "policy": "政策",
}

DASHBOARD_LABELS = [
    ("trade", "外贸"),
    ("finance", "金融"),
    ("technology", "科技"),
    ("energy", "能源"),
    ("employment", "就业"),
    ("public_livelihood", "民生"),
]

WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def esc(value: Any) -> str:
    """安全转义为 HTML 文本。"""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def as_text(value: Any) -> str:
    """把可能是 None / 字典 / 列表的值转成可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "；".join(f"{k}：{v}" for k, v in value.items() if v)
    if isinstance(value, list):
        return "；".join(as_text(v) for v in value if v)
    return str(value).strip()


def first_nonempty(*values: Any) -> str:
    for v in values:
        t = as_text(v)
        if t:
            return t
    return ""


def parse_date(name: str) -> Optional[datetime]:
    try:
        return datetime.strptime(name, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def fmt_date_cn(dt: datetime, with_weekday: bool = False) -> str:
    base = f"{dt.year}年{dt.month}月{dt.day}日"
    if with_weekday:
        base += "　" + WEEKDAY_CN[dt.weekday()]
    return base


def fmt_iso_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


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
    if v >= 8:
        return "chip chip-high"
    if v >= 6:
        return "chip chip-mid"
    return "chip chip-low"


URL_RE = re.compile(r"https?://[^\s，；、）)】\]\"'<>]+")


def linkify(text: str) -> str:
    """
    把一段可能夹带 URL 的文字转成 HTML：URL 变链接，其余原样转义。
    """
    if not text:
        return ""
    out: list[str] = []
    pos = 0
    for m in URL_RE.finditer(text):
        out.append(esc(text[pos : m.start()]))
        url = m.group(0)
        out.append(
            f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(url)}</a>'
        )
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


def truncate_cn(text: str, limit: int) -> str:
    """
    按字符截断中文标题，尽量停在标点处，并补省略号。
    避免出现「……天宫空间站对」这种断在词中间的情况。
    """
    text = as_text(text)
    if not text or len(text) <= limit:
        return text
    window = text[:limit]
    # 在窗口后半段找一个句读，优先在标点处断开
    for i in range(len(window) - 1, max(len(window) // 2, 0), -1):
        if window[i] in "，。；、：！？,;:!?":
            return window[: i + 1] + "…"
    return window + "…"


def text_len(*chunks: Any) -> int:
    """粗略统计中文字数，用于估算阅读时长。"""
    n = 0
    for c in chunks:
        if not c:
            continue
        for s in as_list(c):
            n += len(re.findall(r"[\u4e00-\u9fff]", as_text(s)))
    return n


# --------------------------------------------------------------------------
# 数据加载
# --------------------------------------------------------------------------
def load_briefings(news_dir: Path, strict: bool = False) -> list[dict]:
    """
    扫描 news/YYYY/MM/YYYY-MM-DD.json，返回按日期倒序的期次列表。

    strict=True 时，遇到无法解析的 JSON 直接抛异常（CI 用来做发布闸门）；
    strict=False 时跳过坏文件并告警（本地随便跑跑时更顺手）。
    """
    briefings: list[dict] = []
    if not news_dir.exists():
        return briefings

    for path in sorted(news_dir.rglob("*.json")):
        dt = parse_date(path.stem)
        if dt is None:
            print(f"  跳过非日期 JSON：{path}", file=sys.stderr)
            continue
        try:
            # utf-8-sig 兼容带 BOM 的文件
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            msg = f"JSON 解析失败：{path}：{exc}"
            if strict:
                raise ValueError(msg) from exc
            print(f"  !! {msg}，已跳过", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            msg = f"JSON 顶层不是对象：{path}"
            if strict:
                raise ValueError(msg)
            print(f"  !! {msg}，已跳过", file=sys.stderr)
            continue

        briefings.append(
            {"date": dt, "date_str": fmt_iso_date(dt), "path": path, "data": data}
        )

    briefings.sort(key=lambda b: b["date"], reverse=True)
    return briefings


def collect_items(briefing: dict) -> list[dict]:
    """
    把国际 + 中国的全部新闻条目拉平成一个列表。

    注意：international_news 与 china_news 存在同名分类
    （economy_and_markets / technology_and_ai），因此必须按各自的分区
    使用对应的分类表，否则会重复收集条目。同时按 id 兜底去重。
    """
    items: list[dict] = []
    seen_ids: set[str] = set()
    for group, sections in (
        ("international_news", INTL_SECTIONS),
        ("china_news", CN_SECTIONS),
    ):
        block = briefing.get(group)
        if not isinstance(block, dict):
            continue
        for key, _label in sections:
            for item in as_list(block.get(key)):
                if not isinstance(item, dict):
                    continue
                nid = as_text(item.get("id"))
                if nid:
                    if nid in seen_ids:
                        continue
                    seen_ids.add(nid)
                items.append(item)
    return items


# --------------------------------------------------------------------------
# 组件渲染
# --------------------------------------------------------------------------
def render_head(
    title: str, description: str, canonical: str, base_url: str
) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="stylesheet" href="/style.css">
<link rel="canonical" href="{esc(base_url.rstrip('/') + canonical)}">
<meta property="og:type" content="article">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(base_url.rstrip('/') + canonical)}">
<meta name="color-scheme" content="light dark">
</head>
<body>
<header class="site-header">
  <div class="wrap">
    <a class="brand" href="/"><span class="dot"></span>{esc(SITE_TITLE)}</a>
    <nav class="nav-links">
      <a href="/">今日</a>
      <a href="/archive.html">归档</a>
      <a href="https://github.com/tyuecode/daily-news" target="_blank" rel="noopener noreferrer">源码</a>
    </nav>
  </div>
</header>
<main>
  <div class="wrap">
"""


def render_foot(generated_at: str = "") -> str:
    stamp = (
        f'<p>本期生成时间：{esc(generated_at)}</p>' if generated_at else ""
    )
    return f"""  </div>
</main>
<footer class="footer">
  <div class="wrap">
    {stamp}
    <p>{esc(SITE_TITLE)}｜每日北京时间 08:00 采集，覆盖前一日 08:00 至当日 08:00。</p>
    <p>内容来自公开信息源，已标注报道机构与原始链接；分析判断与客观事实分开陈述，不构成投资建议。</p>
    <p><a href="https://github.com/tyuecode/daily-news" target="_blank" rel="noopener noreferrer">GitHub 仓库</a>
       ｜ <a href="/archive.html">全部归档</a></p>
  </div>
</footer>
</body>
</html>
"""


def render_audio_box(date_str: str, audio_meta: dict) -> str:
    """音频存在才渲染播放器。"""
    if not audio_meta:
        return ""
    dur = fmt_duration(int(audio_meta.get("duration_ms") or 0))
    dur_txt = f"，约 {dur}" if dur else ""
    return f"""      <div class="audio-box">
        <p class="audio-label">语音播报{dur_txt}</p>
        <audio controls preload="none" src="/audio/{esc(date_str)}.mp3">
          你的浏览器不支持音频播放，可<a href="/audio/{esc(date_str)}.mp3">直接下载</a>。
        </audio>
        <p class="audio-note">中文播报稿由 edge-tts 合成，可直接收听或下载。</p>
      </div>
"""


def render_breaking(top5: list, items_by_id: dict) -> str:
    """从 top_5 里识别带突发标记的条目，渲染横幅。"""
    hits = []
    for t in top5:
        if not isinstance(t, dict):
            continue
        status = as_text(t.get("window_status"))
        if any(k in status for k in ("突发", "重大更新", "窗口后")):
            hits.append(t)
    if not hits:
        return ""

    rows = []
    for t in hits:
        title = as_text(t.get("title"))
        reason = as_text(t.get("reason"))
        nid = as_text(t.get("id"))
        item = items_by_id.get(nid, {})
        summary = as_text(item.get("summary"))
        body = summary or reason
        rows.append(
            f"<p><strong>{esc(title)}</strong>"
            + (f" 　{esc(body)}" if body else "")
            + "</p>"
        )
    return (
        '      <div class="breaking">\n'
        '        <span class="tag-breaking">突发重大消息</span>\n'
        + "\n".join("        " + r for r in rows)
        + "\n      </div>\n"
    )


def render_overview(overview: dict, takeaways: list) -> str:
    intl = as_text(overview.get("international_overview"))
    cn = as_text(overview.get("china_overview"))
    if not intl and not cn and not takeaways:
        return ""

    parts = ['      <div class="card">\n        <h2>当天概览</h2>\n']
    if intl:
        parts.append(f"        <h3>国际</h3>\n        <p>{esc(intl)}</p>\n")
    if cn:
        parts.append(f"        <h3>中国</h3>\n        <p>{esc(cn)}</p>\n")
    if takeaways:
        parts.append("        <h3>关键要点</h3>\n        <ul class=\"key-facts\">\n")
        for t in takeaways:
            txt = as_text(t)
            if txt:
                parts.append(f"          <li>{esc(txt)}</li>\n")
        parts.append("        </ul>\n")
    parts.append("      </div>\n")
    return "".join(parts)


def render_top5(top5: list) -> str:
    rows = [t for t in as_list(top5) if isinstance(t, dict)]
    if not rows:
        return ""
    out = [
        '      <div class="card">\n',
        "        <h2>今日最重要的五件事</h2>\n",
        '        <ol class="top-list">\n',
    ]
    for t in rows:
        title = as_text(t.get("title"))
        reason = as_text(t.get("reason"))
        score = t.get("importance_score")
        score_html = (
            f' <span class="{score_class(score)}">{esc(score)} 分</span>'
            if score not in (None, "")
            else ""
        )
        out.append("          <li>\n")
        out.append(
            f'            <p class="top-title">{esc(title)}{score_html}</p>\n'
        )
        if reason:
            out.append(f'            <p class="top-reason">{esc(reason)}</p>\n')
        out.append("          </li>\n")
    out.append("        </ol>\n      </div>\n")
    return "".join(out)


def render_channels(channels: dict) -> str:
    if not isinstance(channels, dict):
        return ""
    rows = []
    for key, label in CHANNEL_LABELS.items():
        val = as_text(channels.get(key))
        if val:
            rows.append(
                f"            <tr><th>{esc(label)}</th><td>{esc(val)}</td></tr>\n"
            )
    if not rows:
        return ""
    return (
        '        <table class="channels">\n' + "".join(rows) + "        </table>\n"
    )


def render_news_item(item: dict) -> str:
    title = as_text(item.get("title"))
    if not title:
        return ""

    score = item.get("importance_score")
    score_html = (
        f'<span class="{score_class(score)}">{esc(score)}</span>'
        if score not in (None, "")
        else ""
    )

    meta_bits = []
    for key, label in (
        ("location", "地点"),
        ("event_time", "事件时间"),
        ("publish_time", "报道时间"),
    ):
        val = as_text(item.get(key))
        if val:
            meta_bits.append(f"<span>{esc(label)}：{esc(val)}</span>")

    out = ['      <article class="news-item">\n', '        <div class="news-head">\n']
    out.append(f"          <h3>{esc(title)}</h3>\n")
    if score_html:
        out.append(f"          {score_html}\n")
    out.append("        </div>\n")
    if meta_bits:
        out.append(
            '        <p class="news-meta">' + "".join(meta_bits) + "</p>\n"
        )

    # 标签 + 影响程度
    chips = []
    impact = item.get("impact_assessment")
    if isinstance(impact, dict):
        degree = as_text(impact.get("degree"))
        if degree:
            cls = {"高": "chip chip-high", "中高": "chip chip-mid", "中": "chip chip-mid"}.get(
                degree, "chip chip-low"
            )
            chips.append(f'<span class="{cls}">影响 {esc(degree)}</span>')
    for tag in as_list(item.get("tags")):
        t = as_text(tag)
        if t:
            chips.append(f'<span class="chip chip-tag">{esc(t)}</span>')
    if chips:
        out.append('        <div class="chips">' + "".join(chips) + "</div>\n")

    summary = as_text(item.get("summary"))
    if summary:
        out.append(f'        <p class="news-summary">{esc(summary)}</p>\n')

    facts = [as_text(f) for f in as_list(item.get("key_facts")) if as_text(f)]
    if facts:
        out.append('        <ul class="key-facts">\n')
        for f in facts:
            out.append(f"          <li>{esc(f)}</li>\n")
        out.append("        </ul>\n")

    # ---- 折叠区：对中国影响 / 核实状态 / 来源 / 后续 ----
    body: list[str] = []
    cia = item.get("china_impact_analysis")
    if isinstance(cia, dict) and cia:
        head = []
        for k in ("degree", "horizon", "confidence"):
            v = as_text(cia.get(k))
            if v:
                label = {"degree": "程度", "horizon": "周期", "confidence": "可信度"}[k]
                head.append(f"{label}：{v}")
        if head:
            body.append(f"          <h4>对中国的潜在影响</h4>\n")
            body.append(f"          <p>{esc('　｜　'.join(head))}</p>\n")
        ch = render_channels(cia.get("channels"))
        if ch:
            body.append(ch)

    if isinstance(impact, dict):
        details = as_text(impact.get("details"))
        if details:
            body.append("          <h4>影响评估</h4>\n")
            body.append(f"          <p>{esc(details)}</p>\n")

    ver = as_text(item.get("verification_status"))
    if ver:
        body.append("          <h4>核实状态</h4>\n")
        body.append(f"          <p>{esc(ver)}</p>\n")

    dev = [as_text(x) for x in as_list(item.get("developing_details")) if as_text(x)]
    if dev:
        body.append("          <h4>仍在发展</h4>\n")
        body.append('          <ul class="key-facts">\n')
        for d in dev:
            body.append(f"            <li>{esc(d)}</li>\n")
        body.append("          </ul>\n")

    fu = [as_text(x) for x in as_list(item.get("follow_up")) if as_text(x)]
    if fu:
        body.append("          <h4>后续关注</h4>\n")
        body.append('          <ul class="key-facts">\n')
        for d in fu:
            body.append(f"            <li>{esc(d)}</li>\n")
        body.append("          </ul>\n")

    sources = [s for s in as_list(item.get("sources")) if isinstance(s, dict)]
    if sources:
        body.append("          <h4>信息来源</h4>\n")
        body.append('          <ul class="sources-list">\n')
        for s in sources:
            org = first_nonempty(s.get("organization"), s.get("publisher"), "来源")
            stitle = as_text(s.get("title"))
            url = as_text(s.get("url"))
            stime = as_text(s.get("publish_time"))
            line = f'            <li><span class="src-org">{esc(org)}</span>'
            if stitle:
                if url:
                    line += (
                        f'：<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                        f"{esc(stitle)}</a>"
                    )
                else:
                    line += f"：{esc(stitle)}"
            elif url:
                line += (
                    f'：<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                    f"原文链接</a>"
                )
            if stime:
                line += f' <span class="src-time">（{esc(stime)}）</span>'
            line += "</li>\n"
            body.append(line)
        body.append("          </ul>\n")

    if body:
        out.append('        <details class="more">\n')
        out.append("          <summary>影响分析 · 核实 · 来源</summary>\n")
        out.append('          <div class="more-body">\n')
        out.extend(body)
        out.append("          </div>\n")
        out.append("        </details>\n")

    out.append("      </article>\n")
    return "".join(out)


def render_section(title: str, items: list) -> str:
    rows = [render_news_item(i) for i in items if isinstance(i, dict)]
    rows = [r for r in rows if r]
    if not rows:
        return ""
    return (
        '      <div class="section-head">\n'
        f"        <h2>{esc(title)}</h2>"
        f'<span class="count">{len(rows)} 条</span>\n'
        "      </div>\n" + "".join(rows)
    )


def render_dashboard(dash: dict) -> str:
    if not isinstance(dash, dict):
        return ""
    rows = []
    for key, label in DASHBOARD_LABELS:
        val = as_text(dash.get(key))
        if val:
            rows.append(
                f"            <tr><th>{esc(label)}</th><td>{esc(val)}</td></tr>\n"
            )
    overall = as_text(dash.get("overall_outlook"))
    conf = as_text(dash.get("confidence"))
    if not rows and not overall:
        return ""

    out = ['      <div class="card">\n', "        <h2>对中国的综合影响</h2>\n"]
    if rows:
        out.append('        <table class="channels">\n' + "".join(rows) + "        </table>\n")
    if overall:
        out.append(f"        <h3>总体展望</h3>\n        <p>{esc(overall)}</p>\n")
    if conf:
        out.append(f"        <h3>判断可信度</h3>\n        <p>{esc(conf)}</p>\n")
    out.append("      </div>\n")
    return "".join(out)


def render_watchlist(watch: dict) -> str:
    if not isinstance(watch, dict):
        return ""

    out = ['      <div class="card">\n', "        <h2>未来 24 小时重点观察</h2>\n"]
    rendered_any = False

    events = [e for e in as_list(watch.get("events")) if isinstance(e, dict)]
    if events:
        rendered_any = True
        out.append('        <table class="channels">\n')
        for e in events:
            name = as_text(e.get("name"))
            when = as_text(e.get("beijing_time"))
            status = as_text(e.get("status"))
            why = as_text(e.get("why_watch"))
            impact_txt = as_text(e.get("potential_impact"))
            cn_link = as_text(e.get("china_link"))
            pri = as_text(e.get("priority"))
            src = as_text(e.get("source"))

            cell = [f"<strong>{esc(name)}</strong>"]
            if when:
                cell.append(f'<br><span class="src-time">北京时间 {esc(when)}</span>')
            if pri:
                cell.append(f' <span class="chip chip-mid">{esc(pri)}</span>')
            if status:
                cell.append(f'<br><span class="src-time">日程状态：{esc(status)}</span>')
            if why:
                cell.append(f"<br>关注原因：{esc(why)}")
            if impact_txt:
                cell.append(f"<br>潜在影响：{esc(impact_txt)}")
            if cn_link:
                cell.append(f"<br>对华关联：{esc(cn_link)}")
            if src:
                cell.append(f"<br><span class=\"src-time\">来源：{linkify(src)}</span>")
            rows_html = "".join(cell)
            out.append(
                f'          <tr><th>{esc(when[:16] if when else "—")}</th><td>{rows_html}</td></tr>\n'
            )
        out.append("        </table>\n")

    def simple_list(key: str, label: str, main_field: str) -> None:
        nonlocal rendered_any
        rows = [r for r in as_list(watch.get(key)) if isinstance(r, dict)]
        if not rows:
            return
        rendered_any = True
        out.append(f"        <h3>{esc(label)}</h3>\n")
        out.append('        <ul class="key-facts">\n')
        for r in rows:
            name = as_text(r.get(main_field)) or as_text(r.get("event")) or as_text(r.get("name"))
            when = as_text(r.get("beijing_time"))
            pri = as_text(r.get("priority"))
            src = as_text(r.get("source"))
            line = esc(name)
            if when:
                line += f"　<span class=\"src-time\">北京时间 {esc(when)}</span>"
            if pri:
                line += f' <span class="chip chip-mid">{esc(pri)}</span>'
            if src:
                line += f'<br><span class="src-time">来源：{linkify(src)}</span>'
            out.append(f"          <li>{line}</li>\n")
        out.append("        </ul>\n")

    simple_list("economic_calendar", "重要经济数据", "event")
    simple_list("policy_announcements", "中国政策与会议", "event")
    simple_list("diplomatic_events", "外交与国际会议", "event")
    simple_list("technology_events", "企业与科技事件", "event")
    simple_list("potential_risks", "风险观察", "risk")

    out.append("      </div>\n")
    return "".join(out) if rendered_any else ""


def render_methodology(config: dict, qc: dict) -> str:
    period = as_text(config.get("coverage_period"))
    generated = as_text(config.get("generated_at"))
    tz = as_text(config.get("timezone")) or "Asia/Shanghai"

    flags = []
    if isinstance(qc, dict):
        mapping = {
            "duplicate_check": "重复条目已合并",
            "major_news_cross_verified": "重大新闻已交叉核实",
            "unverified_claims_marked": "未证实说法已标注",
            "source_urls_checked": "来源链接已核对",
        }
        for k, label in mapping.items():
            if qc.get(k) is True:
                flags.append(label)

    if not period and not flags:
        return ""

    out = ['      <div class="notice">\n']
    out.append("        <strong>编辑说明</strong><br>\n")
    if period:
        out.append(f"        采集窗口：{esc(period)}<br>\n")
    out.append(
        f"        时区：{esc(tz)}；本简报为每日一次的信息整理，不代表全天候实时监测。<br>\n"
    )
    if flags:
        out.append("        " + "；".join(esc(f) for f in flags) + "。<br>\n")
    out.append(
        "        客观事实与媒体分析分开陈述；市场判断存在时间边界，不构成投资建议。\n"
    )
    out.append("      </div>\n")
    return "".join(out)


# --------------------------------------------------------------------------
# 整页渲染
# --------------------------------------------------------------------------
def render_briefing_page(
    briefing: dict, audio_meta: dict, base_url: str
) -> str:
    d = briefing["data"]
    date_str = briefing["date_str"]
    dt = briefing["date"]
    config = d.get("briefing_config") if isinstance(d.get("briefing_config"), dict) else {}
    overview = d.get("daily_overview") if isinstance(d.get("daily_overview"), dict) else {}
    top5 = as_list(d.get("top_5_headlines"))
    qc = d.get("quality_control")

    items = collect_items(d)
    items_by_id = {
        as_text(i.get("id")): i for i in items if as_text(i.get("id"))
    }

    headline = as_text(overview.get("headline")) or first_nonempty(
        (items[0].get("title") if items else ""), SITE_TITLE
    )

    # 阅读时长估算：概览 + 摘要 + 要点
    cn_chars = text_len(
        headline,
        overview.get("international_overview"),
        overview.get("china_overview"),
        overview.get("key_takeaways"),
        [i.get("summary") for i in items],
        [i.get("key_facts") for i in items],
    )
    read_min = max(1, round(cn_chars / 400))

    title = f"{fmt_date_cn(dt)}｜{SITE_TITLE}"
    canonical = f"/news/{date_str}.html"

    page = [render_head(title, headline, canonical, base_url)]
    page.append('      <p class="page-kicker">每日早报</p>\n')
    page.append(f'      <h1 class="page-title">{esc(fmt_date_cn(dt, True))}</h1>\n')
    page.append(
        '      <p class="reading-time">'
        f"<span>{len(items)} 条新闻</span>"
        f"<span>约 {read_min} 分钟阅读</span>"
        f"<span>生成于 {esc(as_text(config.get('generated_at')) or '—')}</span>"
        "</p>\n"
    )

    page.append(render_audio_box(date_str, audio_meta))
    page.append(render_breaking(top5, items_by_id))
    page.append(render_overview(overview, as_list(overview.get("key_takeaways"))))
    page.append(render_top5(top5))

    # 国际
    intl = d.get("international_news") if isinstance(d.get("international_news"), dict) else {}
    intl_html = "".join(
        render_section(label, as_list(intl.get(key))) for key, label in INTL_SECTIONS
    )
    if intl_html:
        page.append(
            '      <div class="section-head"><h2>国际新闻</h2></div>\n'
        )
        page.append(intl_html)

    # 中国
    cn = d.get("china_news") if isinstance(d.get("china_news"), dict) else {}
    cn_html = "".join(
        render_section(label, as_list(cn.get(key))) for key, label in CN_SECTIONS
    )
    if cn_html:
        page.append('      <div class="section-head"><h2>中国新闻</h2></div>\n')
        page.append(cn_html)

    page.append(render_dashboard(d.get("china_impact_dashboard")))
    page.append(render_watchlist(d.get("next_24_hours_watchlist")))
    page.append(render_methodology(config, qc))

    page.append(
        '      <p class="reading-time" style="margin-top:26px">'
        f'<a href="/archive.html">← 查看全部归档</a></p>\n'
    )
    page.append(render_foot(as_text(config.get("generated_at"))))
    return "".join(page)


def render_index_page(
    briefings: list[dict], audio_map: dict, base_url: str
) -> str:
    if not briefings:
        page = [
            render_head(SITE_TITLE, SITE_DESC, "/", base_url),
            '      <div class="empty-state">\n',
            "        <p>还没有任何简报。</p>\n",
            "        <p>把生成的 JSON 放到 <code>news/YYYY/MM/</code> 下并重新构建即可。</p>\n",
            "      </div>\n",
            render_foot(),
        ]
        return "".join(page)

    latest = briefings[0]
    d = latest["data"]
    dt = latest["date"]
    config = d.get("briefing_config") if isinstance(d.get("briefing_config"), dict) else {}
    overview = d.get("daily_overview") if isinstance(d.get("daily_overview"), dict) else {}
    headline = as_text(overview.get("headline")) or SITE_TITLE
    items = collect_items(d)

    page = [render_head(SITE_TITLE, SITE_DESC, "/", base_url)]

    # Hero
    page.append('      <div class="hero">\n')
    page.append(
        f'        <p class="hero-date">{esc(fmt_date_cn(dt, True))}　最新一期</p>\n'
    )
    page.append(f"        <h1>{esc(headline)}</h1>\n")
    page.append(
        '        <div class="hero-actions">\n'
        f'          <a class="btn btn-primary" href="/news/{esc(latest["date_str"])}.html">阅读本期全文</a>\n'
    )
    if audio_map.get(latest["date_str"]):
        page.append(
            f'          <a class="btn" href="/audio/{esc(latest["date_str"])}.mp3">收听语音播报</a>\n'
        )
    page.append('          <a class="btn" href="/archive.html">全部归档</a>\n')
    page.append("        </div>\n      </div>\n")

    page.append(render_audio_box(latest["date_str"], audio_map.get(latest["date_str"], {})))
    page.append(render_overview(overview, as_list(overview.get("key_takeaways"))))
    page.append(render_top5(as_list(d.get("top_5_headlines"))))

    # 最新一期其余条目（简明列表，避免首页过长）
    if len(items) > 5:
        seen = set()
        for t in as_list(d.get("top_5_headlines")):
            if isinstance(t, dict):
                seen.add(as_text(t.get("id")))
        rest = [i for i in items if as_text(i.get("id")) not in seen]
        if rest:
            page.append(
                '      <div class="card">\n        <h2>本期其他要闻</h2>\n'
                '        <ul class="key-facts">\n'
            )
            for i in rest:
                t = as_text(i.get("title"))
                sc = i.get("importance_score")
                sc_html = (
                    f' <span class="{score_class(sc)}">{esc(sc)}</span>'
                    if sc not in (None, "")
                    else ""
                )
                page.append(f"          <li>{esc(t)}{sc_html}</li>\n")
            page.append("        </ul>\n")
            page.append(
                f'        <p style="margin-top:14px"><a href="/news/{esc(latest["date_str"])}.html">'
                "查看全部详情（含影响分析与信息来源）→</a></p>\n"
            )
            page.append("      </div>\n")

    # 近期几期
    if len(briefings) > 1:
        page.append(
            '      <div class="card">\n        <h2>往期回顾</h2>\n'
            '        <ul class="archive-list">\n'
        )
        for b in briefings[1:6]:
            bd = b["data"]
            bo = bd.get("daily_overview") if isinstance(bd.get("daily_overview"), dict) else {}
            raw_head = as_text(bo.get("headline"))
            bhead = truncate_cn(raw_head, 46) or SITE_TITLE
            page.append(
                f'          <li><a href="/news/{esc(b["date_str"])}.html">'
                f'<span class="a-date">{esc(b["date_str"])}</span>'
                f'<span class="a-title">{esc(bhead)}</span></a></li>\n'
            )
        page.append("        </ul>\n")
        page.append(
            f'        <p style="margin-top:14px"><a href="/archive.html">全部 {len(briefings)} 期 →</a></p>\n'
        )
        page.append("      </div>\n")

    page.append(render_foot(as_text(config.get("generated_at"))))
    return "".join(page)


def render_archive_page(
    briefings: list[dict], audio_map: dict, base_url: str
) -> str:
    page = [render_head(f"全部归档｜{SITE_TITLE}", SITE_DESC, "/archive.html", base_url)]
    page.append('      <p class="page-kicker">归档</p>\n')
    page.append('      <h1 class="page-title">全部归档</h1>\n')
    page.append(
        f'      <p class="reading-time"><span>共 {len(briefings)} 期</span></p>\n'
    )

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
            d = b["data"]
            o = d.get("daily_overview") if isinstance(d.get("daily_overview"), dict) else {}
            raw_head = as_text(o.get("headline"))
            head = truncate_cn(raw_head, 52) or SITE_TITLE
            items = collect_items(d)
            top_score = ""
            for i in items:
                sc = i.get("importance_score")
                try:
                    if float(sc) >= 9:
                        top_score = f"{sc} 分"
                        break
                except (TypeError, ValueError):
                    continue
            audio_tag = " · 含语音" if audio_map.get(b["date_str"]) else ""
            page.append(
                f'          <li><a href="/news/{esc(b["date_str"])}.html">'
                f'<span class="a-date">{esc(fmt_date_cn(b["date"], True))}</span>'
                f'<span class="a-title">{esc(head)}{esc(audio_tag)}</span>'
                f'<span class="a-score">{esc(top_score)}</span></a></li>\n'
            )
        page.append("        </ul>\n      </div>\n")

    page.append(render_foot())
    return "".join(page)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="把新闻 JSON 构建成静态网站")
    parser.add_argument("--news-dir", default="news", help="JSON 根目录（默认 news）")
    parser.add_argument("--audio-dir", default="dist/audio", help="音频目录（默认 dist/audio）")
    parser.add_argument("--out-dir", default="dist", help="输出目录（默认 dist）")
    parser.add_argument(
        "--base-url", default="https://daily-news.cc.cd", help="站点根地址，用于 canonical"
    )
    parser.add_argument(
        "--static-dir", default="site", help="静态资源目录，其内容会被复制到输出根"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="遇到非法 JSON 直接失败（CI 发布闸门用）",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只校验 JSON 是否合法，不生成任何文件",
    )
    args = parser.parse_args()

    news_dir = Path(args.news_dir)
    out_dir = Path(args.out_dir)
    audio_dir = Path(args.audio_dir)
    static_dir = Path(args.static_dir)

    print(f"读取 JSON：{news_dir}")
    try:
        briefings = load_briefings(news_dir, strict=args.strict)
    except ValueError as exc:
        print(f"  !! {exc}", file=sys.stderr)
        print("\n构建中止：存在非法 JSON 文件。", file=sys.stderr)
        return 1
    print(f"  找到 {len(briefings)} 期简报")

    if args.check_only:
        print(f"\n校验通过：{len(briefings)} 个 JSON 文件格式合法。")
        return 0

    if not briefings:
        print("  !! 没有可构建的简报", file=sys.stderr)

    # 音频元数据
    audio_map: dict[str, dict] = {}
    if audio_dir.exists():
        for meta_path in sorted(audio_dir.glob("*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                date_key = meta.get("date") or meta_path.stem
                mp3 = audio_dir / f"{date_key}.mp3"
                if mp3.exists() and mp3.stat().st_size > 1024:
                    audio_map[str(date_key)] = meta
            except Exception:  # noqa: BLE001
                continue
    print(f"  找到 {len(audio_map)} 个可用音频")

    # 准备输出目录
    out_dir.mkdir(parents=True, exist_ok=True)

    # 复制静态资源（不删除 dist/audio）
    if static_dir.exists():
        for item in static_dir.iterdir():
            dest = out_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        print(f"  复制静态资源：{static_dir} -> {out_dir}")

    # 简报页
    news_out = out_dir / "news"
    news_out.mkdir(parents=True, exist_ok=True)
    for b in briefings:
        html_doc = render_briefing_page(b, audio_map.get(b["date_str"], {}), args.base_url)
        (news_out / f"{b['date_str']}.html").write_text(html_doc, encoding="utf-8")

    # 首页 / 归档
    (out_dir / "index.html").write_text(
        render_index_page(briefings, audio_map, args.base_url), encoding="utf-8"
    )
    (out_dir / "archive.html").write_text(
        render_archive_page(briefings, audio_map, args.base_url), encoding="utf-8"
    )

    # 顺手输出一个机器可读索引，便于以后接 API / 搜索
    index_data = [
        {
            "date": b["date_str"],
            "headline": as_text(
                (b["data"].get("daily_overview") or {}).get("headline")
                if isinstance(b["data"].get("daily_overview"), dict)
                else ""
            ),
            "news_count": len(collect_items(b["data"])),
            "has_audio": b["date_str"] in audio_map,
            "url": f"/news/{b['date_str']}.html",
        }
        for b in briefings
    ]
    (out_dir / "archive.json").write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # robots.txt 里声明了 sitemap.xml，这里必须真的生成，否则是死链
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
    add_url("/archive.html", briefings[0]["date_str"] if briefings else "", "0.8")
    for b in briefings:
        add_url(f"/news/{b['date_str']}.html", b["date_str"], "0.7")

    sitemap.append("</urlset>")
    (out_dir / "sitemap.xml").write_text("\n".join(sitemap) + "\n", encoding="utf-8")

    print(f"\n构建完成：{out_dir}")
    print(f"  首页    {out_dir / 'index.html'}")
    print(f"  归档    {out_dir / 'archive.html'}")
    print(f"  期次页  {len(briefings)} 个 -> {news_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
