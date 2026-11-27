#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_site.py — 构建后的验收检查（发布闸门第二步）。

为什么需要它
------------
build_site.py 的内容校验只能检查「输入数据」。但历史上真正的事故是
「输入合法、渲染丢失」：top_5 是字符串、china_impact 结构变了，
JSON 完全合法，构建成功，页面却整块空白。

因此这里直接检查「输出 HTML」，用渲染结果反推是否真的把数据展示全了：

  * 每条新闻都要在页面上出现（按标题/ID 计数）
  * 置顶五条必须全部出现在首页与详情页
  * 有多少条带了对华影响，页面就要有多少个对华影响区块
  * 站内链接必须可解析（不允许出现 {esc(...)} 这类未渲染字面量）
  * 不允许 None / undefined / Python 字典字面量泄漏
  * sitemap / robots / 404 必须存在

用法
----
    python scripts/verify_site.py --dist dist --news news
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import normalize as N  # noqa: E402

BAD_LITERALS = (
    "{esc(",
    "{len(",
    "{'",
    "{{",
    "}}",
    ">None<",
    "None 分",
    "undefined",
)

# 需要按词边界判定的（避免 "finance" 里的 "nan"、URL 里的 "none" 误报）
BAD_WORDS = re.compile(r"\b(nan|NaN)\b")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check_one(day: str, html: str, briefing: dict) -> list[str]:
    """对单期详情页做完整性核对，返回错误列表。"""
    errs: list[str] = []
    items = briefing["items"]

    # 1) 每条新闻都应出现（用标题前 12 字做锚点，避免全角差异）
    missing = []
    for item in items:
        anchor = (item.get("title") or "")[:12]
        if anchor and anchor not in html:
            missing.append(item.get("id") or anchor)
    if missing:
        errs.append(f"[{day}] {len(missing)} 条新闻未渲染：{', '.join(missing[:8])}")

    # 2) 置顶五条全部出现
    for t in briefing["top5"]:
        anchor = (t.get("title") or "")[:12]
        if anchor and anchor not in html:
            errs.append(f"[{day}] 置顶条目未渲染：{t.get('id')}")

    # 3) 对华影响区块数量应等于带渠道的条目数
    with_channels = sum(1 for i in items if (i.get("china_impact") or {}).get("channels"))
    rendered = html.count("对中国的潜在影响")
    if with_channels and rendered < with_channels:
        errs.append(
            f"[{day}] 对华影响区块缺失：应有 {with_channels} 个，实际 {rendered} 个"
        )

    # 4) 来源链接数量不应少于有来源的条目数
    with_src = sum(1 for i in items if i.get("sources"))
    src_blocks = html.count('class="sources-list"')
    if with_src and src_blocks < with_src:
        errs.append(f"[{day}] 来源区块缺失：应有 {with_src} 个，实际 {src_blocks} 个")

    # 5) 突发条目必须有横幅
    breaking = [i for i in items if i["breaking"]["is_breaking"]]
    if breaking and "tag-breaking" not in html:
        errs.append(f"[{day}] 有 {len(breaking)} 条突发新闻但未渲染横幅")

    return errs


def check_literals(day: str, html: str) -> list[str]:
    errs = []
    for lit in BAD_LITERALS:
        if lit in html:
            count = html.count(lit)
            errs.append(f"[{day}] 出现未渲染字面量 {lit!r} ×{count}")
    m = BAD_WORDS.findall(html)
    if m:
        errs.append(f"[{day}] 出现 NaN 字面量 ×{len(m)}")
    return errs


def check_links(name: str, html: str, dist: Path) -> list[str]:
    """站内链接与资源引用必须能对应到 dist 下的实际文件。"""
    errs = []
    refs = set(re.findall(r'(?:href|src)="(/[^"#?]*)"', html))
    for ref in refs:
        if ref.startswith("//"):
            continue
        target = ref.strip("/")
        if not target:
            cand = [dist / "index.html"]
        else:
            cand = [
                dist / target,
                dist / f"{target}.html",
                dist / target / "index.html",
                dist / target / "index.htm",
            ]
        if any(c.exists() for c in cand):
            continue
        # 音频/新闻等产物缺失必须报错；其余同样视为失效
        errs.append(f"[{name}] 引用指向不存在的产物：{ref}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="dist")
    ap.add_argument("--news", default="news")
    args = ap.parse_args()

    dist = Path(args.dist)
    news = Path(args.news)
    errors: list[str] = []

    if not (dist / "index.html").exists():
        print("错误：缺少 dist/index.html", file=sys.stderr)
        return 1

    # 必要文件
    for need in ("index.html", "archive.html", "404.html", "style.css",
                 "sitemap.xml", "robots.txt", "archive.json"):
        if not (dist / need).exists():
            errors.append(f"缺少产物：{need}")

    # 逐期核对
    pages = sorted((dist / "news").glob("*.html"))
    if not pages:
        errors.append("没有任何期次页面")
    else:
        for page in pages:
            day = page.stem
            raw_path = news / day[:4] / day[5:7] / f"{day}.json"
            if not raw_path.exists():
                errors.append(f"[{day}] 找不到源 JSON：{raw_path}")
                continue
            data = json.loads(raw_path.read_text(encoding="utf-8-sig"))
            briefing = N.normalize_briefing(data, day)
            html = read(page)
            errors += check_one(day, html, briefing)
            errors += check_literals(day, html)
            errors += check_links(day, html, dist)

        # 首页与归档也要查字面量泄漏
        for name in ("index.html", "archive.html", "404.html"):
            html = read(dist / name)
            errors += check_literals(name, html)
            errors += check_links(name, html, dist)

    # 首页必须出现最新一期置顶
    idx = read(dist / "index.html")
    if pages:
        latest_day = pages[-1].stem if pages else ""
        raw_path = news / latest_day[:4] / latest_day[5:7] / f"{latest_day}.json"
        if raw_path.exists():
            latest = N.normalize_briefing(
                json.loads(raw_path.read_text(encoding="utf-8-sig")), latest_day
            )
            for t in latest["top5"]:
                anchor = (t.get("title") or "")[:12]
                if anchor and anchor not in idx:
                    errors.append(f"[index] 最新一期置顶未渲染：{t.get('id')}")

    if errors:
        print(f"验收未通过：{len(errors)} 个问题", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"验收通过：{len(pages)} 期页面，链接与内容完整性正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
