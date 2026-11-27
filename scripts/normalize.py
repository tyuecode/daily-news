#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize.py — 把不同期次的新闻 JSON 统一成同一套内部结构。

为什么需要这一层
----------------
新闻 JSON 由模型生成，不同期次的字段名和嵌套方式会漂移。已实测到三代：

  v1（2026-11-25）  impact: degree/horizon/confidence
                    china_impact: 嵌套 channels
                    top5: 对象数组
                    watch: why_watch / china_link / source(字符串)

  v2（2026-11-26）  impact: level/period
                    china_impact: 扁平 "trade"/"finance"…
                    top5: 对象数组
                    watch: reason / china_relation / sources(数组)

  v3（2026-11-27）  impact: level/period
                    china_impact: 扁平（含 industry）
                    top5: 字符串 ID 数组
                    watch: 混合 event/event_name/name

如果渲染器直接读原始字段，任何一次漂移都会让整块内容静默消失——
这已经在线上真实发生过（置顶五条、对华影响、观察事项原因全部丢失，
CI 却因为“JSON 语法合法”而放行）。

因此所有渲染只允许消费本模块的输出，不再直接读原始 JSON。

设计原则
--------
1. 只做结构适配，不编造内容。缺的字段留空，绝不用占位文字填充。
2. 同时保留原文（*_raw）以便质控报告说明“该字段在原始数据中缺失”。
3. 解析失败不抛异常，降级为空并在 issues 中记录，由闸门决定是否阻断。
"""

from __future__ import annotations

import re
from typing import Any, Optional

# 影响渠道的展示顺序与中文标签（v1 的 channels 与 v2/v3 的扁平字段共用）
CHANNEL_ORDER = [
    ("trade", "外贸"),
    ("finance", "金融"),
    ("technology", "科技"),
    ("energy", "能源"),
    ("industry", "产业"),
    ("employment", "就业"),
    ("public_livelihood", "民生"),
    ("policy", "政策"),
]

# 未来观察的分类标签
WATCH_SECTIONS = [
    ("events", "重点日程"),
    ("economic_calendar", "重要经济数据"),
    ("policy_announcements", "中国政策与会议"),
    ("diplomatic_events", "外交与国际会议"),
    ("technology_events", "企业与科技事件"),
    ("potential_risks", "风险观察"),
]


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _text(value: Any) -> str:
    """把任意值转成去空白的字符串；None 与容器安全降级。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "；".join(f"{k}：{_text(v)}" for k, v in value.items() if _text(v))
    if isinstance(value, (list, tuple)):
        return "；".join(t for t in (_text(v) for v in value) if t)
    return str(value).strip()


def _list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _first(*values: Any) -> str:
    for v in values:
        t = _text(v)
        if t:
            return t
    return ""


