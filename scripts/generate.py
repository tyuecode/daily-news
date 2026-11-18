"""每日热点简报站 v2 —— 核心生成脚本（L0–L8）。

流程：
L0 准备：读历史快照 / 生成 run_id / 处理音频（先把旧 mp3 挪走）
L1 采集：并发抓英文 RSS + 并发抓 newsnow 热榜
L2 快照与热度计算：保存快照、读历史算 frequency、跨平台合并算 hotness、加权排序
L3 翻译：英文标题分批（8/批）翻译，中文热榜用原文，失败回退英文
L4 理解：直接展示标题，不生成概括句；国际热点做轻量聚类（多家媒体并列）
L5 播报稿：排名靠前若干条改写，600 字内，强制完整结尾
L6 语音：edge-tts 7.x，8 次重试 + 线性退避；失败降级浏览器朗读
L7 发布闸门：国内/国际各 >=3 条、链接合法、中文占比、音频头校验
L8 输出：写 entries.json、渲染首页与归档、清理 30 天前、写运行报告

支持 --dry-run：所有产物写进 _dryrun/，使用假数据，零网络、零 CF 额度。
密钥只从环境变量读取，绝不写进代码、注释或日志。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

# 保证从任意工作目录执行都能 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_client
import hot_client

# ── 路径配置（根目录 = 脚本所在目录的上一级）────────────────────────
ROOT = Path(__file__).resolve().parent.parent

# ── 数据源配置 ──────────────────────────────────────────────────────
INTL_FEEDS = [
    {"name": "BBC", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "publisher": "BBC"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "publisher": "The Guardian"},
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "publisher": "Al Jazeera"},
    {"name": "France 24", "url": "https://www.france24.com/en/rss", "publisher": "France 24"},
    {"name": "NPR", "url": "https://feeds.npr.org/1004/rss.xml", "publisher": "NPR"},
    {"name": "Sky News", "url": "https://feeds.skynews.com/feeds/rss/world.xml", "publisher": "Sky News"},
    {"name": "Google News", "url": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en", "publisher": "Google News"},
]

KEEP_DAYS = 30                  # entries 与归档保留天数
RECENT_DAYS = 7                 # 首页最近几天摘要列表
MAX_RAW_NEWS = 100              # raw_news 最多条数
INTL_TOP_N = 15                 # 国际热点展示条数
DOMESTIC_TOP_N = 8              # 国内热点展示条数：大事件优先，质量优先不凑数
SNAPSHOT_KEEP_DAYS = 7          # 快照只留 7 天（frequency 只看 7 天）
BROADCAST_POINTS = 12           # 播报稿要点数
MIN_SECTION_ITEMS = 3           # 闸门：每个版块至少条数
MIN_CHINESE_RATIO = 0.60        # 闸门：中文占比下限
TTS_RETRIES = 8                 # edge-tts 重试次数（服务端随机抖动，3 次不够）
TTS_VOICE = "zh-CN-XiaoxiaoNeural"
RSS_TIMEOUT = 20
FETCH_WORKERS = 4
BEIJING_TZ = hot_client.BEIJING_TZ


class Paths:
    """一次性计算所有输出路径；dry-run 时把根目录换成 _dryrun。"""

    def __init__(self, root: Path, dry_run: bool = False):
        self.root = root
        self.dry_run = dry_run
        base = root / "_dryrun" if dry_run else root
        self.base = base
        self.data = base / "data"
        self.snapshots = self.data / "snapshots"
        self.runs = self.data / "runs"
        self.archive = base / "archive"
        self.audio = base / "audio"
        self.index = base / "index.html"
        self.entries = self.data / "entries.json"
        self.audio_mp3 = self.audio / "daily.mp3"
        self.audio_backup = self.audio / "daily.mp3.bak"

    def ensure_dirs(self):
        for path in (self.data, self.snapshots, self.runs, self.archive, self.audio):
            path.mkdir(parents=True, exist_ok=True)


def log(message: str):
    print(f"[每日热点] {message}", flush=True)


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


# ── L0 准备 ─────────────────────────────────────────────────────────

def prepare_audio(audio_mp3: Path, backup: Path, logger: Callable = log):
    """先把旧 mp3 挪走（不是删），闸门不通过时还原，避免旧音频配新稿。"""
    if audio_mp3.exists():
        if backup.exists():
            backup.unlink()
        audio_mp3.rename(backup)
        logger("已把旧音频挪到备份位置")


def restore_audio(audio_mp3: Path, backup: Path):
    if backup.exists():
        if audio_mp3.exists():
            audio_mp3.unlink()
        backup.rename(audio_mp3)


def load_entries(entries_path: Path) -> dict:
    try:
        with open(entries_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("days"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"days": []}


def run_id(now: datetime) -> str:
    return now.astimezone(BEIJING_TZ).strftime("%Y%m%d-%H%M%S")


# ── L1 采集 ─────────────────────────────────────────────────────────

def _html_to_text_and_image(summary_html: str) -> tuple[str, str]:
    """从 RSS 摘要中提取纯文本与图片 URL。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(summary_html or "", "html.parser")
    image = ""
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src.startswith("http"):
            image = src
            break
    if not image:
        # media:thumbnail / enclosure 由 feedparser 单独传入
        pass
    text = soup.get_text(" ", strip=True)
    return text, image


def parse_feed_entry(entry, feed: dict) -> dict | None:
    """把一条 feedparser 条目标准化；无标题或无链接则丢弃。"""
    title = str(getattr(entry, "title", "") or "").strip()
    link = str(getattr(entry, "link", "") or "").strip()
    if not title or not link.lower().startswith("http"):
        return None

    summary_html = ""
    if getattr(entry, "summary", None):
        summary_html = str(entry.summary)
    elif getattr(entry, "content", None):
        first = entry.content[0]
        summary_html = str(first.get("value", "")) if isinstance(first, dict) else ""
    summary_text, image = _html_to_text_and_image(summary_html)

    # 图片兜底：media:thumbnail / enclosure（按 type 过滤音频）
    if not image:
        thumb = getattr(entry, "media_thumbnail", None)
        if isinstance(thumb, list) and thumb:
            image = str(thumb[0].get("url", "") or "")
    if not image:
        for enc in getattr(entry, "enclosures", []) or []:
            if str(enc.get("type", "")).startswith("image/"):
                image = str(enc.get("url", "") or "")
                break
    image = upgrade_image_url(image)

    published = ""
    for attr in ("published_parsed", "updated_parsed"):
        struct = getattr(entry, attr, None)
        if struct:
            try:
                published = datetime(*struct[:6]).strftime("%Y-%m-%d")
                break
            except (TypeError, ValueError):
                pass

    return {
        "title_en": title,
        "title_zh": "",             # L3 填充
        "url": link,
        "source": feed["publisher"],
        "publisher": feed["publisher"],
        "summary_en": summary_text[:300],
        "image": image,
        "published": published,
        "section": "international",
    }


