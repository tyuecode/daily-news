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
TTS_RETRIES = 8                 # edge-tts 单个音色重试次数（服务端随机抖动，3 次不够）
TTS_RATE = "-5%"                # 新闻播报语速略慢，提升清晰度

# 可选音色（edge-tts 免费，无需额度）。每个音色独立生成 MP3，读者可自行切换。
# fallback：该音色连续失败过半后临时切换的备用音色。
TTS_VOICES = [
    {"id": "xiaoxiao", "name": "晓晓", "desc": "女声 · 亲和自然",
     "voice": "zh-CN-XiaoxiaoNeural", "fallback": "zh-CN-XiaoyiNeural"},
    {"id": "yunyang", "name": "云扬", "desc": "男声 · 新闻主播",
     "voice": "zh-CN-YunyangNeural", "fallback": "zh-CN-YunjianNeural"},
    {"id": "yunxi", "name": "云希", "desc": "男声 · 年轻清晰",
     "voice": "zh-CN-YunxiNeural", "fallback": "zh-CN-XiaoxiaoNeural"},
]
DEFAULT_VOICE_ID = "xiaoxiao"
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
        self.robots = base / "robots.txt"
        self.entries = self.data / "entries.json"
        self.audio_mp3 = self.audio / "daily.mp3"  # 兼容旧文件名（主音色）
        self.audio_backup = self.audio / "daily.mp3.bak"

    def voice_mp3(self, voice_id: str) -> Path:
        """某个音色的音频文件路径：audio/voice-{id}.mp3。"""
        return self.audio / f"voice-{voice_id}.mp3"

    def voice_backup(self, voice_id: str) -> Path:
        return self.audio / f"voice-{voice_id}.mp3.bak"

    def ensure_dirs(self):
        for path in (self.data, self.snapshots, self.runs, self.archive, self.audio):
            path.mkdir(parents=True, exist_ok=True)


def log(message: str):
    print(f"[每日热点] {message}", flush=True)


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


# ── L0 准备 ─────────────────────────────────────────────────────────

def prepare_audio(paths, logger: Callable = log):
    """先把所有音色的旧 mp3 挪走（不是删），闸门不通过时还原，避免旧音频配新稿。"""
    count = 0
    for voice in TTS_VOICES:
        mp3 = paths.voice_mp3(voice["id"])
        backup = paths.voice_backup(voice["id"])
        if mp3.exists():
            if backup.exists():
                backup.unlink()
            mp3.rename(backup)
            count += 1
    logger(f"已把 {count} 个旧音频挪到备份位置")


def restore_audio(paths, logger: Callable = log):
    """闸门失败时还原所有音色的旧音频。"""
    for voice in TTS_VOICES:
        mp3 = paths.voice_mp3(voice["id"])
        backup = paths.voice_backup(voice["id"])
        if backup.exists():
            if mp3.exists():
                mp3.unlink()
            backup.rename(mp3)
    # 兼容旧文件 daily.mp3
    legacy_backup = paths.audio_backup
    if legacy_backup.exists():
        if paths.audio_mp3.exists():
            paths.audio_mp3.unlink()
        legacy_backup.rename(paths.audio_mp3)


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

def _clean_broadcast_point(title: str) -> str:
    """播报要点清理：剥掉栏目前缀，去首尾空白（不硬切，避免切断词）。"""
    text = re.sub(r"\s+", " ", (title or "").strip())
    for _ in range(2):
        text = re.sub(r"^[\u4e00-\u9fff]{1,4}[丨|]\s*", "", text).strip()
    return text


def build_broadcast(domestic: list[dict], international: list[dict],
                    now: datetime, ai_text: Callable | None = None) -> str:
    """用国内外排名靠前的条目改写播报稿（要点二次过滤 + 前缀清理）。"""
    points = []
    for item in domestic[:6]:
        title = _clean_broadcast_point(item.get("title", ""))
        # 二次质量过滤：花絮/到场/盘点类不进口播
        if title and not hot_client.is_low_quality_title(title):
            points.append(f"国内：{title}")
    for item in international[:6]:
        title = _clean_broadcast_point(item.get("title_zh") or item.get("title_en", ""))
        if title:
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