def _score(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if 0 <= f <= 10 else None


# --------------------------------------------------------------------------
# 影响评估：degree/horizon 与 level/period 两套命名
# --------------------------------------------------------------------------
def normalize_impact(raw: Any) -> dict:
    """
    {'degree'|'level','horizon'|'period','confidence','details'} -> 统一键名
    """
    if not isinstance(raw, dict):
        return {"degree": "", "horizon": "", "confidence": "", "details": ""}
    return {
        "degree": _first(raw.get("degree"), raw.get("level"), raw.get("impact")),
        "horizon": _first(raw.get("horizon"), raw.get("period"), raw.get("timeframe")),
        "confidence": _first(raw.get("confidence"), raw.get("certainty")),
        "details": _text(raw.get("details") or raw.get("description") or raw.get("note")),
    }


# 扁平渠道值形如：「高｜短中期｜高：说明文字」
_CHANNEL_VALUE_RE = re.compile(
    r"^\s*(?P<degree>[^｜|：:]{1,8})\s*[｜|]\s*(?P<horizon>[^｜|：:]{1,8})\s*[｜|]\s*(?P<rest>.+)$"
)


def _parse_channel_value(value: str) -> tuple[str, str, str]:
    """把「高｜短期｜中：说明」拆成 (degree, horizon, 正文)。"""
    text = _text(value)
    if not text:
        return "", "", ""
    m = _CHANNEL_VALUE_RE.match(text)
    if not m:
        return "", "", text
    degree = m.group("degree").strip()
    horizon = m.group("horizon").strip()
    rest = m.group("rest").strip()
    # rest 形如「高：说明」时，冒号前是可信度
    for sep in ("：", ":"):
        if sep in rest:
            head, _, tail = rest.partition(sep)
            if len(head) <= 12 and _text(tail):
                return degree, horizon, _text(tail)
    return degree, horizon, rest


def normalize_china_impact(raw: Any) -> dict:
    """
    两种形态统一成 {degree, horizon, confidence, channels:{key: text}}：

      嵌套式  {"degree":"中","horizon":"短期","confidence":"中","channels":{"trade":"…"}}
      扁平式  {"trade":"高｜短中期｜高：…", "finance":"…"}
    """
    if not isinstance(raw, dict):
        return {"degree": "", "horizon": "", "confidence": "", "channels": {}}

    channels: dict[str, str] = {}
    degrees: list[str] = []
    horizons: list[str] = []
    confidences: list[str] = []

    nested = raw.get("channels")
    if isinstance(nested, dict):
        for key, value in nested.items():
            text = _text(value)
            if text:
                channels[str(key)] = text
        return {
            "degree": _first(raw.get("degree"), raw.get("level")),
            "horizon": _first(raw.get("horizon"), raw.get("period")),
            "confidence": _first(raw.get("confidence")),
            "channels": channels,
        }

    # 扁平式：已知渠道键直接取，未知标量键也保留（避免丢信息）
    for key, value in raw.items():
        if key in ("degree", "level", "horizon", "period", "confidence", "details"):
            continue
        if not isinstance(value, str):
            continue
        degree, horizon, body = _parse_channel_value(value)
        if not body:
            continue
        channels[str(key)] = body
        if degree:
            degrees.append(degree)
        if horizon:
            horizons.append(horizon)
        if degree and horizon and body:
            confidences.append("")

    return {
        "degree": _first(raw.get("degree"), raw.get("level")),
        "horizon": _first(raw.get("horizon"), raw.get("period")),
        "confidence": _first(raw.get("confidence")),
        "channels": channels,
    }


# --------------------------------------------------------------------------
# 突发标记
# --------------------------------------------------------------------------
BREAKING_KEYWORDS = ("窗口后", "窗口外", "突发", "重大更新", "08:00后", "08:00 后")


def detect_breaking(item: dict) -> dict:
    """
    判定一条新闻是否属于「突发 / 窗口后重大更新」。

    优先用结构化字段；字段缺失时退回文本标记（当前数据实际只写在文字里）。
    返回 {'is_breaking': bool, 'window_status': str, 'evidence': str}
    """
    status = _first(
        item.get("window_status"),
        item.get("window"),
        item.get("breaking_status"),
    )
    flag = item.get("breaking")
    is_flag = flag is True or (isinstance(flag, str) and flag.strip().lower() in ("true", "yes", "1"))

    evidence = ""
    if not is_flag:
        # 结构化字段没写时，扫描几个高信噪比位置
        haystack = " ".join(
            [
                status,
                _text(item.get("event_time")),
                _text(item.get("publish_time")),
                _text(item.get("summary")),
                _text(item.get("importance_reason")),
                _text(item.get("verification_status")),
            ]
        )
        for kw in BREAKING_KEYWORDS:
            if kw in haystack:
                evidence = kw
                break

    is_breaking = bool(is_flag or (status and any(k in status for k in BREAKING_KEYWORDS)) or evidence)
    return {"is_breaking": bool(is_breaking), "window_status": status, "evidence": evidence}


# --------------------------------------------------------------------------
# 单条新闻
# --------------------------------------------------------------------------
def normalize_news_item(raw: Any, region: str, section: str = "") -> dict:
    """
    把一条原始新闻规范化。

    region:  'international' | 'china'
    section: 该条目在 JSON 中所属的键名（politics_and_diplomacy 等）。
             这是权威的分类来源——条目自带的 category 字段是中文标签，
             与嵌套键不是一回事，早期按 category 匹配键名导致条目错位。
    """
    if not isinstance(raw, dict):
        return {}

    sources = []
    for s in _list(raw.get("sources")):
        if not isinstance(s, dict):
            continue
        sources.append(
            {
                "organization": _first(s.get("organization"), s.get("publisher"), s.get("source"), s.get("media")),
                "title": _text(s.get("title")),
                "url": _text(s.get("url") or s.get("link")),
                "publish_time": _text(s.get("publish_time") or s.get("published_at") or s.get("date")),
            }
        )

    item = {
        "id": _text(raw.get("id")),
        "title": _text(raw.get("title")),
        "category": section or _text(raw.get("category")),
        "category_label": _text(raw.get("category")),
        "region": region,
        "location": _text(raw.get("location")),
        "event_time": _text(raw.get("event_time")),
        "publish_time": _text(raw.get("publish_time")),
        "summary": _text(raw.get("summary")),
        "key_facts": [t for t in (_text(x) for x in _list(raw.get("key_facts"))) if t],
        "importance_score": _score(raw.get("importance_score") or raw.get("importance")),
        "importance_reason": _first(raw.get("importance_reason"), raw.get("reason")),
        "impact": normalize_impact(raw.get("impact_assessment") or raw.get("impact")),
        "china_impact": normalize_china_impact(
            raw.get("china_impact_analysis") or raw.get("china_impact")
        ),
        "verification_status": _text(raw.get("verification_status")),
        "developing_details": [t for t in (_text(x) for x in _list(raw.get("developing_details"))) if t],
        "follow_up": [t for t in (_text(x) for x in _list(raw.get("follow_up"))) if t],
        "sources": sources,
        "tags": [t for t in (_text(x) for x in _list(raw.get("tags"))) if t],
    }
    item["breaking"] = detect_breaking(raw)
    return item


# --------------------------------------------------------------------------
# 置顶五条
# --------------------------------------------------------------------------
def normalize_top5(raw: Any, items_by_id: dict) -> list[dict]:
    """
    支持两种形态：
      对象数组 [{'id','title','reason','importance_score'}]
      字符串数组 ['INT-…-11', …]  ← v3 实际形态
    字符串形态下，标题/理由/分数从新闻条目回填。
    """
    result: list[dict] = []
    for entry in _list(raw):
        if isinstance(entry, dict):
            nid = _text(entry.get("id"))
            src = items_by_id.get(nid, {})
            result.append(
                {
                    "id": nid,
                    "title": _first(entry.get("title"), src.get("title")),
                    "reason": _first(entry.get("reason"), src.get("importance_reason")),
                    "score": _score(entry.get("importance_score")) or src.get("importance_score"),
                    "window_status": _text(entry.get("window_status")),
                    "resolved": bool(nid and nid in items_by_id),
                }
            )
        elif isinstance(entry, str):
            nid = entry.strip()
            src = items_by_id.get(nid, {})
            result.append(
                {
                    "id": nid,
                    "title": src.get("title", ""),
                    "reason": src.get("importance_reason", ""),
                    "score": src.get("importance_score"),
                    "window_status": "",
                    "resolved": nid in items_by_id,
                }
            )
    return result


# --------------------------------------------------------------------------
# 未来 24 小时观察
# --------------------------------------------------------------------------
def normalize_watch_entry(raw: Any, section: str) -> dict:
    if not isinstance(raw, dict):
        return {}
    sources = [t for t in (_text(x) for x in _list(raw.get("sources") or raw.get("source"))) if t]
    return {
        "section": section,
        "name": _first(
            raw.get("name"),
            raw.get("event_name"),
            raw.get("event"),
            raw.get("risk"),
            raw.get("title"),
        ),
        "beijing_time": _first(raw.get("beijing_time"), raw.get("time"), raw.get("scheduled_time")),
        "status": _text(raw.get("status")),
        "why_watch": _first(raw.get("why_watch"), raw.get("reason"), raw.get("why")),
        "potential_impact": _text(raw.get("potential_impact") or raw.get("impact")),
        "china_link": _first(raw.get("china_link"), raw.get("china_relation"), raw.get("china")),
        "priority": _text(raw.get("priority")),
        "sources": sources,
    }


def normalize_watchlist(raw: Any) -> dict:
    watch = raw if isinstance(raw, dict) else {}
    out: dict[str, list] = {}
    for key, _label in WATCH_SECTIONS:
        rows = [normalize_watch_entry(e, key) for e in _list(watch.get(key))]
        out[key] = [r for r in rows if r and r.get("name")]
    return out


# --------------------------------------------------------------------------
# 整期
# --------------------------------------------------------------------------
def normalize_briefing(data: dict, date_str: str) -> dict:
    """把一期原始 JSON 规范化为渲染层可直接消费的结构。"""
    data = data if isinstance(data, dict) else {}

    config = data.get("briefing_config") if isinstance(data.get("briefing_config"), dict) else {}
    overview = data.get("daily_overview") if isinstance(data.get("daily_overview"), dict) else {}
    dashboard_raw = data.get("china_impact_dashboard")
    qc = data.get("quality_control") if isinstance(data.get("quality_control"), dict) else {}

    # 拉平全部新闻，按 id 去重
    items: list[dict] = []
    seen: set[str] = set()
    for group, region in (("international_news", "international"), ("china_news", "china")):
        block = data.get(group)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if not isinstance(value, list):
                continue
            for raw in value:
                item = normalize_news_item(raw, region, key)
                if not item or not item.get("title"):
                    continue
                nid = item["id"]
                if nid:
                    if nid in seen:
                        continue
                    seen.add(nid)
                items.append(item)

    items_by_id = {i["id"]: i for i in items if i["id"]}

    dashboard = normalize_china_impact(dashboard_raw)
    # 概览表用 degree/horizon/confidence + 各维度正文，与逐条影响同构
    if isinstance(dashboard_raw, dict):
        for key, _label in CHANNEL_ORDER:
            value = dashboard_raw.get(key)
            text = _text(value)
            if text:
                degree, horizon, body = _parse_channel_value(text)
                dashboard["channels"].setdefault(key, body or text)
                if degree and not dashboard["degree"]:
                    dashboard["degree"] = degree

    return {
        "date_str": date_str,
        "config": {
            "title": _text(config.get("title")),
            "news_date": _text(config.get("news_date")),
            "generated_at": _text(config.get("generated_at")),
            "timezone": _first(config.get("timezone"), "Asia/Shanghai"),
            "coverage_period": _text(config.get("coverage_period")),
        },
        "overview": {
            "headline": _text(overview.get("headline")),
            "international_overview": _text(overview.get("international_overview")),
            "china_overview": _text(overview.get("china_overview")),
            "key_takeaways": [t for t in (_text(x) for x in _list(overview.get("key_takeaways"))) if t],
        },
        "top5": normalize_top5(data.get("top_5_headlines"), items_by_id),
        "items": items,
        "items_by_id": items_by_id,
        "dashboard": dashboard,
        "dashboard_overall": _first(
            (dashboard_raw or {}).get("overall_outlook") if isinstance(dashboard_raw, dict) else "",
        ),
        "dashboard_confidence": _first(
            (dashboard_raw or {}).get("confidence") if isinstance(dashboard_raw, dict) else "",
        ),
        "watch": normalize_watchlist(data.get("next_24_hours_watchlist")),
        "quality": {
            "duplicate_check": qc.get("duplicate_check") is True,
            "major_news_cross_verified": qc.get("major_news_cross_verified") is True,
            "unverified_claims_marked": qc.get("unverified_claims_marked") is True,
            "source_urls_checked": qc.get("source_urls_checked") is True,
            "present": bool(qc),
        },
    }