def upgrade_image_url(url: str) -> str:
    """图片 URL 归一：http→https；BBC 240→1024；去 resize；Guardian width=1200。"""
    if not url:
        return ""
    url = url.replace("http://", "https://", 1)
    if "ichef.bbci.co.uk" in url:
        url = re.sub(r"/standard/\d+/", "/standard/1024/", url)
    if "aljazeera" in url or "france24" in url:
        url = re.sub(r"\?resize=[^&]*", "", url)
    if "theguardian" in url:
        url = re.sub(r"([?&])width=\d+", r"\1width=1200", url)
    return url


def is_fresh(entry: dict, now: datetime, max_age_days: int = 7) -> bool:
    """新鲜度守卫：最新条目超过 7 天的整源丢弃（防湿货源）。

    now 通常是北京时间（aware），而 published 是朴素日期字符串；统一转成
    朴素日期比较，避免 offset-naive / offset-aware 相减报错。
    """
    published = entry.get("published")
    if not published:
        return True  # 无时间字段不误杀（如新华网），交给其他规则
    try:
        pub = datetime.strptime(published, "%Y-%m-%d")
        # 去掉 tzinfo 后比较，避免时区混用
        naive_now = now.replace(tzinfo=None) if now.tzinfo else now
        return (naive_now - pub).days <= max_age_days
    except (ValueError, TypeError):
        return True


def fetch_single_feed(feed: dict, timeout: int = RSS_TIMEOUT,
                      fetch_fn: Callable | None = None) -> tuple[str, list[dict]]:
    """抓取并解析一个 RSS；返回 (源名, 条目列表)。失败返回空列表。"""
    import feedparser

    fetch_fn = fetch_fn or (lambda url, t: feedparser.parse(url))
    try:
        parsed = fetch_fn(feed["url"], timeout)
        if getattr(parsed, "bozo", 0) and not getattr(parsed, "entries", None):
            return feed["name"], []
        entries = []
        for raw in getattr(parsed, "entries", [])[:20]:
            item = parse_feed_entry(raw, feed)
            if item:
                entries.append(item)
        # 新鲜度守卫：若最新条目超过 7 天，整源丢弃
        if entries and not is_fresh(entries[0], beijing_now()):
            return feed["name"], []
        return feed["name"], entries
    except Exception as exc:
        log(f"警告：RSS 源「{feed['name']}」抓取失败：{exc}")
        return feed["name"], []


