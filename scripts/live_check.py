#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实环境联调脚本（不消耗 Cloudflare 额度）。

用途：在提交代码前，用**真实的 RSS 抓取 + 假的 AI 客户端**跑一遍，
确认「新闻源能不能抓到、字段解析对不对、聚类效果如何」。
只有当你想验证 AI 链路时才传 --real-ai（那会消耗额度，默认关闭）。

    python scripts/live_check.py                # 真实抓取 + 假 AI（零额度）
    python scripts/live_check.py --real-ai      # 真实抓取 + 真实 AI（消耗额度）

注意：本脚本只做检查并打印报告，**不写任何站点文件**，不会污染 index.html / data / audio。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import generate  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="真实抓取联调检查（默认零额度）")
    parser.add_argument("--real-ai", action="store_true",
                        help="使用真实 Cloudflare AI（会消耗额度，默认用假 AI）")
    args = parser.parse_args()

    print("=" * 70)
    print("真实抓取联调检查")
    print("=" * 70)

    # ---------- L1 真实抓取 ----------
    print("\n【L1 真实抓取国际源】")
    intl_raw, intl_status = generate.collect_all(
        generate.INTL_FEEDS, "international", http_get=generate.http_get_bytes
    )
    for status in intl_status:
        print("  %-14s %-6s 条目 %-3d %s" % (
            status["源"], status["状态"], status["条目数"], status["说明"] or ""))

    print("\n【L1 真实抓取国内源】")
    dom_raw, dom_status = generate.collect_all(
        generate.DOMESTIC_FEEDS, "domestic", http_get=generate.http_get_bytes
    )
    for status in dom_status:
        print("  %-14s %-6s 条目 %-3d %s" % (
            status["源"], status["状态"], status["条目数"], status["说明"] or ""))

    # ---------- L2 规范化 ----------
    print("\n【L2 规范化】")
    intl_items, intl_stats = generate.normalize_items(intl_raw)
    dom_items, dom_stats = generate.normalize_items(dom_raw)
    print("  国际：%s" % intl_stats)
    print("  国内：%s" % dom_stats)

    if not intl_items and not dom_items:
        print("\n❌ 全部源都抓不到，无法继续检查")
        return 1

    # ---------- 字段抽样核对 ----------
    print("\n【字段抽样核对（国际前 5 条）】")
    for item in intl_items[:5]:
        print("  来源=%s 语言=%s 有图=%s 摘要=%d字" % (
            item["source"], item["lang"], "是" if item["image"] else "否",
            len(item["summary"])))
        print("    标题：%s" % item["title"][:70])
        print("    链接：%s" % item["url"][:70])
        if item["image"]:
            print("    图片：%s" % item["image"][:70])

    print("\n【字段抽样核对（国内前 5 条）】")
    for item in dom_items[:5]:
        print("  来源=%s 语言=%s 有图=%s" % (
            item["source"], item["lang"], "是" if item["image"] else "否"))
        print("    标题：%s" % item["title"][:60])
        print("    摘要：%s" % (item["summary"][:60] or "(无)"))

    # ---------- AI 客户端 ----------
    if args.real_ai:
        client = generate.AIClient()
        if not client.configured:
            print("\n⚠️  没有配置 CF_ACCOUNT_ID / CF_API_TOKEN，无法用真实 AI")
            return 1
        print("\n【使用真实 AI（会消耗额度）】")
    else:
        client = generate.FakeAIClient(generate.today_bj())
        print("\n【使用假 AI 客户端（零额度）】")

    # ---------- L3 翻译 + 聚类 ----------
    print("\n【L3 标题翻译】")
    trans_intl = generate.translate_titles(intl_items, client)
    trans_dom = generate.translate_titles(dom_items, client)
    print("  国际：%s" % trans_intl)
    print("  国内：%s" % trans_dom)

    print("\n【L3 聚类结果（国际，按家数排序前 8 簇）】")
    clusters = generate.build_clusters(intl_items, "i")
    for cluster in clusters[:8]:
        print("  [%s] %d 家 | %s" % (
            cluster["cid"], cluster["source_count"],
            (cluster["primary_title_zh"] or "")[:60]))
        for item in cluster["items"]:
            print("        - %-14s %s" % (item["source"], item["title"][:55]))

    print("\n【L3 聚类结果（国内，按家数排序前 5 簇）】")
    dom_clusters = generate.build_clusters(dom_items, "c")
    for cluster in dom_clusters[:5]:
        print("  [%s] %d 家 | %s" % (
            cluster["cid"], cluster["source_count"],
            (cluster["primary_title_zh"] or "")[:60]))

    # ---------- 参考：真 AI 生成的大事 ----------
    if args.real_ai:
        print("\n【L3 真实 AI 生成今日大事（国际）】")
        day, _meta = generate.build_day(
            {"international": intl_items, "domestic": dom_items}, client,
            generate.today_bj(),
        )
        for key, label in (("international", "国际"), ("domestic", "国内")):
            for event in day["sections"][key]:
                print("  [%s] %s —— %s（%d 家）" % (
                    event["cid"], event["zh"], event["importance"] or "—",
                    event["source_count"]))
        print("\n【播报稿】")
        print("  %s" % (day["broadcast"] or "(空)")[:300])
        print("\n【AI 消耗】%s" % client.usage_report())

    # ---------- 汇总 ----------
    print("\n" + "=" * 70)
    ok_intl = [s for s in intl_status if s["状态"] == "正常"]
    ok_dom = [s for s in dom_status if s["状态"] == "正常"]
    print("国际源：%d/%d 可用" % (len(ok_intl), len(intl_status)))
    print("国内源：%d/%d 可用" % (len(ok_dom), len(dom_status)))
    print("规范化后：国际 %d 条、国内 %d 条" % (len(intl_items), len(dom_items)))
    multi = [c for c in clusters if c["source_count"] >= 2]
    print("多源簇：%d 个（体现「多家媒体报道」的事件数）" % len(multi))
    print("=" * 70)

    problems = []
    if not ok_intl:
        problems.append("国际源全部失败")
    if len(intl_items) < 5:
        problems.append("国际条目过少（%d 条）" % len(intl_items))
    if not ok_dom and not dom_items:
        problems.append("国内源全部失败（会自动退化，但建议修配置）")
    if problems:
        print("⚠️  需要关注：%s" % "；".join(problems))
        return 1
    print("✅ 抓取链路正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
