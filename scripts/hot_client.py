"""热榜客户端：封装 newsnow 统一热榜 API，并提供热度计算所需的工具函数。

设计要点：
- 一个 API 拿到 9 个中文平台的真实热搜榜，数组下标即排名（第 0 项 = 第 1 名）。
- 链接安全校验：只接受 https 且域名与平台预期匹配的链接，防止链接劫持。
- 三维度热度：rank（最好名次）+ frequency（跨快照出现次数）+ hotness（跨平台广度）。
- 中文 bigram Jaccard 用于跨平台话题合并（阈值 0.35）。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# ── newsnow 统一 API ────────────────────────────────────────────────
NEWSNOW_API = "https://newsnow.busiyi.world/api/s"

# 中文热榜平台配置（按优先级）。
# expected_domains：链接校验用的主域名；子域名（如 www. / s. / news.）自动放行。
# 注：tieba（吧友手写观后感）与 zhihu（问答娱乐化）不作为主源，故不列入。
HOT_PLATFORMS = [
    {"id": "baidu", "name": "百度热搜", "expected_domains": ["baidu.com"]},
    {"id": "toutiao", "name": "今日头条", "expected_domains": ["toutiao.com"]},
    {"id": "weibo", "name": "微博热搜", "expected_domains": ["weibo.com"]},
    {"id": "thepaper", "name": "澎湃新闻", "expected_domains": ["thepaper.cn"]},
    {"id": "douyin", "name": "抖音热榜", "expected_domains": ["douyin.com"]},
    {"id": "wallstreetcn-hot", "name": "华尔街见闻", "expected_domains": ["wallstreetcn.com"]},
    {"id": "cls-hot", "name": "财联社", "expected_domains": ["cls.cn"]},
    {"id": "ifeng", "name": "凤凰网", "expected_domains": ["ifeng.com"]},
    {"id": "bilibili-hot-search", "name": "B站热搜", "expected_domains": ["bilibili.com"]},
]

# ── 热度算法参数（默认权重，三者和为 1.0）────────────────────────────
DEFAULT_WEIGHTS = {"rank": 0.6, "frequency": 0.3, "hotness": 0.1}
BIGRAM_THRESHOLD = 0.35          # 中文 bigram Jaccard 判定同一话题的阈值
FREQUENCY_WINDOW = 21            # 归一化分母：7 天 × 每天 3 次抓取
HOTNESS_FULL_COVERAGE = 5        # 出现在 5 个平台即视为满覆盖
SNAPSHOT_LOOKBACK_DAYS = 7       # 计算 frequency 时回溯的快照天数
BEIJING_TZ = timezone(timedelta(hours=8))


# ── 链接安全 ────────────────────────────────────────────────────────

def validate_url(url: str, expected_domains: list[str]) -> bool:
    """只接受 https 协议，且主机名等于或隶属于任一预期域名。

    例：expected=['baidu.com'] 时，www.baidu.com 与 baidu.com 均通过，
    evil-baidu.com 与 http://baidu.com 均拒绝。
    """
    from urllib.parse import urlparse

    if not isinstance(url, str) or not url.lower().startswith("https://"):
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for domain in expected_domains:
        domain = domain.lower().lstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


# ── 文本相似度与词边界 ──────────────────────────────────────────────

def bigrams(text: str) -> set[str]:
    """取连续两字 bigram；单字文本返回一个单元素集合。"""
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return set()
    if len(text) == 1:
        return {text}
    return {text[i:i + 2] for i in range(len(text) - 1)}


def bigram_jaccard(a: str, b: str) -> float:
    """中文 bigram Jaccard 相似度；两集合都为空时返回 0。"""
    ga, gb = bigrams(a), bigrams(b)
    if not ga and not gb:
        return 0.0
    union = ga | gb
    return len(ga & gb) / len(union) if union else 0.0


def contains_keyword(text: str, keyword: str, case_sensitive: bool = False) -> bool:
    """词边界关键词匹配。

    - ASCII 关键词：左右不能紧邻字母或数字（Depardieu 不会被 eu 命中，
      software 不会被 war 命中）。
    - 中文关键词：直接子串匹配（中文没有 \\b 词边界）。
    - US / EU / UK 等缩写应传 case_sensitive=True，避免 "tell us" 误命中美国。
    """
    if not text or not keyword:
        return False
    flags = 0 if case_sensitive else re.IGNORECASE
    if re.search(r"[A-Za-z0-9]", keyword):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])"
        return bool(re.search(pattern, text, flags))
    return keyword in text


def is_china_related(title: str) -> bool:
    """涉华判定：只看标题，不看摘要（摘要里顺带提“中美”会误判）。

    中文命中「中国 / 中方 / 北京 / 习近平 / 中共」等；英文用词边界区分大小写，
    避免 "tell us" 误命中 US。
    """
    zh_keywords = ["中国", "中方", "北京", "习近平", "中共", "中华人民共和国", "外交部"]
    if any(k in title for k in zh_keywords):
        return True
    en_terms = ["China", "Chinese", "Beijing", "Xi Jinping", "US-China", "Sino-"]
    if any(contains_keyword(title, k, case_sensitive=True) for k in en_terms):
        return True
    return False


# ── 热度公式 ────────────────────────────────────────────────────────

def normalize_rank(best_rank: int, list_length: int) -> float:
    """第 1 名 = 1.0，第 N 名 = 1 - (N-1)/榜单长度；越界时夹到 [0, 1]。"""
    if list_length <= 0:
        return 0.0
    value = 1.0 - (max(1, best_rank) - 1) / list_length
    return max(0.0, min(1.0, value))


def normalize_frequency(frequency: int) -> float:
    """出现次数归一化，FREQUENCY_WINDOW 次及以上视为满值 1.0。"""
    return max(0.0, min(1.0, frequency / FREQUENCY_WINDOW))


def normalize_hotness(platform_count: int) -> float:
    """跨平台数归一化，HOTNESS_FULL_COVERAGE 个平台及以上视为满值 1.0。"""
    return max(0.0, min(1.0, platform_count / HOTNESS_FULL_COVERAGE))


def compute_score(norm_rank: float, norm_frequency: float, norm_hotness: float,
                  weights: dict | None = None) -> float:
    """三维度加权求和。传入的必须是已归一化的 [0, 1] 数值。"""
    w = weights or DEFAULT_WEIGHTS
    return (w["rank"] * norm_rank
            + w["frequency"] * norm_frequency
            + w["hotness"] * norm_hotness)


def score_item(best_rank: int, list_length: int, frequency: int, platform_count: int,
               weights: dict | None = None) -> float:
    """从原始维度一步算出最终分数（供主流程调用）。"""
    return compute_score(
        normalize_rank(best_rank, list_length),
        normalize_frequency(frequency),
        normalize_hotness(platform_count),
        weights,
    )


# ── newsnow API 抓取 ────────────────────────────────────────────────

def parse_newsnow_response(payload: dict, platform: dict) -> list[dict]:
    """把 newsnow 响应解析为标准化条目列表。

    payload 形如 {"status": "success"|"cache", "items": [{title, url, ...}], ...}；
    数据在顶层 items 键（实测确认，不是 data）。数组下标即排名。
    ⚠️ 实测：newsnow 对缓存命中会返回 status="cache"，必须同样接受，
    否则大部分平台会被误判为失败（曾因此只剩 2/9 个平台）。
    链接不合法或标题为空的条目丢弃。
    """
    if not isinstance(payload, dict):
        return []
    # 接受 success / cache；也兼容未来新增的同类成功状态（只要有 items 列表）。
    # 明确拒绝失败状态，避免把错误响应里的空 items 当作正常数据。
    status = str(payload.get("status", "")).lower()
    if status in ("fail", "error", "failed"):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    result = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not title or not validate_url(url, platform["expected_domains"]):
            continue
        result.append({
            "id": f"{platform['name']}-{index + 1}",
            "title": title,
            "url": url,
            "rank": index + 1,
            "platform": platform["name"],
            "platform_id": platform["id"],
            "rank_total": len(items),
        })
    return result


def fetch_platform(platform: dict, timeout: int = 12, opener=None,
                   logger=None) -> list[dict]:
    """抓取单个平台；失败返回空列表并记录警告，绝不抛给上层中断全流程。"""
    url = f"{NEWSNOW_API}?id={platform['id']}&latest"
    opener = opener or urllib.request.urlopen
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return parse_newsnow_response(payload, platform)
    except Exception as exc:  # 单源失败不影响其他平台
        if logger:
            logger(f"警告：热榜平台「{platform['name']}」抓取失败：{exc}")
        return []


def fetch_all_hot(platforms: list[dict] | None = None, max_workers: int = 4,
                  timeout: int = 12, opener=None, logger=None) -> list[dict]:
    """并发抓取全部热榜平台（默认限 4 路），返回展平后的条目列表。"""
    platforms = platforms or HOT_PLATFORMS
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_platform, p, timeout, opener, logger): p
            for p in platforms
        }
        for future in as_completed(futures):
            results.extend(future.result())
    return results


# ── 快照与 frequency ────────────────────────────────────────────────

def snapshot_filename(moment: datetime) -> str:
    """快照文件名：YYYY-MM-DD-HHMM.json（北京时间）。"""
    return moment.astimezone(BEIJING_TZ).strftime("%Y-%m-%d-%H%M.json")


def count_frequency(items: list[dict], snapshots: list[dict],
                    threshold: float = BIGRAM_THRESHOLD) -> list[int]:
    """对每条当前热搜，统计它在最近 N 次快照中出现过几次（含本次）。

    snapshots 中每个元素是 {"items": [{"title": "...", ...}, ...]}；
    用中文 bigram Jaccard >= threshold 判定同一话题（同一快照内只计一次）。
    """
    frequencies = []
    for item in items:
        count = 0
        for snapshot in snapshots:
            titles = [str(x.get("title", "")) for x in snapshot.get("items", [])]
            if any(bigram_jaccard(item["title"], t) >= threshold for t in titles):
                count += 1
        frequencies.append(count)
    return frequencies


def load_recent_snapshots(snapshot_dir: str, now: datetime | None = None,
                          days: int = SNAPSHOT_LOOKBACK_DAYS) -> list[dict]:
    """读取最近 N 天的快照文件（按文件名倒序，取最新的 FREQUENCY_WINDOW 份）。"""
    import os

    now = now or datetime.now(BEIJING_TZ)
    cutoff = (now.astimezone(BEIJING_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    if not os.path.isdir(snapshot_dir):
        return []
    snapshots = []
    for name in sorted(os.listdir(snapshot_dir), reverse=True):
        if not name.endswith(".json"):
            continue
        if name[:10] < cutoff:
            continue
        try:
            with open(os.path.join(snapshot_dir, name), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                snapshots.append(data)
        except (OSError, ValueError):
            continue
        if len(snapshots) >= FREQUENCY_WINDOW:
            break
    return snapshots


def merge_platform_clusters(items: list[dict],
                            threshold: float = BIGRAM_THRESHOLD) -> list[dict]:
    """跨平台合并同一话题：bigram Jaccard >= threshold 的条目归入同一簇。

    返回簇列表，每个簇：
    - 主条目（排名最好的那条）的字段作为展示字段；
    - rank / rank_total 取主条目（最好名次）；
    - hotness = 簇内不同平台数；
    - cluster = 簇内所有条目（按排名升序），供折叠区展示各家报道。
    """
    clusters: list[list[dict]] = []
    for item in items:
        placed = False
        for cluster in clusters:
            if any(bigram_jaccard(item["title"], other["title"]) >= threshold
                   for other in cluster):
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    merged = []
    for cluster in clusters:
        cluster.sort(key=lambda x: (x.get("rank", 999), x.get("platform", "")))
        primary = dict(cluster[0])
        platforms = {x.get("platform") for x in cluster}
        primary["hotness"] = len(platforms)
        primary["source_count"] = len(platforms)
        primary["cluster"] = [
            {
                "source": x.get("platform", ""),
                "title_zh": x.get("title", ""),
                "title_en": "",
                "url": x.get("url", ""),
                "is_primary": i == 0,
                "rank": x.get("rank", 0),
            }
            for i, x in enumerate(cluster)
        ]
        merged.append(primary)

    merged.sort(key=lambda x: (x.get("score", 0), -x.get("rank", 999)), reverse=True)
    return merged


# ── 国内热点质量筛选（大事件优先，不要求数量）────────────────────────

# 纯缩写/过短标题：看不出发生了什么，如「APEC」「G20」
_LOW_INFO_EXACT = {"APEC", "G20", "G7", "GDP", "AI", "CEO", "IPO"}

# 花絮/八卦/知识点类模式：不是「大事件」，命中即淘汰。
# 用 contains_keyword 的词边界逻辑，中文按子串匹配。
_TRIVIA_PATTERNS = [
    "摘手套", "握手时", "拍拍拍", "知识点", "随行人员", "表情包", "同款",
    "reaction", " Reaction", "围观", "可爱", "萌", "搞笑", "盘点",
]

# 重大事件信号词：命中越多，越可能是「今天的大事」
_MAJOR_SIGNALS = [
    "习近平", "普京", "李在明", "会见", "会谈", "峰会", "开幕", "出席",
    "国务院", "中央", "发布", "宣布", "推出", "实施", "通过", "启动",
    "规划", "政策", "重大", "首次", "全面", "国务院常务会议", "新闻发布会",
]


def is_low_quality_title(title: str) -> bool:
    """判断是否为低信息量/花絮类标题，应当从国内热点主列表中剔除。

    规则保守，宁可放过也不误杀真新闻：
    - 纯英文缩写（APEC / G20 等）或与之一致的短标题；
    - 标题长度 < 4 个汉字；
    - 命中明确的花絮关键词（摘手套、拍拍拍、知识点等）。
    """
    text = (title or "").strip()
    if not text:
        return True
    if text.upper() in _LOW_INFO_EXACT:
        return True
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    if chinese_count < 4:
        return True
    for keyword in _TRIVIA_PATTERNS:
        if keyword in text:
            return True
    return False


def major_event_boost(title: str) -> float:
    """大事件信号加分：每命中一个信号词 +0.02，最多 +0.12（封顶）。"""
    return min(0.12, sum(1 for k in _MAJOR_SIGNALS if k in (title or "")) * 0.02)


def refine_domestic(items: list[dict], top_n: int = 8) -> list[dict]:
    """国内热点质量筛选：淘汰低信息量标题，大事件加权，取前 N 条精品。

    - 过滤：is_low_quality_title 命中的条目直接丢弃。
    - 加权：score += major_event_boost(title)，重新排序。
    - 限量：只取前 top_n 条（默认 8），质量优先、不要求数量。
    - 安全兜底：若过滤后不足 top_n 条，放宽到不过滤的原始列表补足，
      保证发布闸门（>=3 条）不会因过滤过度而误伤。
    """
    kept = [x for x in items if not is_low_quality_title(x.get("title", ""))]
    if not kept:
        kept = list(items)  # 极端情况全被过滤时退回原始列表
    for item in kept:
        item["score"] = round(item.get("score", 0) + major_event_boost(item.get("title", "")), 4)
    kept.sort(key=lambda x: (x.get("score", 0), -x.get("rank", 999)), reverse=True)
    return kept[:top_n]