def fetch_all_feeds(feeds: list[dict] | None = None,
                    fetch_fn: Callable | None = None) -> list[dict]:
    """并发抓取全部 RSS（单源超时 20s，失败跳过并记录）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    feeds = feeds or INTL_FEEDS
    results = []
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(fetch_single_feed, f, RSS_TIMEOUT, fetch_fn): f for f in feeds}
        for future in as_completed(futures):
            name, items = future.result()
            log(f"RSS「{name}」获得 {len(items)} 条")
            results.extend(items)
    return results


# ── L2 快照与热度 ───────────────────────────────────────────────────

def save_snapshot(hot_items: list[dict], paths: Paths, now: datetime):
    """保存本次热榜快照到 data/snapshots/YYYY-MM-DD-HHMM.json。"""
    payload = {
        "captured_at": now.astimezone(BEIJING_TZ).isoformat(),
        "items": [{"title": x["title"], "platform": x["platform"], "rank": x["rank"]}
                  for x in hot_items],
    }
    path = paths.snapshots / hot_client.snapshot_filename(now)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def score_hot_items(hot_items: list[dict], paths: Paths, now: datetime) -> list[dict]:
    """计算 frequency / hotness / score 并排序。

    先保存本次快照，再读最近 7 天快照（含本次）算 frequency；
    跨平台合并同一话题算 hotness（source_count）；最后加权排序。
    """
    save_snapshot(hot_items, paths, now)
    snapshots = hot_client.load_recent_snapshots(str(paths.snapshots), now)
    # 确保本次快照已计入（load_recent_snapshots 从磁盘读取，应该包含刚写的文件）
    if not snapshots:
        snapshots = [{"items": hot_items}]
    frequencies = hot_client.count_frequency(hot_items, snapshots)

    for item, freq in zip(hot_items, frequencies):
        item["frequency"] = freq
        item["is_new"] = freq <= 1
        # hotness 暂时按单平台 1 计算；merge 后会重算
        item["hotness"] = 1
        item["score"] = hot_client.score_item(
            item["rank"], item.get("rank_total", 30), freq, 1
        )

    merged = hot_client.merge_platform_clusters(hot_items)
    # merge 后 hotness/score 需要重算（此时 hotness 已是跨平台的簇大小）
    for item in merged:
        item["score"] = hot_client.score_item(
            item["rank"], item.get("rank_total", 30),
            item.get("frequency", 1), item.get("hotness", 1)
        )
    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged


# ── L3 翻译 ─────────────────────────────────────────────────────────

def translate_international(items: list[dict], ai_text: Callable | None = None) -> list[dict]:
    """分批翻译国际标题；title_zh 填充，失败回退英文。"""
    en_titles = [x["title_en"] for x in items]
    zh_titles = ai_client.translate_titles(en_titles, ai_text=ai_text, logger=log)
    for item, zh in zip(items, zh_titles):
        item["title_zh"] = zh
    return items


# ── L4 理解（国际轻量聚类）─────────────────────────────────────────

def cluster_international(items: list[dict], threshold: float = 0.35) -> list[dict]:
    """同一事件的多家媒体报道并到一张卡片（用中文译文 bigram Jaccard）。

    与国内 merge 不同：这里按 title_zh 聚类，且同一来源（publisher）之间不合并，
    避免同一家媒体的两篇报道被误判为「多家媒体报道」。
    """
    clusters: list[list[dict]] = []
    for item in items:
        placed = False
        for cluster in clusters:
            publishers = {x["publisher"] for x in cluster}
            if item["publisher"] in publishers:
                continue  # 同来源不合并
            if any(hot_client.bigram_jaccard(item["title_zh"], x["title_zh"]) >= threshold
                   for x in cluster):
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    merged = []
    for cluster in clusters:
        # 主条目：source_count 最多的簇里取第一条（按原始顺序）
        primary = dict(cluster[0])
        publishers = {x["publisher"] for x in cluster}
        primary["source_count"] = len(publishers)
        primary["cluster"] = [
            {
                "source": x["publisher"],
                "title_zh": x["title_zh"],
                "title_en": x["title_en"],
                "url": x["url"],
                "image": x.get("image", ""),
                "is_primary": i == 0,
            }
            for i, x in enumerate(cluster)
        ]
        primary["image"] = cluster[0].get("image", "")
        merged.append(primary)

    merged.sort(key=lambda x: x["source_count"], reverse=True)
    return merged


# ── L5/L6 播报稿与语音 ─────────────────────────────────────────────

def build_broadcast(domestic: list[dict], international: list[dict],
                    now: datetime, ai_text: Callable | None = None) -> str:
    """用国内外排名靠前的条目改写播报稿。"""
    points = []
    for item in domestic[:6]:
        points.append(f"国内：{item['title']}")
    for item in international[:6]:
        title = item.get("title_zh") or item.get("title_en", "")
        points.append(f"国际：{title}")
    date_str = f"{now.month}月{now.day}日"
    return ai_client.generate_broadcast(points[:BROADCAST_POINTS], date_str, ai_text=ai_text, logger=log)


def is_valid_mp3(path: Path) -> bool:
    """按文件头校验 MP3：ID3 或 0xFF 帧同步（byte[1] & 0xE0 == 0xE0）。"""
    try:
        with open(path, "rb") as fh:
            header = fh.read(3)
    except OSError:
        return False
    if len(header) < 2:
        return False
    if header[:3] == b"ID3":
        return True
    return header[0] == 0xFF and (header[1] & 0xE0) == 0xE0


def generate_tts(text: str, target: Path, retries: int = TTS_RETRIES) -> bool:
    """edge-tts 7.x 生成语音，8 次重试 + 线性退避；成功写文件并返回 True。"""
    import edge_tts

    async def _save():
        communicate = edge_tts.Communicate(text, voice=TTS_VOICE)
        await communicate.save(str(target))

    for attempt in range(retries):
        try:
            asyncio.run(_save())
            if is_valid_mp3(target):
                return True
            log(f"TTS 第 {attempt + 1} 次返回非音频内容，重试")
        except Exception as exc:
            log(f"TTS 第 {attempt + 1} 次失败：{type(exc).__name__}")
        time.sleep(attempt + 1)  # 线性退避 1s, 2s, 3s ...
    return False


def generate_tts_fake(text: str, target: Path, retries: int = TTS_RETRIES) -> bool:
    """dry-run 用的假 TTS：写一个合法的 MP3 文件头，零网络。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)
    return True


# ── L7 发布闸门 ─────────────────────────────────────────────────────

def chinese_ratio(text: str) -> float:
    """统计中文字符占比（忽略空白与标点以外的字符）。"""
    if not text:
        return 0.0
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    total = len(re.sub(r"\s", "", text))
    return chinese / total if total else 0.0


def passes_gate(domestic: list[dict], international: list[dict],
                tts_ok: bool, audio_path: Path | None, logger: Callable = log) -> tuple[bool, str]:
    """发布闸门：条数、链接合法、中文占比、音频头校验。返回 (是否通过, 原因)。"""
    if len(domestic) < MIN_SECTION_ITEMS:
        return False, f"国内热点只有 {len(domestic)} 条，少于 {MIN_SECTION_ITEMS} 条"
    if len(international) < MIN_SECTION_ITEMS:
        return False, f"国际热点只有 {len(international)} 条，少于 {MIN_SECTION_ITEMS} 条"

    all_items = domestic + international
    for item in all_items:
        url = item.get("url", "")
        if not str(url).lower().startswith("http"):
            return False, f"条目「{item.get('title', '')}」链接不合法：{url}"
        for member in item.get("cluster", []):
            member_url = member.get("url", "")
            if member_url and not str(member_url).lower().startswith("http"):
                return False, f"卡片内链接不合法：{member_url}"

    titles = "".join(
        (item.get("title", "") + item.get("title_zh", "")) for item in all_items
    )
    ratio = chinese_ratio(titles)
    if ratio <= MIN_CHINESE_RATIO:
        return False, f"中文占比仅 {ratio:.1%}，低于 {MIN_CHINESE_RATIO:.0%}"

    if tts_ok:
        if audio_path is None or not is_valid_mp3(audio_path):
            return False, "声明 tts_ok=true 但音频头不合法"

    logger(f"发布闸门通过：国内 {len(domestic)} 条，国际 {len(international)} 条，中文占比 {ratio:.1%}")
    return True, "通过"


# ── L8 输出 ─────────────────────────────────────────────────────────