def normalize_tts_text(text: str) -> str:
    """播报稿转语音前的文本归一化，避免机器朗读卡顿或怪异停顿。

    - 段落空行 → 单个句号停顿（中文朗读不需要长静音）；
    - 连续换行/空白 → 单个空格；
    - 去掉行首行尾空白、折叠重复标点。
    """
    if not text:
        return ""
    text = text.replace("\r", "")
    # 段落分隔：把「。\n\n」或「\n\n」归一为一个句号 + 空格
    text = re.sub(r"[。]?\s*\n\s*\n\s*", "。", text)
    # 剩余单换行 → 空格
    text = re.sub(r"\s*\n\s*", " ", text)
    # 折叠空白与重复标点
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"_{2,}|={2,}|-{2,}", " ", text)
    text = re.sub(r"。{2,}", "。", text)
    return text.strip()


def generate_tts(text: str, target: Path, voice_config: dict | None = None,
                 retries: int = TTS_RETRIES) -> bool:
    """edge-tts 7.x 生成语音：文本归一化 + 语速控制 + 备用音色 + 线性退避。

    voice_config：TTS_VOICES 中的一项；省略时用默认音色。
    主音色连续失败过半（第 5 次起）切换到该音色的备用音色，提高成功率。
    成功写文件并返回 True。
    """
    import edge_tts

    voice_config = voice_config or TTS_VOICES[0]
    clean_text = normalize_tts_text(text)
    primary = voice_config["voice"]
    fallback = voice_config.get("fallback") or TTS_VOICES[0]["voice"]

    async def _save(voice: str):
        communicate = edge_tts.Communicate(clean_text, voice=voice, rate=TTS_RATE)
        await communicate.save(str(target))

    for attempt in range(retries):
        voice = primary if attempt < retries // 2 else fallback
        try:
            asyncio.run(_save(voice))
            if is_valid_mp3(target):
                log(f"TTS 成功（{voice_config['name']}，第 {attempt + 1} 次，音色 {voice}）")
                return True
            log(f"TTS 第 {attempt + 1} 次返回非音频内容，重试")
        except Exception as exc:
            log(f"TTS 第 {attempt + 1} 次失败（{voice}）：{type(exc).__name__}")
        time.sleep(attempt + 1)  # 线性退避 1s, 2s, 3s ...
    return False