def render_day(day: dict, is_today: bool, site_url: str = "") -> str:
    """渲染单日页面（首页与归档共用同一模板，保证一致）。"""
    date = day["date"]
    sections = day.get("sections", {})
    domestic = sections.get("domestic", [])
    international = sections.get("international", [])
    updated = day.get("updated_at", "")

    parts = []
    parts.append("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    parts.append(f"<title>每日热点简报 · {html.escape(date)}</title>")
    parts.append(_CSS)
    parts.append("</head><body><div class=\"wrap\">")

    # ── 顶部：标题 + 今日一句话摘要 + 概览数字条 ──
    parts.append("<header><h1>每日热点简报</h1>")
    parts.append(f"<p class=\"sub\">更新于 {html.escape(updated)}</p></header>")

    brief = build_today_brief(day)
    if brief:
        parts.append(f"<div class=\"brief\"><span class=\"brief-label\">今日焦点</span>"
                     f"<span class=\"brief-text\">{html.escape(brief)}</span></div>")

    platform_count = len({x.get("platform") for x in domestic if x.get("platform")})
    parts.append(
        f"<div class=\"overview\"><span><b>{len(domestic)}</b> 条国内热点</span>"
        f"<span class=\"sep\">·</span><span><b>{len(international)}</b> 条国际热点</span>"
        f"<span class=\"sep\">·</span><span>来自 <b>{platform_count}</b> 个热榜平台</span></div>"
    )

    # 版块快速跳转（手机上一键到国内/国际）
    parts.append("<nav class=\"jump\"><a href=\"#domestic\">国内热点 ↓</a>"
                 "<a href=\"#international\">国际热点 ↓</a></nav>")

    # 今日播报
    parts.append("<section class=\"broadcast\" id=\"broadcast\"><h2>今日播报</h2>")
    broadcast = day.get("broadcast", "")
    if is_today and day.get("tts_ok") and broadcast:
        parts.append("<audio controls preload=\"none\" src=\"audio/daily.mp3\"></audio>")
    elif is_today and broadcast:
        parts.append("<button type=\"button\" onclick=\"speak()\">朗读播报</button>")
    if broadcast:
        parts.append("<details><summary>展开播报稿</summary>")
        parts.append(f"<p class=\"script\">{html.escape(broadcast)}</p></details>")
    else:
        parts.append("<p class=\"empty\">暂无播报稿</p>")
    parts.append("</section>")

    # 国内热点（前 3 条头条样式）
    parts.append("<section id=\"domestic\"><h2>国内热点</h2>")
    parts.append(_render_cards(domestic, kind="domestic", headline_count=3))
    parts.append("</section>")

    # 国际热点（前 2 条头条样式）
    parts.append("<section id=\"international\"><h2>国际热点</h2>")
    parts.append(_render_cards(international, kind="international", headline_count=2))
    parts.append("</section>")

    # 首页：最近 7 天摘要（时间线样式）
    if is_today:
        recent = day.get("recent_days", [])
        if recent:
            parts.append("<section class=\"recent\"><h2>往期回顾</h2><ul class=\"timeline\">")
            for item in recent:
                parts.append(
                    "<li><div class=\"day-line\">"
                    f"<span class=\"dot\"></span><a href=\"archive/{html.escape(item['date'])}.html\">"
                    f"{html.escape(item['date'])}</a></div>"
                    f"<div class=\"day-sum\">{html.escape(item.get('summary', ''))}</div></li>"
                )
            parts.append("</ul></section>")
    else:
        parts.append("<p class=\"note\">本页为归档，音频仅当日有效。</p>")
        # 归档页在 /archive/ 下，返回首页必须是 ../index.html（相对路径），
        # 否则会解析成 /archive/index.html 死链。site_url 为空时使用相对路径。
        home_href = html.escape(site_url) if site_url else "../index.html"
        parts.append(f"<p><a href=\"{home_href}\">返回首页</a></p>")

    # 悬浮回到顶部（放在 div 外，避免被 wrap 的层级裁剪）
    parts.append("</div><a class=\"to-top\" href=\"#\" aria-label=\"回到顶部\">↑</a>")

    # 播报降级脚本（放在末尾，JSON 中把 < > & 转成 Unicode 转义，杜绝任何标签注入）
    safe_json = (json.dumps(broadcast, ensure_ascii=False)
                 .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    script = (
        "<script>function speak(){if('speechSynthesis' in window){"
        "var u=new SpeechSynthesisUtterance(" + safe_json + ");"
        "u.lang='zh-CN';window.speechSynthesis.speak(u);}"
        "else{alert('当前浏览器不支持朗读');}}</script>"
    )
    parts.append(script)
    parts.append("</body></html>")
    return "\n".join(parts)


def _render_cards(items: list[dict], kind: str, headline_count: int = 3) -> str:
    if not items:
        return "<p class=\"empty\">暂无数据，请稍后再看。</p>"
    out = ["<div class=\"cards\">"]
    for index, item in enumerate(items):
        is_headline = index < headline_count
        rank = item.get("rank", 0)
        rank_label = str(rank) if rank else "—"  # 国际条目无榜单排名，显示破折号
        title = item.get("title") if kind == "domestic" else (item.get("title_zh") or item.get("title_en", ""))
        card_class = "card headline" if is_headline else "card"
        out.append(f"<article class=\"{card_class}\">")
        out.append(f"<div class=\"rank\">{html.escape(rank_label)}</div>")
        out.append("<div class=\"body\">")
        out.append(f"<h3>{html.escape(str(title))}</h3>")
        if kind == "international" and item.get("title_en"):
            # 英文原标题弱化：小字灰色，hover 才稍明显
            out.append(f"<p class=\"en\">{html.escape(item['title_en'])}</p>")

        # 来源与热点标记（徽章化）
        out.append("<div class=\"meta-line\">")
        if item.get("platform"):
            out.append(f"<span class=\"source\">{html.escape(item['platform'])}</span>")
        elif item.get("publisher"):
            out.append(f"<span class=\"source\">{html.escape(item['publisher'])}</span>")
        if item.get("is_new"):
            out.append("<span class=\"badge new\">🆕 新增</span>")
        elif item.get("frequency", 0) > 1:
            out.append(f"<span class=\"badge hot\">持续 {item['frequency']} 次</span>")
        if item.get("source_count", 0) > 1:
            out.append(f"<span class=\"badge multi\">{item['source_count']} 家媒体报道</span>")
        out.append("</div>")

        url = item.get("url", "")
        if url:
            out.append(f"<a class=\"link\" href=\"{html.escape(url)}\" target=\"_blank\" rel=\"noopener\">查看原文 ›</a>")

        # 多家媒体报道：默认折叠；展开区才显示图片
        cluster = item.get("cluster", [])
        if len(cluster) > 1:
            out.append("<details class=\"views\"><summary>查看各家报道 ▸</summary>")
            out.append(f"<p class=\"hint\">以下报道来自 {len(cluster)} 家媒体，观点可能不同，请自行判断</p>")
            for member in cluster:
                out.append("<div class=\"view\">")
                # 图片只在展开区显示，不进默认视图（避免拖慢 30 秒速读）
                if member.get("image"):
                    out.append(f"<img class=\"view-img\" src=\"{html.escape(member['image'])}\" "
                               "loading=\"lazy\" onerror=\"this.style.display='none'\">")
                out.append(f"<a href=\"{html.escape(member.get('url', ''))}\" target=\"_blank\" rel=\"noopener\">"
                           f"{html.escape(member.get('source', ''))}："
                           f"{html.escape(member.get('title_zh') or member.get('title', ''))}</a>")
                if member.get("title_en"):
                    out.append(f"<span class=\"en\">{html.escape(member['title_en'])}</span>")
                out.append("</div>")
            out.append("</details>")
        out.append("</div></article>")
    out.append("</div>")
    return "\n".join(out)


_CSS = """
<style>
:root{
  --bg:#f4f5f7;--card:#fff;--text:#1a1a1a;--muted:#8a8f98;--accent:#e64340;
  --accent-soft:#fff0ef;--line:#eceef1;--shadow:0 2px 10px rgba(20,30,50,.06);
  --headline-bg:linear-gradient(135deg,#fff 0%,#fff8f7 100%);
}
@media(prefers-color-scheme:dark){
  :root{
    --bg:#14161a;--card:#1e2126;--text:#e8eaed;--muted:#9aa0aa;--accent:#ff6b66;
    --accent-soft:#3a2424;--line:#2d3138;--shadow:0 2px 10px rgba(0,0,0,.3);
    --headline-bg:linear-gradient(135deg,#1e2126 0%,#2a2020 100%);
  }
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:720px;margin:0 auto;padding:16px 14px 40px}
header{padding:8px 2px 0}
header h1{font-size:27px;margin:6px 0 2px;letter-spacing:.5px}
.sub{color:var(--muted);font-size:13px;margin:0}

/* A1 今日一句话摘要 */
.brief{margin-top:14px;background:var(--accent-soft);border:1px solid var(--accent);
  border-radius:12px;padding:12px 14px;display:flex;gap:10px;align-items:baseline}
.brief-label{flex:0 0 auto;background:var(--accent);color:#fff;font-size:12px;
  font-weight:700;padding:2px 8px;border-radius:5px}
.brief-text{font-size:15px;font-weight:600;line-height:1.5}

/* B5 概览数字条 */
.overview{margin-top:10px;color:var(--muted);font-size:13px;display:flex;gap:8px;flex-wrap:wrap}
.overview b{color:var(--text);font-size:15px}
.overview .sep{opacity:.5}

/* B6 版块跳转 */
.jump{margin-top:14px;display:flex;gap:10px}
.jump a{flex:1;text-align:center;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:11px 6px;color:var(--text);font-size:14px;
  text-decoration:none;min-height:44px;line-height:22px}
.jump a:active{background:var(--accent-soft)}

h2{font-size:20px;margin:30px 0 12px;padding-left:11px;border-left:4px solid var(--accent);
  scroll-margin-top:16px}

/* 今日播报 */
.broadcast{background:var(--card);border-radius:14px;padding:16px;box-shadow:var(--shadow)}
.broadcast audio{width:100%;margin-bottom:8px}
.broadcast button{background:var(--accent);color:#fff;border:0;border-radius:9px;
  padding:12px 20px;font-size:16px;min-height:44px;cursor:pointer}
.script{white-space:pre-wrap;color:var(--text);font-size:15px;margin:10px 0 0}
summary{cursor:pointer;min-height:44px;line-height:44px;color:var(--accent);font-weight:600}

/* 卡片 */
.cards{display:flex;flex-direction:column;gap:10px}
.card{display:flex;background:var(--card);border-radius:13px;padding:14px;
  box-shadow:var(--shadow);border-left:3px solid transparent}
/* A2 头条样式分级 */
.card.headline{background:var(--headline-bg);border-left-color:var(--accent);
  box-shadow:0 4px 16px rgba(230,67,64,.10)}
.card.headline h3{font-size:19px;line-height:1.45}
.card.headline .rank{width:40px;height:40px;font-size:17px}
.rank{flex:0 0 34px;width:34px;height:34px;border-radius:9px;background:var(--accent);
  color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;
  font-size:15px;margin-right:12px}
.headline .rank{background:linear-gradient(135deg,#ff7a5c,#e64340)}
.body{flex:1;min-width:0}
h3{font-size:16px;margin:1px 0 7px;line-height:1.5}

/* A3 英文原标题弱化 */
.en{color:var(--muted);font-size:12px;margin:2px 0;opacity:.75;transition:opacity .2s}
.card:hover .en{opacity:1}

/* A4 热点标记徽章 */
.meta-line{margin:7px 0;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.source{color:var(--muted);font-size:12px}
.badge{font-size:11px;padding:2px 7px;border-radius:20px;font-weight:600;white-space:nowrap}
.badge.new{background:#e8f5e9;color:#2e7d32}
.badge.hot{background:#fff3e0;color:#e65100}
.badge.multi{background:var(--accent-soft);color:var(--accent)}
@media(prefers-color-scheme:dark){
  .badge.new{background:#1b3320;color:#81c784}
  .badge.hot{background:#3a2a15;color:#ffb74d}
}
.link{color:var(--accent);font-size:13px;text-decoration:none}
.views{margin-top:9px;border-top:1px solid var(--line);padding-top:4px}
.hint{color:var(--muted);font-size:12px;background:var(--accent-soft);
  padding:8px 10px;border-radius:7px;margin:6px 0}
.view{display:flex;flex-direction:column;gap:2px;padding:9px 0;
  border-bottom:1px solid var(--line);font-size:14px}
.view a{color:var(--text);text-decoration:none}
/* C8 展开区图片 */
.view-img{width:100%;max-height:160px;object-fit:cover;border-radius:8px;margin-bottom:4px}
.empty,.note{color:var(--muted);padding:12px;background:var(--card);border-radius:10px}

/* B7 往期回顾时间线 */
.recent ul.timeline{list-style:none;margin:0;padding:0 0 0 6px}
.recent li{border-left:2px solid var(--line);padding:0 0 14px 18px;position:relative}
.recent li:last-child{border-left-color:transparent}
.recent .dot{position:absolute;left:-6px;top:4px;width:10px;height:10px;
  border-radius:50%;background:var(--accent)}
.recent .day-line{font-size:15px;font-weight:600}
.recent a{color:var(--text);text-decoration:none}
.recent .day-sum{color:var(--muted);font-size:13px;margin-top:2px}

/* B6 悬浮回到顶部 */
.to-top{position:fixed;right:18px;bottom:24px;width:44px;height:44px;
  border-radius:50%;background:var(--accent);color:#fff;font-size:22px;
  display:flex;align-items:center;justify-content:center;text-decoration:none;
  box-shadow:0 4px 14px rgba(230,67,64,.35);z-index:99}
@media(min-width:700px){.wrap{padding:24px 16px 60px}}
</style>
"""


# 新闻编辑部栏目前缀（如「详讯丨」「独家|」），摘要里应剥掉
_BRIEF_NEWSROOM_PREFIX = re.compile(r"^[\u4e00-\u9fff]{1,4}[丨|]\s*")
# 已知人物主语：连续多条以同一人物开头时，后续不再重复其名字
_BRIEF_SUBJECTS = ("习近平", "普京", "特朗普", "李在明", "拜登")


def _clean_brief_title(title: str) -> str:
    """剥掉栏目前缀与空白，不做硬截断（避免切断词）。"""
    text = re.sub(r"\s+", " ", (title or "").strip())
    # 最多剥两层（如「详讯丨独家丨…」）
    for _ in range(2):
        text = _BRIEF_NEWSROOM_PREFIX.sub("", text).strip()
    return text


def build_today_brief(day: dict) -> str:
    """首页「今日一句话摘要」：取国内前两条 + 国际第一条，拼成焦点句。

    只取标题，不做 AI 总结，避免编造。
    - 剥掉「详讯丨」等栏目前缀，避免重复啰嗦；
    - 不在词中间硬切（坑 #7）：必要时只在标点边界截断；
    - 同一人物主语连续出现时，后续省略其名字。
    """
    domestic = day.get("sections", {}).get("domestic", [])
    international = day.get("sections", {}).get("international", [])
    parts: list[str] = []
    seen_subjects: set[str] = set()

    for item in domestic[:2]:
        title = _clean_brief_title(item.get("title", ""))
        if not title:
            continue
        # 主语去重：若与前面已出现的人物相同，剥掉开头的人名
        for subject in _BRIEF_SUBJECTS:
            if title.startswith(subject):
                if subject in seen_subjects:
                    title = title[len(subject):].lstrip("，, ")
                else:
                    seen_subjects.add(subject)
                break
        if title:
            parts.append(title)

    if international:
        first = international[0]
        title = _clean_brief_title(first.get("title_zh") or first.get("title_en", ""))
        if title:
            parts.append("国际上：" + title)

    result = "，".join(parts)
    # 仅在明显过长时于标点边界收尾，绝不切在词中间
    if len(result) > 100:
        window = result[:100]
        cut = max(window.rfind(m) for m in ("，", "。", "；", "、", "："))
        result = window[:cut + 1].rstrip("，。；、：") + "…" if cut > 40 else window[:97] + "…"
    return result


def truncate_title_for_summary(day: dict) -> str:
    """首页摘要：取国内外各第一条标题拼成一句话。"""
    titles = []
    for key in ("domestic", "international"):
        section = day.get("sections", {}).get(key, [])
        if section:
            first = section[0]
            title = first.get("title") if key == "domestic" else (first.get("title_zh") or first.get("title_en", ""))
            titles.append(str(title))
    return "｜".join(titles)[:60]


def cleanup_old(paths: Paths, now: datetime, logger: Callable = log):
    """清理 30 天前的归档/entries/运行报告，以及 7 天前的热榜快照。

    frequency 只回溯 7 天快照，更早的快照无用，必须清掉，否则每天 3 份无限累积。
    """
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    snapshot_cutoff = (now - timedelta(days=SNAPSHOT_KEEP_DAYS)).strftime("%Y-%m-%d")
    count = 0
    for archive in paths.archive.glob("*.html"):
        if archive.stem < cutoff:
            archive.unlink()
            count += 1
    for run in paths.runs.glob("*.json"):
        if run.stem[:10] < cutoff:
            run.unlink()
    snapshot_count = 0
    for snapshot in paths.snapshots.glob("*.json"):
        # 快照文件名格式 YYYY-MM-DD-HHMM.json，前 10 位是日期
        if snapshot.stem[:10] < snapshot_cutoff:
            snapshot.unlink()
            snapshot_count += 1

    entries = load_entries(paths.entries)
    before = len(entries["days"])
    entries["days"] = [d for d in entries["days"] if d.get("date", "") >= cutoff]
    entries["days"].sort(key=lambda d: d.get("date", ""), reverse=True)
    with open(paths.entries, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)
    if count or snapshot_count or before != len(entries["days"]):
        logger(f"已清理 {count} 个过期归档，{snapshot_count} 个过期快照，"
               f"{before - len(entries['days'])} 条过期数据")


def write_run_report(paths: Paths, now: datetime, report: dict):
    """运行报告按「日期-时分」命名，每天 3 次运行各自保留，不再互相覆盖。"""
    path = paths.runs / f"{now.strftime('%Y-%m-%d-%H%M')}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)


# ── 主流程 ──────────────────────────────────────────────────────────

def generate(dry_run: bool = False, ai_text_fn: Callable | None = None,
             fetch_rss_fn: Callable | None = None, fetch_hot_fn: Callable | None = None,
             tts_fn: Callable | None = None, site_url: str = ""):
    """执行 L0–L8。各依赖可注入，便于 dry-run 与冒烟测试。"""
    paths = Paths(ROOT, dry_run=dry_run)
    paths.ensure_dirs()
    now = beijing_now()
    date_str = now.strftime("%Y-%m-%d")
    rid = run_id(now)
    if dry_run:
        log(f"【dry-run】所有产物写入 {paths.base}（零网络、零额度）")
    log(f"开始生成：run_id={rid}，日期={date_str}")

    # L0
    prepare_audio(paths.audio_mp3, paths.audio_backup, log)
    load_entries(paths.entries)

    # L1
    log("L1 采集：抓取国际 RSS 与国内热榜")
    international_raw = fetch_all_feeds(fetch_fn=fetch_rss_fn) if fetch_rss_fn is None else fetch_rss_fn(INTL_FEEDS)
    hot_raw = (hot_client.fetch_all_hot(logger=log) if fetch_hot_fn is None
               else fetch_hot_fn(hot_client.HOT_PLATFORMS))

    # 空数据守卫
    if not international_raw and not hot_raw:
        log("警告：热榜与 RSS 全部为 0 条，不写任何文件，保留上一版页面")
        restore_audio(paths.audio_mp3, paths.audio_backup)
        return False

    # L2
    log("L2 快照与热度计算")
    domestic = score_hot_items(hot_raw, paths, now)
    # 国内热点质量筛选：过滤花絮/缩写，大事件加权，只取 8 条精品
    domestic = hot_client.refine_domestic(domestic, top_n=DOMESTIC_TOP_N)
    log(f"国内热点质量筛选后保留 {len(domestic)} 条（大事件优先）")

    # L3
    log("L3 翻译国际标题（每批 8 条）")
    international = translate_international(international_raw, ai_text=ai_text_fn)

    # L4
    log("L4 国际热点轻量聚类")
    international = cluster_international(international)[:INTL_TOP_N]

    # 空版块守卫（闸门前先兜底）
    if len(domestic) < MIN_SECTION_ITEMS or len(international) < MIN_SECTION_ITEMS:
        log("警告：某个版块条数不足，不写任何文件，保留上一版页面")
        restore_audio(paths.audio_mp3, paths.audio_backup)
        return False

    # L5
    log("L5 改写播报稿")
    broadcast = build_broadcast(domestic, international, now, ai_text=ai_text_fn)

    # L6
    log("L6 生成语音")
    tts_fn = tts_fn or generate_tts
    tts_ok = tts_fn(broadcast, paths.audio_mp3)
    log(f"语音生成{'成功' if tts_ok else '失败，页面将降级为浏览器朗读'}")

    # L7
    log("L7 发布闸门")
    ok, reason = passes_gate(domestic, international, tts_ok,
                             paths.audio_mp3 if tts_ok else None, log)
    if not ok:
        log(f"闸门未通过：{reason}；不写任何文件，保留上一版页面")
        restore_audio(paths.audio_mp3, paths.audio_backup)
        return False

    # L8
    log("L8 写出产物")
    day = {
        "date": date_str,
        "updated_at": now.astimezone(BEIJING_TZ).isoformat(),
        "broadcast": broadcast,
        "tts_ok": tts_ok,
        "sections": {"domestic": domestic, "international": international},
        "raw_news": (international_raw + hot_raw)[:MAX_RAW_NEWS],
    }
    entries = load_entries(paths.entries)
    entries["days"] = [d for d in entries["days"] if d.get("date") != date_str]
    entries["days"].insert(0, day)
    entries["days"].sort(key=lambda d: d.get("date", ""), reverse=True)
    with open(paths.entries, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)

    # 首页：注入最近 7 天摘要
    recent = []
    for other in entries["days"][:RECENT_DAYS + 1]:
        if other["date"] == date_str:
            continue
        recent.append({"date": other["date"], "summary": truncate_title_for_summary(other)})
    day_view = dict(day)
    day_view["recent_days"] = recent[:RECENT_DAYS]
    with open(paths.index, "w", encoding="utf-8") as fh:
        fh.write(render_day(day_view, is_today=True, site_url=site_url))

    # 归档页（不放播放器）
    archive_path = paths.archive / f"{date_str}.html"
    with open(archive_path, "w", encoding="utf-8") as fh:
        fh.write(render_day(day, is_today=False, site_url=site_url))

    cleanup_old(paths, now, log)
    write_run_report(paths, now, {
        "run_id": rid,
        "date": date_str,
        "rss_count": len(international_raw),
        "hot_count": len(hot_raw),
        "domestic_count": len(domestic),
        "international_count": len(international),
        "tts_ok": tts_ok,
        "gate": reason,
    })
    if paths.audio_backup.exists():
        paths.audio_backup.unlink()
    log(f"生成完成：首页 {paths.index}")
    return True


# ── dry-run 假数据 ──────────────────────────────────────────────────

def fake_ai_text(prompt: str) -> str:
    """假 AI：零网络、零额度；按输入里的编号返回译文或播报稿。"""
    if "翻译" in prompt:
        lines = [line for line in prompt.splitlines() if re.match(r"^\d+\.\s", line)]
        # 返回纯中文译文（不回显英文），模拟真实模型行为，保证闸门中文占比达标；
        # 编号取自输入，保证不同标题译文不同，避免假数据全部并成一簇
        zh_pool = ["国际峰会多国领导人举行会谈", "全球经济论坛发布最新展望",
                   "多国就气候议题展开磋商", "地区局势最新进展受到关注",
                   "国际贸易协定谈判取得进展", "多国科学家发布研究成果",
                   "联合国呼吁加强人道主义援助", "新一轮外交磋商即将举行"]
        output = []
        for line in lines:
            match = re.match(r"^(\d+)\.\s+(.+)$", line)
            if match:
                num = int(match.group(1))
                # 用英文原文里的媒体名派生纯中文唯一译文，避免跨媒体假聚类且保证中文占比
                en = match.group(2)
                m = re.search(r"reports global summit (\S+) number (\d+)", en)
                publisher = m.group(1) if m else "未知"
                zh_publisher = {"BBC": "英国广播公司", "TheGuardian": "卫报",
                                "AlJazeera": "半岛电视台", "France24": "法国二十四台",
                                "NPR": "美国公共广播", "SkyNews": "天空新闻",
                                "GoogleNews": "谷歌新闻"}.get(publisher, "海外媒体")
                serial = int(m.group(2)) if m else num
                output.append(f"[{num}] {zh_pool[(serial - 1) % len(zh_pool)]}{zh_publisher}报道")
        return "\n".join(output)
    return "各位好，今天是11月18日，欢迎收听今日热点。今日国内外发生多起重要事件。" + ai_client.END_SENTENCE


def fake_feed(url: str, timeout: int):
    """假 feedparser 结果，零网络。"""
    class Entry:
        def __init__(self, i):
            self.title = f"World news headline number {i} about economy and climate talks"
            self.link = f"https://example.com/news/{i}"
            self.summary = f"<p>Summary {i} <img src='https://example.com/img/{i}.jpg'></p>"
            self.published_parsed = (2026, 11, 18, 8, 0, 0, 0, 0, 0)
            self.media_thumbnail = []
            self.enclosures = []
            self.content = []
    class Result:
        bozo = 0
        entries = [Entry(i) for i in range(1, 6)]
    return Result()


def fake_fetch_feeds(feeds: list[dict]) -> list[dict]:
    """直接返回已标准化的假 RSS 条目。"""
    out = []
    for fi, feed in enumerate(feeds):
        for i in range(1, 4):
            out.append({
                "title_en": f"{feed['publisher']} reports global summit {feed['publisher'].replace(' ', '')} number {i}",
                "title_zh": "",
                "url": f"https://example.com/{fi}/{i}",
                "source": feed["publisher"],
                "publisher": feed["publisher"],
                "summary_en": "",
                "image": "",
                "published": "2026-11-18",
                "section": "international",
            })
    return out


def fake_fetch_hot(platforms: list[dict]) -> list[dict]:
    """直接返回假热榜条目（每平台 4 条，零网络）。

    标题刻意做成主题各异，避免假数据之间 bigram 相似度过高被全部并成一簇，
    这样才能验证真实的跨平台合并逻辑。
    """
    topics = [
        "国产大飞机新增航线正式投入运营",
        "多地推出住房补贴新政惠及首套房",
        "全国铁路暑运旅客发送量创新高",
        "新能源汽车下乡活动启动",
        "人工智能产业大会在沪开幕",
        "长江流域进入汛期防汛启动",
        "医保药品目录调整结果公布",
        "航天员完成空间站出舱任务",
        "高校毕业生就业帮扶行动开展",
    ]
    out = []
    for pi, platform in enumerate(platforms):
        for rank in range(1, 5):
            out.append({
                "id": f"{platform['name']}-{rank}",
                "title": topics[(pi * 4 + rank - 1) % len(topics)],
                "url": "https://www.baidu.com/s?wd=test" if platform["id"] == "baidu" else f"https://{platform['expected_domains'][0]}/hot/{rank}",
                "rank": rank,
                "rank_total": 4,
                "platform": platform["name"],
                "platform_id": platform["id"],
            })
    return out


def prepare_dryrun_history(paths: Paths):
    """构造 3 份假快照与假历史数据：今天、昨天、31 天前（验证清理与 frequency）。"""
    paths.ensure_dirs()
    now = beijing_now()
    # 假快照：今天 3 份，内容与本次假热榜一致，验证 frequency 能算出 >=3
    fake_hot = fake_fetch_hot(hot_client.HOT_PLATFORMS)
    past_items = [
        {"title": x["title"], "platform": x["platform"], "rank": x["rank"]}
        for x in fake_hot
    ]
    for hours in (0, 1, 2):
        moment = now - timedelta(hours=hours)
        payload = {"captured_at": moment.isoformat(), "items": past_items}
        with open(paths.snapshots / hot_client.snapshot_filename(moment), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)

    # 假历史 entries：昨天 + 31 天前
    old_date = (now - timedelta(days=31)).strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    entries = {"days": [
        {"date": yesterday, "updated_at": yesterday, "broadcast": "昨日播报",
         "tts_ok": False, "sections": {"domestic": [], "international": []}, "raw_news": []},
        {"date": old_date, "updated_at": old_date, "broadcast": "旧播报",
         "tts_ok": False, "sections": {"domestic": [], "international": []}, "raw_news": []},
    ]}
    with open(paths.entries, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False)
    # 31 天前的归档文件
    with open(paths.archive / f"{old_date}.html", "w", encoding="utf-8") as fh:
        fh.write("<html>旧归档</html>")
    log("dry-run：已准备 3 个假快照 + 昨天/31 天前的假历史数据")


def run_dry_run():
    """端到端 dry-run 入口：注入全部假数据，零网络、零额度。"""
    paths = Paths(ROOT, dry_run=True)
    prepare_dryrun_history(paths)
    ok = generate(
        dry_run=True,
        ai_text_fn=fake_ai_text,
        fetch_rss_fn=fake_fetch_feeds,
        fetch_hot_fn=fake_fetch_hot,
        tts_fn=generate_tts_fake,
    )
    if ok:
        with open(paths.entries, "r", encoding="utf-8") as fh:
            entries = json.load(fh)
        dates = [d["date"] for d in entries["days"]]
        log(f"dry-run 结果：entries 日期 = {dates}")
        assert all(d >= (beijing_now() - timedelta(days=30)).strftime("%Y-%m-%d") for d in dates), "30 天清理失败"
        assert not (paths.archive / ((beijing_now() - timedelta(days=31)).strftime("%Y-%m-%d") + ".html")).exists(), "旧归档未清理"

        today = entries["days"][0]
        domestic = today["sections"]["domestic"]
        international = today["sections"]["international"]
        assert domestic and international, "国内/国际版块不能为空"
        assert len(domestic) <= DOMESTIC_TOP_N, "国内热点未限量到 8 条精品"
        assert all(not hot_client.is_low_quality_title(x["title"]) for x in domestic), "低质量标题未过滤"
        assert max(x["frequency"] for x in domestic) >= 3, "frequency 计算失败（应至少出现 3 次）"
        assert any(not x["is_new"] for x in domestic), "持续热点未标注为新增=false"
        assert paths.index.exists() and paths.audio_mp3.exists(), "首页或音频未生成"
        html_text = paths.index.read_text(encoding="utf-8")
        assert "查看各家报道 ▸" in html_text and "观点可能不同" in html_text, "折叠观点区缺失"
        assert "<audio" in html_text, "播放器缺失"
        log("dry-run 自检通过：30 天清理、frequency≥3、双版块、折叠观点区、播放器均正常")
    return ok


def main():
    parser = argparse.ArgumentParser(description="每日热点简报生成器")
    parser.add_argument("--dry-run", action="store_true",
                        help="使用假数据端到端演练，零网络、零 Cloudflare 额度、不碰真实文件")
    args = parser.parse_args()
    if args.dry_run:
        success = run_dry_run()
    else:
        success = generate()
    sys.exit(0 if success else 0)  # 闸门失败也 exit(0)，保留上一版页面不动


if __name__ == "__main__":
    main()