def generate_tts_fake(text: str, target: Path, voice_config: dict | None = None,
                      retries: int = TTS_RETRIES) -> bool:
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

    brief_text = build_today_brief(day)
    description = brief_text[:78] or f"{date} 国内外热点简报"

    parts = []
    parts.append("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    parts.append(f"<title>每日热点简报 · {html.escape(date)}</title>")
    parts.append(f"<meta name=\"description\" content=\"{html.escape(description, quote=True)}\">")
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
    voices = day.get("voices") or [
        {"id": v["id"], "name": v["name"], "desc": v["desc"], "ok": True}
        for v in TTS_VOICES
    ]
    # 只有生成成功的音色才可切换
    available_voices = [v for v in voices if v.get("ok", True)]
    if is_today and day.get("tts_ok") and broadcast:
        parts.append(_render_audio_player(broadcast, mode="mp3", voices=available_voices))
    elif is_today and broadcast:
        parts.append(_render_audio_player(broadcast, mode="tts", voices=available_voices))
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
        # 最近几天归档导航（P4）：归档页之间也能互相跳转
        recent = day.get("recent_days", [])
        if recent:
            parts.append("<section class=\"recent\"><h2>其他日期</h2><ul class=\"timeline\">")
            for item in recent:
                parts.append(
                    "<li><div class=\"day-line\">"
                    f"<span class=\"dot\"></span><a href=\"{html.escape(item['date'])}.html\">"
                    f"{html.escape(item['date'])}</a></div>"
                    f"<div class=\"day-sum\">{html.escape(item.get('summary', ''))}</div></li>"
                )
            parts.append("</ul></section>")

    # 悬浮回到顶部（放在 div 外，避免被 wrap 的层级裁剪）
    parts.append("</div><a class=\"to-top\" href=\"#\" aria-label=\"回到顶部\">↑</a>")

    # 播报脚本（放在末尾，JSON 中把 < > & 转成 Unicode 转义，杜绝任何标签注入）
    parts.append(_build_audio_script(broadcast))
    parts.append("</body></html>")
    return "\n".join(parts)


def _render_audio_player(broadcast: str, mode: str = "mp3",
                         voices: list[dict] | None = None) -> str:
    """完善的播报播放器：音色选择 + 播放/暂停/停止 + 进度条 + 时间显示 + 语速。

    mode='mp3'：切换音色即换 audio 源（audio/voice-{id}.mp3）；
    mode='tts'：TTS 全部失败时用浏览器 SpeechSynthesis，音色下拉列出可用中文语音。
    """
    safe_text = html.escape(broadcast, quote=True)
    voices = voices or [{"id": v["id"], "name": v["name"], "desc": v["desc"], "ok": True}
                        for v in TTS_VOICES]
    label = "播放今日播报" if mode == "mp3" else "朗读今日播报（语音合成）"

    # 音色选择器：MP3 模式用音色按钮；TTS 降级用浏览器语音下拉
    if mode == "mp3":
        voice_buttons = "".join(
            f'<button type="button" class="voice-btn'
            f'{" active" if v["id"] == DEFAULT_VOICE_ID else ""}"'
            f' data-voice="{html.escape(v["id"])}"'
            f' data-src="audio/voice-{html.escape(v["id"])}.mp3"'
            f' title="{html.escape(v.get("desc", ""))}">{html.escape(v["name"])}</button>'
            for v in voices
        )
        source_attr = f"audio/voice-{DEFAULT_VOICE_ID}.mp3"
        voice_ui = f'<div class="voice-row"><span>音色</span>{voice_buttons}</div>'
    else:
        voice_ui = '<div class="voice-row"><span>音色</span>' \
                   '<select id="browserVoice" class="voice-select" aria-label="选择朗读音色"></select></div>'
        source_attr = ""

    return (
        f'<div class="player" id="player" data-mode="{mode}" data-text="{safe_text}">'
        f'<audio id="audioEl" preload="metadata" src="{source_attr}"></audio>'
        '<div class="player-row">'
        '<button type="button" class="play-btn" id="playBtn" aria-label="播放/暂停">▶</button>'
        '<div class="player-info">'
        f'<div class="player-title">{label}</div>'
        '<div class="player-status" id="playerStatus">点按即可收听，约 2 分钟</div>'
        '</div>'
        '<button type="button" class="stop-btn" id="stopBtn" aria-label="停止">■</button>'
        '</div>'
        '<div class="seek-row">'
        '<span class="time" id="curTime">0:00</span>'
        '<input type="range" class="seek" id="seekBar" min="0" max="1000" value="0" aria-label="播放进度">'
        '<span class="time" id="durTime">0:00</span>'
        '</div>'
        '<div class="rate-row"><span>语速</span>'
        '<button type="button" class="rate-btn" data-rate="0.85">0.85x</button>'
        '<button type="button" class="rate-btn active" data-rate="1">1x</button>'
        '<button type="button" class="rate-btn" data-rate="1.25">1.25x</button>'
        '<button type="button" class="rate-btn" data-rate="1.5">1.5x</button>'
        '</div>'
        f'{voice_ui}'
        '</div>'
    )


def _build_audio_script(broadcast: str) -> str:
    """生成播放器交互脚本：MP3 多音色切换 + 进度/时间 + 语速 + 浏览器朗读音色选择。

    所有外部文本已 Unicode 转义，防止标签注入。
    """
    safe_json = (json.dumps(broadcast, ensure_ascii=False)
                 .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    return (
        "<script>"
        "(function(){"
        "var player=document.getElementById('player');if(!player)return;"
        "var mode=player.getAttribute('data-mode');"
        "var audio=document.getElementById('audioEl');"
        "var playBtn=document.getElementById('playBtn');"
        "var stopBtn=document.getElementById('stopBtn');"
        "var statusEl=document.getElementById('playerStatus');"
        "var seekBar=document.getElementById('seekBar');"
        "var curTime=document.getElementById('curTime');"
        "var durTime=document.getElementById('durTime');"
        "var rateBtns=document.querySelectorAll('.rate-btn');"
        "var voiceBtns=document.querySelectorAll('.voice-btn');"
        "var voiceSelect=document.getElementById('browserVoice');"
        "var text=" + safe_json + ";"
        "var rate=1,playing=false,utterance=null,startedAt=0,elapsed=0,seeking=false;"
        "function fmt(t){t=Math.max(0,Math.floor(t||0));return Math.floor(t/60)+':'+('0'+(t%60)).slice(-2);}"
        "function setStatus(s){statusEl.textContent=s;}"
        "function applyRate(){if(mode==='mp3'){audio.playbackRate=rate;}"
        "else if(utterance){utterance.rate=rate;}}"
        "function zhVoices(){var vs=(window.speechSynthesis&&speechSynthesis.getVoices())||[];"
        "return vs.filter(function(v){return v.lang&&v.lang.toLowerCase().indexOf('zh')===0;});}"
        "function fillVoiceSelect(){if(!voiceSelect)return;var z=zhVoices();"
        "if(!z.length){voiceSelect.innerHTML='<option>浏览器默认中文</option>';return;}"
        "voiceSelect.innerHTML=z.map(function(v,i){return '<option value=\"'+i+'\">'+v.name+'</option>';}).join('');}"
        "if('speechSynthesis' in window){fillVoiceSelect();speechSynthesis.onvoiceschanged=fillVoiceSelect;}"
        "function ttsElapsed(){return elapsed+(startedAt?((Date.now()-startedAt)/1000):0);}"
        "function ttsDuration(){return Math.max(1,text.length/4.5);}"
        "function updateProgress(cur,dur){"
        "if(!isFinite(dur)||dur<=0){dur=ttsDuration();}"
        "if(!seeking)seekBar.value=Math.round(cur/dur*1000);"
        "curTime.textContent=fmt(cur);durTime.textContent=fmt(dur);}"
        "function start(){"
        "if(mode==='mp3'){applyRate();var p=audio.play();"
        "if(p&&p.catch){p.catch(function(){setStatus('播放失败，可点“展开播报稿”阅读');});}"
        "playing=true;playBtn.textContent='❚❚';setStatus('正在播放…');"
        "}else{"
        "if(!('speechSynthesis' in window)){setStatus('当前浏览器不支持朗读，请阅读播报稿');return;}"
        "if(speechSynthesis.paused){speechSynthesis.resume();playing=true;playBtn.textContent='❚❚';"
        "startedAt=Date.now();setStatus('继续朗读…');return;}"
        "speechSynthesis.cancel();utterance=new SpeechSynthesisUtterance(text);"
        "utterance.lang='zh-CN';utterance.rate=rate;"
        "var z=zhVoices();if(voiceSelect&&z[parseInt(voiceSelect.value,10)])utterance.voice=z[parseInt(voiceSelect.value,10)];"
        "else if(z[0])utterance.voice=z[0];"
        "utterance.onend=function(){playing=false;playBtn.textContent='▶';setStatus('朗读结束');"
        "elapsed=ttsDuration();startedAt=0;updateProgress(ttsDuration(),ttsDuration());};"
        "utterance.onerror=function(){playing=false;playBtn.textContent='▶';setStatus('朗读失败，请阅读播报稿');};"
        "speechSynthesis.speak(utterance);playing=true;playBtn.textContent='❚❚';"
        "startedAt=Date.now();setStatus('正在朗读…');"
        "}}"
        "function pause(){"
        "if(mode==='mp3'){audio.pause();}else if('speechSynthesis' in window){"
        "elapsed=ttsElapsed();startedAt=0;speechSynthesis.pause();}"
        "playing=false;playBtn.textContent='▶';setStatus('已暂停');"
        "}"
        "function stop(){"
        "if(mode==='mp3'){audio.pause();audio.currentTime=0;}else if('speechSynthesis' in window){speechSynthesis.cancel();}"
        "playing=false;startedAt=0;elapsed=0;playBtn.textContent='▶';"
        "seekBar.value=0;curTime.textContent='0:00';setStatus('已停止');"
        "}"
        "playBtn.addEventListener('click',function(){if(playing)pause();else start();});"
        "stopBtn.addEventListener('click',stop);"
        "audio.addEventListener('timeupdate',function(){updateProgress(audio.currentTime,audio.duration);});"
        "audio.addEventListener('loadedmetadata',function(){updateProgress(0,audio.duration);});"
        "audio.addEventListener('ended',function(){playing=false;playBtn.textContent='▶';setStatus('播放结束');});"
        "seekBar.addEventListener('input',function(){seeking=true;"
        "var dur=(mode==='mp3')?audio.duration:ttsDuration();"
        "var t=dur*(parseInt(seekBar.value,10)/1000);updateProgress(t,dur);});"
        "seekBar.addEventListener('change',function(){"
        "var dur=(mode==='mp3')?audio.duration:ttsDuration();"
        "var t=dur*(parseInt(seekBar.value,10)/1000);"
        "if(mode==='mp3'){audio.currentTime=t;}else{elapsed=t;startedAt=playing?Date.now():0;}"
        "seeking=false;});"
        "rateBtns.forEach(function(b){b.addEventListener('click',function(){"
        "rate=parseFloat(b.getAttribute('data-rate'));"
        "rateBtns.forEach(function(x){x.classList.remove('active');});b.classList.add('active');"
        "applyRate();setStatus('语速 '+rate+'x');"
        "});});"
        "voiceBtns.forEach(function(b){b.addEventListener('click',function(){"
        "voiceBtns.forEach(function(x){x.classList.remove('active');});b.classList.add('active');"
        "var wasPlaying=playing;audio.pause();audio.src=b.getAttribute('data-src');audio.load();"
        "playing=false;playBtn.textContent='▶';"
        "setStatus('已切换音色：'+b.textContent+(wasPlaying?'，点播放收听':''));"
        "});});"
        "if(mode==='tts'){setInterval(function(){"
        "if(playing&&!seeking)updateProgress(ttsElapsed(),ttsDuration());},1000);}"
        "})();"
        "</script>"
    )


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
.broadcast #audioEl{display:none}
/* 统一播报控制条 */
.player{background:var(--accent-soft);border:1px solid var(--accent);border-radius:12px;
  padding:12px;margin-bottom:10px}
.player-row{display:flex;align-items:center;gap:12px}
.play-btn{flex:0 0 52px;width:52px;height:52px;border-radius:50%;background:var(--accent);
  color:#fff;border:0;font-size:20px;cursor:pointer;display:flex;align-items:center;
  justify-content:center;box-shadow:0 3px 10px rgba(230,67,64,.3)}
.play-btn:active{transform:scale(.94)}
.player-info{flex:1;min-width:0}
.player-title{font-size:15px;font-weight:600}
.player-status{font-size:12px;color:var(--muted);margin-top:2px}
.stop-btn{flex:0 0 44px;width:44px;height:44px;border-radius:10px;background:var(--card);
  color:var(--muted);border:1px solid var(--line);font-size:14px;cursor:pointer}
.rate-row{display:flex;align-items:center;gap:6px;margin-top:10px;flex-wrap:wrap}
.rate-row span{font-size:12px;color:var(--muted);margin-right:2px}
.rate-btn{background:var(--card);color:var(--muted);border:1px solid var(--line);
  border-radius:16px;padding:5px 11px;font-size:12px;cursor:pointer;min-height:30px}
.rate-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
/* 进度条与时间 */
.seek-row{display:flex;align-items:center;gap:8px;margin-top:10px}
.time{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;flex:0 0 34px}
.time:last-child{text-align:right}
.seek{flex:1;accent-color:var(--accent);height:24px;cursor:pointer}
/* 音色选择 */
.voice-row{display:flex;align-items:center;gap:6px;margin-top:10px;flex-wrap:wrap}
.voice-row>span{font-size:12px;color:var(--muted);margin-right:2px}
.voice-btn{background:var(--card);color:var(--muted);border:1px solid var(--line);
  border-radius:16px;padding:5px 12px;font-size:12px;cursor:pointer;min-height:30px}
.voice-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.voice-select{flex:1;min-width:120px;background:var(--card);color:var(--text);
  border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:13px;min-height:32px}
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
/* 国际卡片来源更醒目：让读者明确知道观点来自哪家媒体 */
.card:has(.en) .source{color:var(--accent);font-weight:600;font-size:13px}
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
                    # 省略主语后补「并」连接，避免「，迎接特朗普」这类突兀短语
                    remainder = title[len(subject):].lstrip("，, ")
                    title = ("并" + remainder) if remainder else ""
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
    prepare_audio(paths, log)
    load_entries(paths.entries)

    # L1
    log("L1 采集：抓取国际 RSS 与国内热榜")
    international_raw = fetch_all_feeds(fetch_fn=fetch_rss_fn) if fetch_rss_fn is None else fetch_rss_fn(INTL_FEEDS)
    hot_raw = (hot_client.fetch_all_hot(logger=log) if fetch_hot_fn is None
               else fetch_hot_fn(hot_client.HOT_PLATFORMS))

    # 空数据守卫
    if not international_raw and not hot_raw:
        log("警告：热榜与 RSS 全部为 0 条，不写任何文件，保留上一版页面")
        restore_audio(paths, log)
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
        restore_audio(paths, log)
        return False

    # L5
    log("L5 改写播报稿")
    broadcast = build_broadcast(domestic, international, now, ai_text=ai_text_fn)

    # L6：为每种音色生成一份音频
    log(f"L6 生成语音（{len(TTS_VOICES)} 种音色）")
    tts_fn = tts_fn or generate_tts
    voice_status: dict[str, bool] = {}
    for voice in TTS_VOICES:
        voice_status[voice["id"]] = tts_fn(broadcast, paths.voice_mp3(voice["id"]), voice)
    # 至少一种音色成功即可启用 MP3 播放器
    tts_ok = any(voice_status.values())
    log(f"语音生成完成：{voice_status}；"
        f"{'播放器可用' if tts_ok else '全部失败，页面降级为浏览器朗读'}")

    # L7
    log("L7 发布闸门")
    # 闸门校验默认音色的音频头；若默认音色失败但其他音色成功，改用成功的那个
    primary_ok = voice_status.get(DEFAULT_VOICE_ID, False)
    gate_voice_id = DEFAULT_VOICE_ID if primary_ok else next(
        (k for k, v in voice_status.items() if v), None)
    ok, reason = passes_gate(domestic, international, tts_ok,
                             paths.voice_mp3(gate_voice_id) if gate_voice_id else None, log)
    if not ok:
        log(f"闸门未通过：{reason}；不写任何文件，保留上一版页面")
        restore_audio(paths, log)
        return False

    # L8
    log("L8 写出产物")
    day = {
        "date": date_str,
        "updated_at": now.astimezone(BEIJING_TZ).isoformat(),
        "broadcast": broadcast,
        "tts_ok": tts_ok,
        "voices": [{"id": v["id"], "name": v["name"], "desc": v["desc"],
                    "ok": voice_status.get(v["id"], False)} for v in TTS_VOICES],
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

    # robots.txt：允许搜索引擎收录（利于国内用户搜索到本站）
    with open(paths.robots, "w", encoding="utf-8") as fh:
        fh.write("User-agent: *\nAllow: /\n")

    # 归档页（不放播放器，但带最近几天导航）
    archive_view = dict(day)
    archive_view["recent_days"] = recent[:RECENT_DAYS]
    archive_path = paths.archive / f"{date_str}.html"
    with open(archive_path, "w", encoding="utf-8") as fh:
        fh.write(render_day(archive_view, is_today=False, site_url=site_url))

    cleanup_old(paths, now, log)
    write_run_report(paths, now, {
        "run_id": rid,
        "date": date_str,
        "rss_count": len(international_raw),
        "hot_count": len(hot_raw),
        "domestic_count": len(domestic),
        "international_count": len(international),
        "tts_ok": tts_ok,
        "voice_status": voice_status,
        "gate": reason,
    })
    # 清理所有音色的备份文件
    for voice in TTS_VOICES:
        backup = paths.voice_backup(voice["id"])
        if backup.exists():
            backup.unlink()
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
        assert paths.index.exists(), "首页未生成"
        assert all(paths.voice_mp3(v["id"]).exists() for v in TTS_VOICES), "多音色音频未全部生成"
        assert len(today.get("voices", [])) == len(TTS_VOICES), "数据契约缺少音色状态"
        html_text = paths.index.read_text(encoding="utf-8")
        assert "查看各家报道 ▸" in html_text and "观点可能不同" in html_text, "折叠观点区缺失"
        assert 'class="player"' in html_text, "播放器缺失"
        assert 'data-voice=' in html_text, "音色切换按钮缺失"
        assert 'id="seekBar"' in html_text, "进度条缺失"
        log("dry-run 自检通过：30 天清理、frequency≥3、多音色音频、音色切换、进度条均正常")
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
