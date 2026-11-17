#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时冒烟测试。

为什么必须有这个文件：
    历史教训——`py_compile` 只查语法，不查 NameError。上一次的实现就是因为
    一个函数定义被误删，语法检查通过、线上却 `NameError: clean_title is not
    defined`，导致抓取全线失败、整站空白。
    所以本文件不是「单元测试」，而是**实际调用每一个关键函数**的运行时测试，
    零网络、零 Cloudflare 调用、零额度消耗，几十毫秒跑完。

在 workflow 里它排在 `generate.py` 之前，失败即中止，绝不带着坏代码去抓新闻。

跑法：
    python smoke_test.py
    python smoke_test.py -v     显示每个用例名
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import traceback

# 让脚本能 import 到 scripts/ 下的模块
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ai_client  # noqa: E402
import generate  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

BJ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 迷你断言框架


class Case:
    def __init__(self):
        self.total = 0
        self.passed = 0
        self.failures = []


CASE = Case()
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def check(name, condition, detail=""):
    """一个断言。失败不中断，继续跑完所有用例，最后统一汇报。"""
    CASE.total += 1
    if condition:
        CASE.passed += 1
        if VERBOSE:
            print("  ✅ %s" % name)
    else:
        CASE.failures.append((name, detail))
        print("  ❌ %s%s" % (name, ("  →  " + str(detail)) if detail else ""))


def group(title):
    """开一个用例组。"""
    print("\n【%s】" % title)


def expect_eq(name, actual, expected):
    check(name, actual == expected, "实际=%r 期望=%r" % (actual, expected))


# ---------------------------------------------------------------- 1. AI 返回结构


def test_ai_payloads():
    group("AI 返回结构的两种格式（只认一种会把成功误判成失败）")

    # 格式 1：纯文本模型
    payload1 = {"result": {"response": "中文译文"}, "success": True}
    expect_eq("结构一 result.response", ai_client.extract_text(payload1), "中文译文")

    # 格式 2：OpenAI 兼容模型（gemma 走这条）
    payload2 = {
        "result": {"choices": [{"message": {"role": "assistant", "content": "中文译文"}}]},
    }
    expect_eq("结构二 result.choices[0].message.content",
              ai_client.extract_text(payload2), "中文译文")

    # 结构二：没有 success 字段也要认（实测确实可能缺）
    payload3 = {"result": {"choices": [{"message": {"content": "无 success 字段"}}]}}
    expect_eq("结构二缺 success 字段", ai_client.extract_text(payload3), "无 success 字段")

    # content 是数组的畸形情况
    payload4 = {"result": {"choices": [{"message": {"content": [{"text": "数组型"}, {"text": "内容"}]}}]}}
    expect_eq("结构二 content 为数组", ai_client.extract_text(payload4), "数组型内容")

    # success=false 必须抛异常
    try:
        ai_client.extract_text({"success": False, "errors": [{"message": "配额不足"}]})
        check("success=false 抛 AIError", False, "没有抛异常")
    except ai_client.AIError:
        check("success=false 抛 AIError", True)

    # 完全无法识别的结构必须抛异常
    try:
        ai_client.extract_text({"result": {"unknown_field": 1}})
        check("无法识别结构抛 AIError", False, "没有抛异常")
    except ai_client.AIError:
        check("无法识别结构抛 AIError", True)


def test_code_fence():
    group("markdown 代码块清洗")
    expect_eq("带 json 标注的代码块",
              ai_client.strip_code_fence('```json\n{"a": 1}\n```'), '{"a": 1}')
    expect_eq("无标注代码块", ai_client.strip_code_fence("```\n甲\n乙\n```"), "甲\n乙")
    expect_eq("普通文本原样返回", ai_client.strip_code_fence("正常输出"), "正常输出")
    expect_eq("空字符串", ai_client.strip_code_fence(""), "")


def test_ai_retry():
    group("429 / 5xx 指数退避重试（1s→2s→4s），3 次失败后抛异常")

    sleeps = []

    class FlakyClient(ai_client.AIClient):
        """前两次返回 429，第三次成功。"""

        def __init__(self):
            super().__init__(account_id="acc", api_token="tok", sleep=sleeps.append)
            self.attempts = 0

        def _post(self, body_bytes):
            self.attempts += 1
            if self.attempts < 3:
                raise ai_client._Retryable("演练：HTTP 429")
            return {"result": {"response": "第三次成功了"}, "success": True}

    client = FlakyClient()
    expect_eq("退避后成功", client.complete("测试"), "第三次成功了")
    expect_eq("重试了 3 次", client.attempts, 3)
    expect_eq("退避间隔 1s、2s", sleeps, [1.0, 2.0])

    # 3 次全失败 → 抛 AIError（由上层降级）
    class AlwaysFail(ai_client.AIClient):
        def __init__(self):
            super().__init__(account_id="acc", api_token="tok", sleep=lambda _s: None)

        def _post(self, body_bytes):
            raise ai_client._Retryable("演练：HTTP 503")

    try:
        AlwaysFail().complete("测试")
        check("3 次失败抛 AIError", False, "没有抛异常")
    except ai_client.AIError:
        check("3 次失败抛 AIError", True)

    # 真实的 _post 必须把 HTTP 429/5xx 翻译成可重试错误，把 401 翻译成不可重试错误。
    # （这条最容易写漏：HTTPError 没被归类，重试逻辑就形同虚设。）
    import urllib.error

    def _raise_http(code):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError("url", code, "err", {}, None)

        return fake_urlopen

    for code in (429, 500, 503):
        client = ai_client.AIClient(account_id="a", api_token="t", sleep=lambda _s: None)
        original = ai_client.urllib.request.urlopen
        ai_client.urllib.request.urlopen = _raise_http(code)
        try:
            client._post(b"{}")
            check("HTTP %d 被归类为可重试" % code, False, "没有抛异常")
        except ai_client._Retryable:
            check("HTTP %d 被归类为可重试" % code, True)
        except ai_client.AIError:
            check("HTTP %d 被归类为可重试" % code, False, "抛了不可重试的 AIError")
        finally:
            ai_client.urllib.request.urlopen = original

    client = ai_client.AIClient(account_id="a", api_token="t", sleep=lambda _s: None)
    original = ai_client.urllib.request.urlopen
    ai_client.urllib.request.urlopen = _raise_http(401)
    try:
        client._post(b"{}")
        check("HTTP 401 被归类为不可重试", False, "没有抛异常")
    except ai_client._Retryable:
        check("HTTP 401 被归类为不可重试", False, "401 不该重试")
    except ai_client.AIError:
        check("HTTP 401 被归类为不可重试", True)
    finally:
        ai_client.urllib.request.urlopen = original

    # 401 不该重试（凭据问题，重试没意义）
    class Unauthorized(ai_client.AIClient):
        def __init__(self):
            super().__init__(account_id="acc", api_token="tok", sleep=lambda _s: None)
            self.attempts = 0

        def _post(self, body_bytes):
            self.attempts += 1
            raise ai_client.AIError("演练：HTTP 401 不可重试")

    client401 = Unauthorized()
    try:
        client401.complete("测试")
    except ai_client.AIError:
        pass
    expect_eq("401 只调用 1 次不重试", client401.attempts, 1)


def test_ai_missing_credentials():
    group("缺少凭据时立刻失败（不发起任何网络请求）")

    class NoCreds(ai_client.AIClient):
        def __init__(self):
            super().__init__(account_id="", api_token="")

        def _post(self, body_bytes):
            raise AssertionError("没有凭据时不应该发起网络请求！")

    try:
        NoCreds().complete("测试")
        check("缺凭据抛 AIError", False, "没有抛异常")
    except ai_client.AIError:
        check("缺凭据抛 AIError", True)


# ---------------------------------------------------------------- 2. 词边界过滤


def test_word_boundary():
    group("词边界过滤（实测教训：'eu' 不能命中 Depardieu，'war' 不能命中 software）")

    check("Depardieu 不被 eu 过滤",
          generate.contains_word("Gerard Depardieu went on trial", "eu") is False)
    check("software 不被 war 过滤",
          generate.contains_word("New software released today", "war") is False)
    check("独立的 eu 能被命中",
          generate.contains_word("The EU summit opened in Brussels", "eu") is True)
    check("独立的 war 能被命中",
          generate.contains_word("War erupted in the region", "war") is True)
    check("u.s. 这种带点缩写也能命中",
          generate.contains_word("The U.S. envoy arrived", "u.s.") is True)

    # 整条软新闻判定
    check("足球标题被判为软新闻",
          generate.is_soft_news("Football star wins player of the year award") is True)
    check("Depardieu 庭审不被误判为软新闻",
          generate.is_soft_news("Gerard Depardieu trial opens in Paris court") is False)
    check("软件公司新闻不被误判为软新闻",
          generate.is_soft_news("Software outage hits airlines worldwide") is False)
    check("中文软新闻被识别", generate.is_soft_news("某明星演唱会门票售罄") is True)


# ---------------------------------------------------------------- 3. 聚类


def test_bigram_jaccard():
    group("中文 bigram 与 Jaccard 相似度")

    grams = generate.zh_bigrams("超级油轮")
    check("bigram 切分正确",
          grams == {"超级", "级油", "油轮"}, "实际=%r" % (grams,))
    expect_eq("完全相同的文本相似度为 1",
              generate.jaccard({"甲", "乙"}, {"甲", "乙"}), 1.0)
    expect_eq("完全不相交为 0", generate.jaccard({"甲", "乙"}, {"丙", "丁"}), 0.0)
    expect_eq("一边为空时为 0", generate.jaccard(set(), {"甲"}), 0.0)
    expect_eq("一半重叠为 1/3", round(generate.jaccard({"甲", "乙"}, {"乙", "丙"}), 4), 0.3333)
    check("英文字母与标点都不参与 bigram",
          generate.zh_bigrams("A股，大涨！") == {"股", "大涨"},
          "实际=%r" % (generate.zh_bigrams("A股，大涨！"),))
    check("单字连续段保留单字",
          generate.zh_bigrams("股 大涨") == {"股", "大涨"},
          "实际=%r" % (generate.zh_bigrams("股 大涨"),))


def test_clustering_rules():
    group("聚类三条铁律：同来源不合并、GN 不计票、多来源计票")

    def item(source, title_zh, url_suffix, hours_ago=2):
        return {
            "source": source,
            "section": "international",
            "title": title_zh,
            "title_zh": title_zh,
            "title_en": "",
            "summary": "",
            "url": "https://www.bbc.com/news/%s" % url_suffix,
            "image": "",
            "published": datetime.now(BJ) - timedelta(hours=hours_ago),
            "lang": "zh",
        }

    # 同一事件两家媒体 → 合并为一个簇
    items = [
        item("BBC", "超级油轮在霍尔木兹海峡触雷后沉没", "a"),
        item("The Guardian", "超级油轮在霍尔木兹海峡触雷后沉没", "b"),
    ]
    clusters = generate.cluster_items(items)
    expect_eq("同事件两家媒体合并成 1 簇", len(clusters), 1)
    expect_eq("计票为 2 家", generate.vote_count(clusters[0]["items"]), 2)

    # 同一家媒体的两条相似报道 → 绝不合并
    items = [
        item("BBC", "超级油轮在霍尔木兹海峡触雷后沉没", "a"),
        item("BBC", "超级油轮在霍尔木兹海峡触雷后沉没", "b"),
    ]
    clusters = generate.cluster_items(items)
    expect_eq("同来源两条不合并（2 簇）", len(clusters), 2)

    # Google News 与具体媒体同簇 → 不计票
    items = [
        item("BBC", "超级油轮在霍尔木兹海峡触雷后沉没", "a"),
        item("Google News", "超级油轮在霍尔木兹海峡触雷后沉没", "b"),
    ]
    clusters = generate.cluster_items(items)
    expect_eq("GN+BBC 合并成 1 簇", len(clusters), 1)
    expect_eq("GN 有具体媒体时不计票（1 家）",
              generate.vote_count(clusters[0]["items"]), 1)

    # Google News 单独成簇 → 计 1 票
    items = [item("Google News", "联合国警告苏丹面临饥荒风险", "c")]
    clusters = generate.cluster_items(items)
    expect_eq("GN 单独成簇计 1 家", generate.vote_count(clusters[0]["items"]), 1)

    # 完全不相干的事件不合并
    items = [
        item("BBC", "超级油轮在霍尔木兹海峡触雷后沉没", "a"),
        item("Sky News", "日本近海发生强震并发布海啸提示", "b"),
    ]
    clusters = generate.cluster_items(items)
    expect_eq("不相干事件不合并", len(clusters), 2)


def test_cid_stability():
    group("稳定 cid 分配（杜绝摘要与链接错位）")

    def raw_item(source, title, suffix):
        return {
            "source": source, "section": "international", "title": title,
            "title_zh": title, "title_en": "", "summary": "",
            "url": "https://www.bbc.com/news/%s" % suffix,
            "image": "",
            "published": datetime.now(BJ), "lang": "zh",
        }

    # 多源簇（3 家媒体同一个事件） vs 单源簇（1 家）
    raw = [
        raw_item("BBC", "超级油轮在霍尔木兹海峡触雷后沉没", "a"),
        raw_item("The Guardian", "超级油轮在霍尔木兹海峡触雷后沉没", "b"),
        raw_item("Al Jazeera", "超级油轮在霍尔木兹海峡触雷后沉没", "c"),
        raw_item("Sky News", "日本近海发生强震并发布海啸提示", "d"),
    ]
    built = generate.build_clusters(raw, "i")

    cids = [c["cid"] for c in built]
    check("cid 形如 i1/i2 且不重复",
          len(cids) == len(set(cids)) and all(c.startswith("i") for c in cids),
          "实际=%r" % (cids,))
    expect_eq("家数多的事件排第一", built[0]["source_count"], 3)
    expect_eq("单源事件排后面", built[1]["source_count"], 1)

    # cid 只由排序位置决定，跟条目顺序无关：同样的输入顺序不同，事件与 cid 的对应仍然稳定
    built_again = generate.build_clusters(list(reversed(raw)), "i")
    mapping_a = {c["primary_title_zh"]: c["cid"] for c in built}
    mapping_b = {c["primary_title_zh"]: c["cid"] for c in built_again}
    expect_eq("打乱输入顺序后事件与 cid 的对应关系不变", mapping_b, mapping_a)

    # 国内版块用 c 前缀
    dom = generate.build_clusters([raw_item("联合早报", "中国三季度经济增速超出市场预期", "e")], "c")
    check("国内版块 cid 以 c 开头", dom[0]["cid"].startswith("c"), dom[0]["cid"])


# ---------------------------------------------------------------- 4. 翻译解析


def test_translation_parsing():
    group("标题翻译编号解析（含缺编号降级）")

    text = "[1] 南非枪击事件致17人死亡\n[2] 油轮沉没\n"
    result = generate.parse_indexed_translations(text, 2)
    expect_eq("正常解析编号 1", result.get(1), "南非枪击事件致17人死亡")
    expect_eq("正常解析编号 2", result.get(2), "油轮沉没")

    # 模型只用中文序号、不带方括号
    text = "1. 第一条译文\n2. 第二条译文"
    result = generate.parse_indexed_translations(text, 2)
    expect_eq("无方括号也能解析", result.get(1), "第一条译文")

    # 缺编号：按出现顺序补位
    text = "[1] 有编号的\n没有编号的译文"
    result = generate.parse_indexed_translations(text, 2)
    expect_eq("缺编号按顺序补位", result.get(2), "没有编号的译文")

    # 编号乱序
    text = "[2] 第二条\n[1] 第一条"
    result = generate.parse_indexed_translations(text, 2)
    expect_eq("乱序编号按编号对位", result.get(1), "第一条")
    expect_eq("乱序编号按编号对位2", result.get(2), "第二条")

    # 超出范围的多余行被丢弃
    text = "[1] 唯一一条\n[99] 编外的内容"
    result = generate.parse_indexed_translations(text, 1)
    expect_eq("超范围编号不计入", len(result), 1)


def test_translate_titles_fallback():
    group("标题翻译失败时回退英文原文（不空掉整站）")

    class FailingAI:
        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            raise ai_client.AIError("演练：AI 全线失败")

    items = [
        {"title": "English headline one", "lang": "en", "title_zh": "", "title_en": ""},
        {"title": "中文标题", "lang": "zh", "title_zh": "", "title_en": ""},
    ]
    stats = generate.translate_titles(items, FailingAI())
    expect_eq("中文源零翻译（直接用原文）", items[1]["title_zh"], "中文标题")
    expect_eq("英文源翻译失败回退英文原文", items[0]["title_zh"], "English headline one")
    expect_eq("回退计数正确", stats["回退原文"], 1)

    class GoodAI:
        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            return "[1] 英文标题一号"

    items2 = [{"title": "English headline one", "lang": "en", "title_zh": "", "title_en": ""}]
    generate.translate_titles(items2, GoodAI())
    expect_eq("翻译成功写入中文", items2[0]["title_zh"], "英文标题一号")
    expect_eq("英文原文保留在 title_en", items2[0]["title_en"], "English headline one")


def test_batch_size():
    group("翻译分批（每批 8 条，一次给 20 条只会翻前 5 条）")

    seen_batches = []

    class CountingAI:
        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            numbered = [l for l in prompt.splitlines() if l.strip().endswith(tuple("abcdefghijklmnopqrstuvwxyz"))]
            lines = [l for l in prompt.splitlines() if l and l[0].isdigit() and ". " in l]
            seen_batches.append(len(lines))
            return "\n".join("[%d] 译文%d" % (i + 1, i + 1) for i in range(len(lines)))

    items = [
        {"title": "Headline number %d" % i, "lang": "en", "title_zh": "", "title_en": ""}
        for i in range(20)
    ]
    generate.translate_titles(items, CountingAI(), batch_size=8)
    expect_eq("20 条被拆成 8+8+4 三批", seen_batches, [8, 8, 4])


# ---------------------------------------------------------------- 5. 分段翻译


def test_segment_split():
    group("分段翻译在句子边界切分（约 900 字符，800–950 窗口）")

    sentence = "这是用于测试分段的一句话，长度大约二十个字。"  # 24 字
    text = sentence * 100  # 2400 字
    segments = generate.split_text_segments(text, target=900, low=800, high=950)

    check("长文被切成多段", len(segments) >= 3, "实际=%d 段" % len(segments))
    check("每段都不空", all(s.strip() for s in segments))
    check("每段长度不超过 950",
          all(len(s) <= 950 for s in segments),
          "最长=%d" % max(len(s) for s in segments))
    check("每段都以句号结尾（在句子边界切开）",
          all(s.endswith("。") for s in segments),
          "段尾=%r" % [s[-1] for s in segments])
    check("拼回来等于原文",
          "".join(segments).replace("\n", "") == text.replace("\n", ""))

    # 短文本不分段
    expect_eq("短文本不分段", generate.split_text_segments(sentence), [sentence])
    expect_eq("空文本返回空列表", generate.split_text_segments(""), [])

    # 没有句号的超长文本也必须能切（退到空格/硬切）
    no_punct = "word " * 600
    segments = generate.split_text_segments(no_punct, target=900, low=800, high=950)
    check("无标点也能分段", len(segments) >= 2, "实际=%d 段" % len(segments))
    check("无标点分段后长度合规", all(len(s) <= 950 for s in segments))


# ---------------------------------------------------------------- 6. HTML 转义


def test_html_escape():
    group("HTML 转义（RSS 里的恶意标题不能破坏整页）")

    evil = '<script>alert("x")</script>'
    escaped = generate.esc(evil)
    check("<script> 被转义", "<script>" not in escaped, "实际=%r" % escaped)
    check("尖括号变成实体", "&lt;script&gt;" in escaped)

    # 内嵌 <script> 的 JSON 必须转义 </
    payload = {"title": "</script><script>alert(1)</script>"}
    dumped = generate.safe_json_for_script(payload)
    check("内嵌 JSON 里的 </script> 被转义",
          "</script>" not in dumped, "实际=%r" % dumped)
    check("内嵌 JSON 仍可被 json 解析",
          json.loads(dumped.replace("\\/", "/"))["title"].endswith("</script>"))

    expect_eq("单引号被转义", generate.esc("it's"), "it&#x27;s")
    expect_eq("None 变成空串", generate.esc(None), "")
    expect_eq("HTML 标签剥离", generate.strip_html("<p>正文<b>加粗</b></p>"), "正文 加粗")


# ---------------------------------------------------------------- 7. 图片


def test_image_normalize():
    group("图片 URL 归一（BBC 240→1024、去 resize、http→https）")

    bbc = generate.normalize_image_url(
        "http://ichef.bbci.co.uk/ace/standard/240/cpsprodpb/abc.jpg")
    check("BBC 240 升到 1024", "/1024/" in bbc and "/240/" not in bbc, "实际=%s" % bbc)
    check("http 升 https", bbc.startswith("https://"), "实际=%s" % bbc)

    aj = generate.normalize_image_url(
        "https://www.aljazeera.com/wp-content/uploads/a.jpg?resize=770%2C513")
    check("Al Jazeera 去掉 resize 参数", "resize" not in aj, "实际=%s" % aj)

    guardian = generate.normalize_image_url(
        "https://i.guim.co.uk/img/media/a.jpg?width=500&quality=85")
    check("Guardian width 改成 1200", "width=1200" in guardian, "实际=%s" % guardian)
    check("Guardian 保留其它参数", "quality=85" in guardian, "实际=%s" % guardian)

    expect_eq("协议相对链接补 https",
              generate.normalize_image_url("//cdn.news.example-site.com/a.jpg"),
              "https://cdn.news.example-site.com/a.jpg")
    expect_eq("空值返回空串", generate.normalize_image_url(""), "")
    expect_eq("非法值返回空串", generate.normalize_image_url("not-a-url"), "")

    # 从 description 的 <img> 里兜底提取
    found = generate.extract_image_from_html(
        '<p>图：<img src="http://ichef.bbci.co.uk/ace/standard/240/x.jpg"></p>')
    check("从 <img src> 兜底提取并升级分辨率",
          found.startswith("https://") and "/1024/" in found, "实际=%s" % found)


# ---------------------------------------------------------------- 8. URL


def test_url_validation():
    group("链接合法性校验与去重归一")

    check("正常链接合法", generate.is_valid_url("https://www.bbc.com/news/x"))
    check("空链接非法", not generate.is_valid_url(""))
    check("缺协议非法", not generate.is_valid_url("www.bbc.com/news"))
    check("占位域名非法", not generate.is_valid_url("https://example.com/x"))
    check("javascript 伪协议非法", not generate.is_valid_url("javascript:alert(1)"))
    check("ftp 协议非法", not generate.is_valid_url("ftp://x.com/a"))

    a = generate.normalize_url("https://www.bbc.com/news/x?utm_source=rss&id=1")
    check("utm 参数被去掉", "utm_source" not in a, "实际=%s" % a)
    check("有效参数被保留", "id=1" in a, "实际=%s" % a)

    # 只有追踪参数时，归一后应与「完全不带参数」的链接一致（否则同一篇文章会被算两次）
    b = generate.normalize_url("https://www.bbc.com/news/x?utm_source=rss")
    c = generate.normalize_url("https://www.bbc.com/news/x")
    expect_eq("去掉追踪参数后与无参数链接一致", b, c)

    # 域名大小写与 http→https 在归一里不强制改协议，但域名统一小写
    expect_eq("域名统一小写",
              generate.normalize_url("https://WWW.BBC.COM/news/x"),
              "https://www.bbc.com/news/x")


# ---------------------------------------------------------------- 9. 国家实体


def test_country_entities():
    group("国家实体校验（防编造 + 防错位）")

    found = generate.find_countries("南非同日发生两起大规模枪击，美国国务院表示关注")
    check("中文国家名被识别", "南非" in found and "美国" in found, "实际=%r" % (found,))

    found = generate.find_countries("South Africa shootings, US officials react")
    check("英文国家名+简称被识别", "南非" in found and "美国" in found, "实际=%r" % (found,))

    # 反例：小写 "us"（tell us）不能被当成美国
    found = generate.find_countries("Please tell us what happened in Peru")
    check("小写 us 不误判为美国", "美国" not in found, "实际=%r" % (found,))

    # 反例：Depardieu 里没有独立的 eu，所以不该命中欧盟
    found = generate.find_countries("Gerard Depardieu went on trial in Paris")
    check("Depardieu 不会误命中欧盟", "欧盟" not in found, "实际=%r" % (found,))
    check("Paris 命中法国", "法国" in found, "实际=%r" % (found,))

    # 正例：独立的 EU / U.S. 必须命中
    check("独立的 EU 命中欧盟",
          "欧盟" in generate.find_countries("The EU imposed new sanctions"))
    check("U.S. 命中美国",
          "美国" in generate.find_countries("The U.S. Treasury announced new rules"))

    # 编造检测：摘要提到两个原文里没有的国家 → 不通过
    cluster = {"items": [{
        "source": "BBC",
        "title_zh": "超级油轮在霍尔木兹海峡触雷后沉没",
        "title_en": "Supertanker sinks in Strait of Hormuz",
        "title": "Supertanker sinks in Strait of Hormuz",
        "summary": "",
        "url": "https://www.bbc.com/news/a",
    }]}
    ok, detail = generate.validate_event("沙特与以色列宣布关系正常化", cluster)
    check("编造的国家实体被判为不通过", ok is False, detail)

    # 真实对齐 → 通过
    cluster_ok = {"items": [{
        "source": "BBC",
        "title_zh": "南非同日发生两起大规模枪击致至少17人死亡",
        "title_en": "South Africa shootings kill 17",
        "title": "South Africa shootings kill 17",
        "summary": "",
        "url": "https://www.bbc.com/news/b",
    }]}
    ok, detail = generate.validate_event("南非枪击致17人死亡", cluster_ok)
    check("实体对得上判为通过", ok is True, detail)

    # 摘要不含国家实体 → 直接通过
    ok, _detail = generate.validate_event("一艘油轮在海上沉没", cluster_ok)
    check("不含国家实体直接通过", ok is True)


def test_event_redo_and_fallback():
    group("事件重做与条数兜底（AI 条数不足时不能空版块）")

    class PartialAI:
        """只返回 1 条事件，模拟 AI 偷懒。"""

        def __init__(self):
            self.calls = []

        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            self.calls.append(prompt[:40])
            if "大事" in prompt:
                first_cid = None
                for line in prompt.splitlines():
                    if line.startswith("[c"):
                        first_cid = line[1:line.index("]")]
                        break
                if first_cid:
                    return "[%s] 演练事件概括——演练重要性" % first_cid
                return ""
            return "[c1] 重做后的事件概括——重做重要性"

    def make_cluster(cid, title):
        return {
            "cid": cid,
            "section": "domestic",
            "source_count": 2,
            "image": "",
            "primary_title_zh": title,
            "published": datetime.now(BJ),
            "items": [{
                "source": "联合早报", "title_zh": title, "title": title,
                "title_en": "", "summary": "",
                "url": "https://www.zaobao.com.sg/news/%s" % cid,
                "image": "", "published": datetime.now(BJ), "lang": "zh",
            }],
        }

    clusters = [
        make_cluster("c1", "中国三季度经济增速超出市场预期"),
        make_cluster("c2", "国务院常务会议部署扩大内需措施"),
        make_cluster("c3", "全国多地迎来寒潮降温天气"),
    ]
    events, gate_log = generate.build_section_events(clusters, "国内", PartialAI())
    check("AI 只给 1 条时兜底补到 3 条", len(events) >= 3, "实际=%d 条" % len(events))
    check("兜底事件也被记录进闸门日志",
          any(row["结果"] == "兜底补齐" for row in gate_log))
    check("每条事件都带 cid", all(cid for cid, _ in events))


# ---------------------------------------------------------------- 10. 渲染


def make_day(tts_ok, image="https://ichef.bbci.co.uk/ace/standard/1024/x.jpg"):
    """造一天完整数据（覆盖多源簇、英文对照、无图条目）。"""
    return {
        "date": "2026-11-18",
        "broadcast": "各位好，今天是2026年11月18日，欢迎收听今日国际大事。",
        "tts_ok": tts_ok,
        "section_notes": {"domestic": "", "international": ""},
        "sections": {
            "domestic": [{
                "cid": "c1",
                "zh": "中国三季度经济增速超出市场预期",
                "importance": "显示经济韧性强",
                "source_count": 2,
                "image": "",
                "cluster": [
                    {"source": "联合早报", "title_zh": "中国三季度经济增速超出市场预期",
                     "title_en": "", "url": "https://www.zaobao.com.sg/news/a",
                     "summary_zh": "国家统计局公布数据。", "image": "", "is_primary": True},
                    {"source": "BBC中文", "title_zh": "中国经济数据好于预期",
                     "title_en": "", "url": "https://www.bbc.com/zhongwen/b",
                     "summary_zh": "分析认为四季度仍有压力。", "image": "", "is_primary": False},
                ],
            }],
            "international": [{
                "cid": "i1",
                "zh": "超级油轮在霍尔木兹海峡触雷后沉没",
                "importance": "或冲击国际油价",
                "source_count": 3,
                "image": image,
                "cluster": [
                    {"source": "BBC", "title_zh": "超级油轮在霍尔木兹海峡触雷后沉没",
                     "title_en": "Supertanker sinks after striking mine in Strait of Hormuz",
                     "url": "https://www.bbc.com/news/a",
                     "summary_zh": "油轮在凌晨沉没。",
                     "summary_en": "The tanker sank in the early hours.",
                     "image": image, "is_primary": True},
                    {"source": "Al Jazeera", "title_zh": "油轮触雷沉没引发油价上涨",
                     "title_en": "Oil prices rise after tanker sinks",
                     "url": "https://www.aljazeera.com/news/b",
                     "summary_zh": "分析人士预计运费走高。",
                     "summary_en": "Analysts expect freight rates to rise.",
                     "image": "", "is_primary": False},
                    {"source": "Sky News", "title_zh": "海湾油轮沉没事件调查启动",
                     "title_en": "Investigation launched into Gulf tanker sinking",
                     "url": "https://news.sky.com/story/c",
                     "summary_zh": "多国呼吁克制。",
                     "summary_en": "Several countries called for restraint.",
                     "image": "", "is_primary": False},
                ],
            }],
        },
        "raw_news": [{
            "en": "Supertanker sinks after striking mine",
            "zh": "超级油轮触雷沉没",
            "url": "https://www.bbc.com/news/a",
            "source": "BBC",
            "image": "",
            "section": "international",
        }],
    }


def test_render_with_audio():
    group("页面渲染：今日 + 有音频分支")
    html = generate.render_page(make_day(tts_ok=True), True, [], "https://daily.news.example-site.com")

    check("输出是完整 HTML",
          html.lstrip().startswith("<!DOCTYPE html>") and html.rstrip().endswith("</html>"))
    check("有音频时输出 <audio>",
          '<audio controls preload="none" src="audio/daily.mp3">' in html)
    check("有音频时不输出浏览器朗读按钮",
          "dailyNewsSpeak()" not in html)
    check("播报稿折叠区存在", "展开播报稿全文" in html)
    check("国内版块存在", "国内热点" in html)
    check("国际版块存在", "国际热点" in html)
    check("家数标注存在", "3 家媒体报道" in html, "应有「3 家媒体报道」标注")

    # 默认折叠：多观点面板必须带 hidden
    check("多观点面板默认为 hidden",
          'class="cluster" id="cluster-i1" hidden' in html,
          "未找到 hidden 的多观点面板")
    check("有「查看各家报道」按钮", "查看各家报道" in html)
    check("展开提示文案正确",
          "观点可能不同，请自行判断" in html)
    check("并列显示多家媒体来源",
          "BBC" in html and "Al Jazeera" in html and "Sky News" in html)
    check("英文原文带 lang=en 标记", 'lang="en"' in html)
    check("头条大图不加 lazy",
          'class="card-img"' in html and 'class="card-img" loading="lazy"' not in html)
    check("图片有 onerror 兜底", "onerror=" in html)
    check("响应式 viewport 存在", 'name="viewport"' in html)
    check("手机优先的 CSS 存在", "@media (min-width:600px)" in html)
    check("规范链接存在", 'rel="canonical"' in html)
    check("原始新闻列表存在", "原始新闻列表" in html)


def test_render_without_audio():
    group("页面渲染：今日 + 无音频降级分支")

    html = generate.render_page(make_day(tts_ok=False), True, [], "https://daily.news.example-site.com")
    check("无音频时不输出 <audio>", "<audio" not in html)
    check("无音频时输出朗读按钮", "dailyNewsSpeak()" in html)
    check("朗读按钮文案存在", "朗读播报" in html)
    check("脚本里有 SpeechSynthesis 逻辑", "speechSynthesis" in html)
    check("朗读语言标记为 zh-CN", "zh-CN" in html)
    check("监听 onvoiceschanged", "onvoiceschanged" in html)
    check("播报稿仍然可见（可折叠展开）", "展开播报稿全文" in html)


def test_render_archive():
    group("页面渲染：归档页（不放播放器，注明音频仅当日有效）")

    html = generate.render_page(make_day(tts_ok=True), False, None, "https://daily.news.example-site.com")
    check("归档页不放 <audio>", "<audio" not in html)
    check("归档页有 audio 相关提示", "音频仅当日有效" in html)
    check("归档页不放朗读按钮", "dailyNewsSpeak()" not in html)
    check("归档页有返回首页链接", 'href="../index.html"' in html, "缺少返回首页链接")
    check("归档页有归档 canonical",
          "/archive/2026-11-18.html" in html)
    check("归档页不放最近7天列表", "最近 7 天" not in html)


def test_render_empty():
    group("页面渲染：零数据时给中文友好提示，不显示空白页")

    empty_day = {
        "date": "2026-11-18",
        "broadcast": "",
        "tts_ok": False,
        "sections": {"domestic": [], "international": []},
        "section_notes": {"domestic": "", "international": ""},
        "raw_news": [],
    }
    html = generate.render_page(empty_day, True, [], "")
    check("空数据也有友好中文提示",
          "暂时没有抓到有效新闻" in html, "缺少友好提示")
    check("空数据不是空白页（有完整结构）", "</html>" in html)
    check("空数据不报错（函数正常返回）", isinstance(html, str) and len(html) > 500)


def test_render_with_recent_days():
    group("首页最近 N 天列表")

    recent = [
        {"date": "2026-11-17", "sections": {"domestic": [{"zh": "昨天的国内要闻"}],
                                            "international": [{"zh": "昨天的国际要闻"}]}},
        {"date": "2026-11-16", "sections": {"domestic": [], "international": []}},
    ]
    html = generate.render_page(make_day(True), True, recent, "")
    check("近 7 天列表存在", "最近 2 天" in html)
    check("归档链接指向 archive/",
          'href="archive/2026-11-17.html"' in html)
    check("无新闻的那天有占位文案", "当日无有效新闻" in html)


# ---------------------------------------------------------------- 11. 音频与清理


def test_mp3_header():
    group("MP3 文件头校验（按大小猜会把 JSON 当音频写进去）")

    with tempfile.TemporaryDirectory() as tmp:
        id3_path = os.path.join(tmp, "id3.mp3")
        with open(id3_path, "wb") as handle:
            handle.write(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100)
        check("ID3 头被认作合法 MP3", generate.is_valid_mp3(id3_path))

        frame_path = os.path.join(tmp, "frame.mp3")
        with open(frame_path, "wb") as handle:
            handle.write(b"\xFF\xFB\x90\x00" + b"\x00" * 100)
        check("0xFF 帧同步头被认作合法 MP3", generate.is_valid_mp3(frame_path))

        json_path = os.path.join(tmp, "fake.mp3")
        with open(json_path, "wb") as handle:
            handle.write(b'{"result": {"audio": "base64..."}}' * 2000)
        check("5MB 的 JSON 不被当成音频", not generate.is_valid_mp3(json_path))

        empty_path = os.path.join(tmp, "empty.mp3")
        open(empty_path, "wb").close()
        check("空文件不合法", not generate.is_valid_mp3(empty_path))
        check("不存在的文件不合法", not generate.is_valid_mp3(os.path.join(tmp, "nope.mp3")))


def test_prune():
    group("30 天归档清理")

    today = generate.today_bj()
    days = []
    for offset in (0, 1, 7, 29, 30, 31, 60):
        date = today - timedelta(days=offset)
        days.append({"date": date.strftime("%Y-%m-%d"), "sections": {}})

    kept = generate.prune_days(days, today, keep_days=30)
    kept_dates = {d["date"] for d in kept}

    check("今天被保留", today.strftime("%Y-%m-%d") in kept_dates)
    check("29 天前被保留",
          (today - timedelta(days=29)).strftime("%Y-%m-%d") in kept_dates)
    check("30 天前被清理",
          (today - timedelta(days=30)).strftime("%Y-%m-%d") not in kept_dates)
    check("31 天前被清理",
          (today - timedelta(days=31)).strftime("%Y-%m-%d") not in kept_dates)
    check("60 天前被清理",
          (today - timedelta(days=60)).strftime("%Y-%m-%d") not in kept_dates)
    check("结果按日期倒序",
          [d["date"] for d in kept] == sorted((d["date"] for d in kept), reverse=True))

    # 归档文件清理
    with tempfile.TemporaryDirectory() as tmp:
        keep_dates = [today.strftime("%Y-%m-%d")]
        for name in (today.strftime("%Y-%m-%d") + ".html",
                     (today - timedelta(days=31)).strftime("%Y-%m-%d") + ".html",
                     "not-a-date.html"):
            open(os.path.join(tmp, name), "w").close()
        removed = generate.prune_archive_files(tmp, keep_dates)
        remaining = sorted(os.listdir(tmp))
        check("过期归档被删除",
              (today - timedelta(days=31)).strftime("%Y-%m-%d") + ".html" not in remaining,
              "剩余=%r" % (remaining,))
        check("当日归档被保留",
              today.strftime("%Y-%m-%d") + ".html" in remaining)
        check("非日期文件名不被误删", "not-a-date.html" in remaining)
        expect_eq("删除数量正确", len(removed), 1)

    # 音频只留最新一天
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("daily.mp3", "2026-11-17.mp3", "old.mp3"):
            open(os.path.join(tmp, name), "w").close()
        generate.prune_audio(tmp)
        expect_eq("音频只保留 daily.mp3", sorted(os.listdir(tmp)), ["daily.mp3"])


# ---------------------------------------------------------------- 12. 发布闸门


def test_publish_gate():
    group("发布闸门（不通过就不写任何文件）")

    day = make_day(tts_ok=False)
    with tempfile.TemporaryDirectory() as tmp:
        passed, checks, problems = generate.publish_gate(
            day, {"domestic": {"可用条目数": 10}, "international": {"可用条目数": 10},
                  "音频路径": os.path.join(tmp, "none.mp3")},
        )
        # 每版块只有 1 条大事 → 应判不通过
        check("条数不足判不通过", passed is False, "问题=%r" % (problems,))
        check("检查项被完整列出", len(checks) >= 4)

    # 补齐到 3 条 → 通过
    full = make_day(tts_ok=False)
    for key in ("domestic", "international"):
        template = full["sections"][key][0]
        for index in range(2, 4):
            clone = json.loads(json.dumps(template))
            clone["cid"] = "%s%d" % (key[0], index)
            full["sections"][key].append(clone)
    passed, _checks, problems = generate.publish_gate(
        full, {"domestic": {"可用条目数": 10}, "international": {"可用条目数": 10},
               "音频路径": "/nonexistent.mp3"},
    )
    check("条数与中文占比达标则通过", passed is True, "问题=%r" % (problems,))

    # 声明 tts_ok 但没有合法音频 → 不通过
    declared = json.loads(json.dumps(full))
    declared["tts_ok"] = True
    passed, _checks, problems = generate.publish_gate(
        declared, {"domestic": {"可用条目数": 10}, "international": {"可用条目数": 10},
                   "音频路径": "/nonexistent.mp3"},
    )
    check("声明有音频但文件非法时判不通过", passed is False, "问题=%r" % (problems,))

    # 版块素材本来就不足（可用条目数<3）→ 放行，避免小新闻日永久停更
    tiny = json.loads(json.dumps(day))
    passed, _checks, _problems = generate.publish_gate(
        tiny, {"domestic": {"可用条目数": 1}, "international": {"可用条目数": 2},
               "音频路径": "/nonexistent.mp3"},
    )
    check("素材本身不足 3 条时放行（不当永久停更）", passed is True)


# ---------------------------------------------------------------- 13. 规范化


def test_normalize_items():
    group("规范化：时间过滤 / 去重 / 软新闻 / 语言判定")

    now = datetime.now(BJ)

    def item(title, url, hours_ago, source="BBC", image=""):
        return {
            "source": source, "section": "international", "title": title,
            "summary": "", "url": url, "image": image,
            "published": now - timedelta(hours=hours_ago), "lang": "en",
        }

    items = [
        item("Recent world news", "https://www.bbc.com/news/1", 2),
        item("Old world news", "https://www.bbc.com/news/2", 48),          # 超窗
        item("Recent world news", "https://www.bbc.com/news/1", 3),        # URL 重复
        item("Football star wins award", "https://www.bbc.com/news/3", 2),  # 软新闻
        item("中文新闻标题", "https://www.zaobao.com.sg/news/4", 2),        # 中文源
    ]
    kept, stats = generate.normalize_items(items, now=now)

    expect_eq("超时间窗被过滤", stats["超出时间窗"], 1)
    expect_eq("软新闻被过滤", stats["软新闻"], 1)
    expect_eq("URL 重复被去掉", stats["链接重复"], 1)
    expect_eq("最终保留 2 条", stats["保留条数"], 2)
    check("中文源被标记为 zh",
          any(it["lang"] == "zh" for it in kept), "实际=%r" % [it["lang"] for it in kept])
    check("结果按时间倒序",
          kept[0]["published"] >= kept[1]["published"])

    # 无时间条目：保留而不是丢弃（很多源不给 pubDate）
    kept2, stats2 = generate.normalize_items(
        [{"source": "X", "section": "international", "title": "No date news",
          "summary": "", "url": "https://www.bbc.com/news/9", "image": "",
          "published": None, "lang": "en"}],
        now=now,
    )
    expect_eq("无时间条数被统计", stats2["无时间(保留)"], 1)
    expect_eq("无时间条目被保留", len(kept2), 1)


def test_china_related_filter():
    group("国内源全挂时的退化方案：从国际源筛涉华报道")

    items = [
        {"source": "BBC", "section": "international", "title": "China economy grows faster than expected",
         "summary": "", "url": "https://www.bbc.com/news/a", "image": "",
         "published": datetime.now(BJ), "lang": "en"},
        {"source": "BBC", "section": "international", "title": "Peru election results announced",
         "summary": "", "url": "https://www.bbc.com/news/b", "image": "",
         "published": datetime.now(BJ), "lang": "en"},
    ]
    filtered = generate.filter_china_related(items)
    expect_eq("只留下涉华报道", len(filtered), 1)
    expect_eq("涉华报道被归到 domestic 版块", filtered[0]["section"], "domestic")


def test_source_status_collection():
    group("采集：单源失败不阻断其它源")

    import feedparser  # noqa: F401 确认依赖可用

    def fake_http(url, timeout=20):
        if "bbci" in url:
            raise TimeoutError("演练超时")
        if "theguardian" in url:
            # 0 条的坏源（空 channel）
            return "<rss><channel><title>坏源</title></channel></rss>".encode("utf-8")
        return (
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<title>好源</title><link>https://news.example-site.com/</link>"
            "<item><title>Good news headline</title>"
            "<link>https://www.npr.org/2026/11/17/good</link>"
            "<description>Something happened.</description>"
            "<pubDate>Mon, 17 Nov 2026 06:00:00 +0000</pubDate></item>"
            "</channel></rss>"
        ).encode("utf-8")

    sources = [
        {"name": "BBC", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
        {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss"},
        {"name": "NPR", "url": "https://feeds.npr.org/1004/rss.xml"},
    ]
    items, statuses = generate.collect_all(sources, "international", http_get=fake_http)
    expect_eq("超时的源被跳过且记录", len(statuses), 3)
    check("超时源状态标记为失败",
          any(s["源"] == "BBC" and s["状态"] == "失败" for s in statuses))
    check("0 条源状态标记为失败",
          any(s["源"] == "The Guardian" and s["状态"] == "失败" for s in statuses))
    check("正常源成功抓取",
          any(s["源"] == "NPR" and s["状态"] == "正常" for s in statuses))
    expect_eq("只有正常源贡献条目", len(items), 1)
    expect_eq("条目来源正确", items[0]["source"], "NPR")
    check("条目时间被解析",
          items[0]["published"] is not None)


# ---------------------------------------------------------------- 14. 提示词


def test_prompts():
    group("提示词（措辞实测有效，只把编号换成稳定 cid）")

    check("标题翻译提示词含 [编号] 格式约定",
          "[编号] 中文译文" in generate.TITLE_TRANSLATE_PROMPT)
    check("标题翻译提示词禁止开场白",
          "不要输出任何开场白" in generate.TITLE_TRANSLATE_PROMPT)
    check("大事总结提示词用稳定 cid",
          "c1、c7" in generate.EVENTS_SUMMARY_PROMPT)
    check("大事总结提示词限 5 到 8 条",
          "5 到 8 条" in generate.EVENTS_SUMMARY_PROMPT)
    check("大事总结提示词要求忠实原文",
          "禁止编造" in generate.EVENTS_SUMMARY_PROMPT)
    check("全文翻译提示词要求完整翻译",
          "完整翻译，不省略" in generate.FULLTEXT_TRANSLATE_PROMPT)
    check("播报稿提示词的字数上限由常量统一控制",
          "{max_chars} 字以内" in generate.BROADCAST_PROMPT,
          "提示词没有使用 max_chars 占位符")
    check("播报稿提示词给了固定开头",
          "欢迎收听今日国际大事" in generate.BROADCAST_PROMPT)
    check("系统提示词禁止思考过程",
          "禁止输出思考过程" in ai_client.SYSTEM_PROMPT)

    # 模板占位符必须都能被 format 填上（防 KeyError）
    try:
        generate.TITLE_TRANSLATE_PROMPT.format(count=1, items="1. a")
        generate.EVENTS_SUMMARY_PROMPT.format(scope="国际", count=1, items="[c1] a")
        generate.FULLTEXT_TRANSLATE_PROMPT.format(title="t", body="b")
        generate.BROADCAST_PROMPT.format(
            date="2026年11月18日", max_chars=900, point_count=3, points="1. a")
        generate.EVENT_REDO_PROMPT.format(cid="c1", items="- a")
        check("所有提示词模板都能安全 format", True)
    except KeyError as exc:
        check("所有提示词模板都能安全 format", False, "缺少占位符：%s" % exc)


# ---------------------------------------------------------------- 15. 配置


def test_config_integrity():
    group("配置完整性（源列表、常量、依赖）")

    check("国际源数量为 7", len(generate.INTL_FEEDS) == 7, "实际=%d" % len(generate.INTL_FEEDS))
    names = {s["name"] for s in generate.INTL_FEEDS}
    for required in ("BBC", "The Guardian", "Al Jazeera", "France 24", "NPR",
                     "Sky News", "Google News"):
        check("国际源含 %s" % required, required in names)
    for forbidden in ("CBC", "NHK", "UN News"):
        check("国际源不含 %s（实测不可用）" % forbidden, forbidden not in names)

    check("国内源是配置项且非空", isinstance(generate.DOMESTIC_FEEDS, list)
          and len(generate.DOMESTIC_FEEDS) >= 1)
    check("国内源每项都有 name 与 url",
          all(s.get("name") and s.get("url") for s in generate.DOMESTIC_FEEDS))

    expect_eq("时效窗口 36 小时", generate.WINDOW_HOURS, 36)
    expect_eq("归档保留 30 天", generate.KEEP_DAYS, 30)
    expect_eq("分批翻译 8 条", generate.TRANSLATE_BATCH, 8)
    expect_eq("聚类阈值 0.25（实测标定值）", generate.CLUSTER_THRESHOLD, 0.25)
    expect_eq("单源超时 20 秒", generate.SOURCE_TIMEOUT, 20)
    expect_eq("TTS 音色", generate.TTS_VOICE, "zh-CN-XiaoxiaoNeural")
    check("模型为 gemma-4-26b-a4b-it",
          "gemma-4-26b-a4b-it" in ai_client.DEFAULT_MODEL, ai_client.DEFAULT_MODEL)
    check("cluget 聚合器名单含 Google News",
          "Google News" in generate.AGGREGATOR_SOURCES)
    check("国家实体表条目数在 30–80 之间",
          30 <= len(generate.COUNTRY_ALIASES) <= 80,
          "实际=%d" % len(generate.COUNTRY_ALIASES))

    # requirements.txt 必须存在且用范围约束（精确版本号会失效）
    req_path = os.path.join(ROOT, "requirements.txt")
    check("requirements.txt 存在", os.path.exists(req_path))
    if os.path.exists(req_path):
        with open(req_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        for package in ("edge-tts", "feedparser", "beautifulsoup4"):
            check("依赖包含 %s" % package, package in content)
        check("依赖使用范围约束（含 >= 或 <）", ">=" in content and "<" in content)
        check("依赖里没有精确版本锁定（==）", "==" not in content)


# ---------------------------------------------------------------- 16. 端到端 dry-run


def test_stale_source_guard():
    group("停更源守卫（HTTP 200 + bozo=False 但内容停在几年前的僵尸源）")

    now = datetime.now(BJ)

    def item(hours_ago=None):
        base = {
            "source": "人民网", "section": "domestic", "title": "某条新闻",
            "summary": "", "url": "https://www.people.com.cn/rss/x",
            "image": "", "published": None,
        }
        if hours_ago is not None:
            base["published"] = now - timedelta(hours=hours_ago)
        return base

    check("全新的源不算停更",
          generate.is_stale_source([item(2), item(5)], now=now) is False)
    check("最新条目是 531 天前 → 判定停更",
          generate.is_stale_source([item(24 * 531)], now=now) is True)
    check("最新条目 8 天前 → 判定停更（超过 7 天阈值）",
          generate.is_stale_source([item(24 * 8)], now=now) is True)
    check("最新条目 3 天前 → 不算停更",
          generate.is_stale_source([item(24 * 3)], now=now) is False)
    check("完全没有时间字段的源判定为停更（无法验证时效）",
          generate.is_stale_source([item(None)], now=now) is True)
    check("空列表不算停更（那是抓取失败的范畴）",
          generate.is_stale_source([], now=now) is False)

    # 集成：collect_all 必须把停更源整源丢弃，且不影响其它源
    def fake_http(url, timeout=20):
        if "people" in url:
            # 僵尸源：HTTP 200、bozo=False、有条目，但时间停在两年前
            old = (now - timedelta(days=730)).astimezone(timezone.utc)
            return (
                '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
                "<title>僵尸源</title><link>https://news.example-site.com/</link>"
                "<item><title>两年前的旧闻标题</title>"
                "<link>https://www.people.com.cn/old</link>"
                "<description>旧内容</description>"
                "<pubDate>%s</pubDate></item>"
                "</channel></rss>" % old.strftime("%a, %d %b %Y %H:%M:%S +0000")
            ).encode("utf-8")
        fresh = (now - timedelta(hours=2)).astimezone(timezone.utc)
        return (
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<title>新源</title><link>https://news.example-site.com/</link>"
            "<item><title>刚发生的新鲜新闻</title>"
            "<link>https://www.chinanews.com.cn/fresh</link>"
            "<description>新内容</description>"
            "<pubDate>%s</pubDate></item>"
            "</channel></rss>" % fresh.strftime("%a, %d %b %Y %H:%M:%S +0000")
        ).encode("utf-8")

    sources = [
        {"name": "人民网(僵尸)", "url": "https://www.people.com.cn/rss/politics.xml"},
        {"name": "中新网", "url": "https://www.chinanews.com.cn/rss/scroll-news.xml"},
    ]
    items, statuses = generate.collect_all(sources, "domestic", http_get=fake_http)

    check("停更源被标记为「停更源」",
          any(s["源"] == "人民网(僵尸)" and s["状态"] == "停更源" for s in statuses),
          "实际=%r" % [(s["源"], s["状态"]) for s in statuses])
    check("停更源的条目没有混进来",
          all("旧闻" not in it["title"] for it in items),
          "实际标题=%r" % [it["title"] for it in items])
    check("正常源不受影响", any(it["title"] == "刚发生的新鲜新闻" for it in items))


def test_collapse_repeated_prefix():
    group("摘要重复首句折叠（中新网 content/description 拼接残留）")

    dup = (
        "中新社北京11月17日电 国务院常务会议部署扩大内需措施。"
        "中新社北京11月17日电 国务院常务会议部署扩大内需措施，提出一系列政策。"
    )
    result = generate.collapse_repeated_prefix(dup)
    check("重复的开头被截断", result.count("中新社北京11月17日电") == 1,
          "实际=%r" % result)
    check("截断后内容仍然完整可读",
          result.startswith("中新社北京11月17日电") and len(result) > 20,
          "实际=%r" % result)

    normal = "这是一条没有重复开头的正常摘要，内容清晰完整，不需要任何处理。"
    expect_eq("不重复的摘要原样返回", generate.collapse_repeated_prefix(normal), normal)
    expect_eq("空摘要返回空串", generate.collapse_repeated_prefix(""), "")


def test_split_summary_importance():
    group("「概括——重要性」拆分（各种破折号写法与残留清理）")

    cases = [
        ("超级油轮沉没——或冲击国际油价", "超级油轮沉没", "或冲击国际油价"),
        ("超级油轮沉没 —— 或冲击国际油价", "超级油轮沉没", "或冲击国际油价"),
        ("超级油轮沉没—或冲击国际油价", "超级油轮沉没", "或冲击国际油价"),
        ("超级油轮沉没--或冲击国际油价", "超级油轮沉没", "或冲击国际油价"),
        ("超级油轮沉没", "超级油轮沉没", ""),
    ]
    for raw, expect_summary, expect_importance in cases:
        summary, importance = generate.split_summary_importance(raw)
        expect_eq("拆分 %r 的概括" % raw[:12], summary, expect_summary)
        expect_eq("拆分 %r 的重要性" % raw[:12], importance, expect_importance)

    # 关键回归：不能留下「或冲击——影响值得关注」这种双破折号
    summary, importance = generate.split_summary_importance(
        "一艘载油200万桶的超级油轮在霍尔木兹海峡触雷沉没——或冲击——影响值得关注"
    )
    check("拆分后概括里没有残留破折号", "——" not in summary and "—" not in summary,
          "实际=%r" % summary)
    check("拆分后重要性里没有残留破折号",
          "——" not in importance and "—" not in importance,
          "实际=%r" % importance)
    expect_eq("多段式拆分取第一段做概括", summary,
              "一艘载油200万桶的超级油轮在霍尔木兹海峡触雷沉没")
    expect_eq("多段式拆分取最后一段做重要性", importance, "影响值得关注")


def test_media_title_not_duplicated():
    group("展开区不重复显示卡片标题（否则「看各家报道」等于没新信息）")

    # 场景一：媒体标题与事件概括完全一样 → 展开区不该再渲染一遍同样的 <h4>
    dup_day = make_day(tts_ok=False)
    event = dup_day["sections"]["international"][0]
    event_summary = event["zh"]
    event["cluster"][0]["title_zh"] = event_summary  # 头条标题 == 事件概括
    html = generate.render_page(dup_day, True, [], "")

    duplicated_h4 = ('<h4><a href="https://www.bbc.com/news/a" target="_blank" '
                     'rel="noopener noreferrer">%s</a></h4>' % generate.esc(event_summary))
    check("与概括相同的媒体标题不再重复渲染 h4",
          duplicated_h4 not in html, "展开区仍重复渲染了相同标题")
    check("不显示重复标题时，原文链接仍然保留",
          'href="https://www.bbc.com/news/a"' in html, "链接丢失")

    # 场景二：措辞不同的媒体标题必须保留——那才是「不同观点」的价值所在
    diff_day = make_day(tts_ok=False)
    diff_day["sections"]["international"][0]["cluster"][1]["title_zh"] = "油轮触雷沉没引发油价上涨"
    html2 = generate.render_page(diff_day, True, [], "")
    check("措辞不同的媒体标题仍然展示",
          "油轮触雷沉没引发油价上涨" in html2, "不同措辞的标题被误删了")
    check("不同措辞标题保留了原文链接",
          'href="https://www.aljazeera.com/news/b"' in html2)

    # 场景三：同一句英文摘要不该既标 lang=en 又用中文，反之亦然
    check("中文摘要不会被误标为 lang=en",
          'lang="en">国家统计局' not in html2,
          "中文摘要被标成了英文，会误导浏览器翻译兜底")
    check("英文摘要正确标注 lang=en",
          'class="sum-en" lang="en"' in html2 or 'class="en-title" lang="en"' in html2)


def test_chinese_source_not_translated():
    group("中文源不做「中译中」（浪费额度且产出诡异结果）")

    class CountingAI:
        def __init__(self):
            self.prompts = []

        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            self.prompts.append(prompt)
            return "不该被调用的译文"

    ai = CountingAI()
    cluster = {
        "cid": "c1",
        "source_count": 1,
        "items": [{
            "source": "中新网", "lang": "zh",
            "title": "中国三季度经济增速超出市场预期",
            "title_zh": "中国三季度经济增速超出市场预期",
            "title_en": "",
            "summary": "国家统计局公布数据。",
            "url": "https://www.chinanews.com.cn/a",
            "image": "", "published": datetime.now(BJ),
        }],
    }

    # 每段必须 >= 40 字，否则会被正文提取器当成噪声丢掉
    paragraph = (
        "国家统计局今日公布最新数据，三季度国内生产总值同比增长超出市场预期，"
        "工业增加值与社会消费品零售总额均实现稳步回升，就业形势总体保持稳定。"
    )

    def fake_http(url, timeout=20):
        return ("<html><body><article>" +
                "".join("<p>%s</p>" % paragraph for _ in range(6)) +
                "</article></body></html>").encode("utf-8")

    done = generate.deep_dive([cluster], ai, limit=1, http_get=fake_http)
    expect_eq("中文源没有调用 AI 翻译", len(ai.prompts), 0)
    expect_eq("中文源仍抓到了正文", done, 1)
    check("中文源摘要直接使用正文原文",
          "国家统计局今日公布最新数据" in (cluster["items"][0].get("summary_zh") or ""),
          "实际=%r" % (cluster["items"][0].get("summary_zh") or "")[:60])


def test_publisher_voting():
    group("按「媒体」计票而不是按「源」计票（同媒体多频道不能互相充数）")

    now = datetime.now(BJ)

    def item(source, publisher, title, suffix):
        return {
            "source": source, "publisher": publisher, "section": "domestic",
            "title": title, "title_zh": title, "title_en": "", "summary": "",
            "url": "https://www.chinanews.com.cn/%s" % suffix, "image": "",
            "published": now, "lang": "zh",
        }

    # 真实场景：中新网 5 个频道报道同一件事。
    # 它们在聚类阶段就各自成簇（同媒体绝不合并，这是刻意的），
    # 关键是每一簇的「家数」都必须是 1，绝不能因为频道多就变成「多家媒体报道」。
    same_media = [
        item("中新网·即时", "中新网", "习近平会见越共中央总书记苏林", "a"),
        item("中新网·时政", "中新网", "习近平会见越共中央总书记苏林", "b"),
        item("中新网·要闻", "中新网", "习近平会见越共中央总书记苏林", "c"),
    ]
    clusters = generate.cluster_items(same_media)
    expect_eq("同一媒体的多个频道各自成簇（不互相合并）", len(clusters), 3)
    check("同一媒体的每一簇家数都是 1（不互相充数）",
          all(generate.vote_count(c["items"]) == 1 for c in clusters),
          "实际=%r" % [generate.vote_count(c["items"]) for c in clusters])

    # 真正的两家媒体 → 合并成 1 簇且计 2 家
    two_media = [
        item("中新网·即时", "中新网", "习近平会见越共中央总书记苏林", "a"),
        item("联合早报", "联合早报", "习近平会见越共中央总书记苏林", "b"),
    ]
    clusters = generate.cluster_items(two_media)
    expect_eq("不同媒体的同一事件合并成 1 簇", len(clusters), 1)
    expect_eq("不同媒体正确计为 2 家",
              generate.vote_count(clusters[0]["items"]), 2)

    # 同一媒体同期有多个频道 + 另一家媒体 → 计 2 家（不是 3 家）
    mixed = [
        item("中新网·即时", "中新网", "习近平会见越共中央总书记苏林", "a"),
        item("中新网·要闻", "中新网", "习近平会见越共中央总书记苏林", "b"),
        item("联合早报", "联合早报", "习近平会见越共中央总书记苏林", "c"),
    ]
    clusters = generate.cluster_items(mixed)
    total_votes = generate.vote_count(clusters[0]["items"])
    check("同媒体多频道 + 另一家媒体计为 2 家而不是 3 家",
          total_votes == 2, "实际=%d 家" % total_votes)

    # 没有 publisher 字段时回退到 source（向后兼容旧数据）
    legacy = [{
        "source": "BBC", "section": "international", "title": "x", "title_zh": "x",
        "title_en": "", "summary": "", "url": "https://www.bbc.com/a",
        "image": "", "published": now, "lang": "zh",
    }]
    expect_eq("缺 publisher 时回退用 source 计票",
              generate.vote_count(legacy), 1)
    expect_eq("item_publisher 缺字段时回退 source",
              generate.item_publisher(legacy[0]), "BBC")

    # 关键回归：DOMESTIC_FEEDS 里同属中新网的频道必须都标 publisher=中新网
    cn_sources = [s for s in generate.DOMESTIC_FEEDS if s["name"].startswith("中新网")]
    check("中新网各频道都声明了 publisher",
          all(s.get("publisher") == "中新网" for s in cn_sources),
          "实际=%r" % [(s["name"], s.get("publisher")) for s in cn_sources])
    check("国内源都声明了 publisher 字段",
          all(s.get("publisher") for s in generate.DOMESTIC_FEEDS),
          "有源没声明 publisher：%r" % [
              s["name"] for s in generate.DOMESTIC_FEEDS if not s.get("publisher")])


def test_entity_conflict_guard():
    group("实体冲突守卫（降阈值后必须配套，防止同框架不同国家被误合并）")

    # 典型陷阱：同一个「习近平会见X国总理」框架，但是两件不同的事
    a = "习近平会见加拿大总理卡尼"
    b = "习近平会见巴布亚新几内亚总理马拉佩"
    check("同框架不同国家判定为实体冲突",
          generate.entity_conflict(a, b) is True, "未检出冲突")
    check("陷阱对相似度高到会被阈值误合并（所以必须靠实体守卫兜住）",
          generate.text_similarity(a, b) >= 0.20,
          "相似度=%.3f" % generate.text_similarity(a, b))

    # 同一国家的同一事件：不算冲突
    c = "南非两起大规模枪击事件导致至少17人死亡"
    d = "南非同日发生两起大规模枪击致至少17人死亡"
    check("同一国家不算实体冲突", generate.entity_conflict(c, d) is False)

    # 一边没有国家实体：无法判断，按不冲突处理（否则会漏并真事件）
    check("一方无国家实体时不判冲突",
          generate.entity_conflict("特朗普前往亚洲并计划与中俄进行核对话",
                                   "特朗普出发前往东盟参加峰会") is False)
    check("两边都无国家实体时不判冲突",
          generate.entity_conflict("联合国警告苏丹面临饥荒风险", "苏丹数百万人面临饥荒") is False)

    # 集成验证：陷阱对在 cluster_items 里绝不能被合并
    now = datetime.now(BJ)

    def item(source, publisher, title, suffix):
        return {
            "source": source, "publisher": publisher, "section": "domestic",
            "title": title, "title_zh": title, "title_en": "", "summary": "",
            "url": "https://www.chinanews.com.cn/%s" % suffix, "image": "",
            "published": now, "lang": "zh",
        }

    trap = [
        item("中新网·时政", "中新网", a, "a"),
        item("联合早报", "联合早报", b, "b"),
    ]
    clusters = generate.cluster_items(trap)
    expect_eq("实体冲突的两条绝不合并", len(clusters), 2)
    check("两条各自独立成簇且家数都是 1",
          all(generate.vote_count(c["items"]) == 1 for c in clusters))


def test_cluster_threshold_calibration():
    group("聚类阈值标定（0.25 是根据真实标题对扫描出来的，不是拍脑袋）")

    # 真实抓取数据里「明显同事件、不同措辞」的标题对
    same_event_pairs = [
        ("南非两起大规模枪击事件导致至少17人死亡",
         "南非同日发生两起大规模枪击致至少17人死亡"),
        ("五个欧盟国家计划在卢旺达建立移民“返还中心”",
         "多个欧盟国家拟在卢旺达设立遣返中心"),
        ("联合国警告苏丹面临饥荒风险",
         "苏丹数百万人面临严重饥荒"),
    ]
    # 真实数据里「明显不同事件」的标题对
    different_pairs = [
        ("菲律宾数百人在反特朗普抗议活动中被捕",
         "联合国会议寻求遏制“杀人机器人”等人工智能武器"),
        ("我国快递业务量连续两个月超170亿件",
         "中东部将迎来大范围雨雪及寒潮天气"),
        ("印尼卡伊群岛发生6.6级地震",
         "10月中国经济生产消费就业底盘稳健"),
    ]

    th = generate.CLUSTER_THRESHOLD
    check("阈值为 0.25", th == 0.25, "实际=%.2f" % th)
    check("阈值不再是最初的 0.35（实测太保守，多源簇几乎聚不出来）",
          th < 0.35)

    merged_same = sum(1 for a, b in same_event_pairs
                      if generate.text_similarity(a, b) >= th)
    merged_diff = sum(1 for a, b in different_pairs
                      if generate.text_similarity(a, b) >= th)

    check("同事件至少能合并 2/3（0.35 时只有 1/3）",
          merged_same >= 2, "实际合并 %d/3" % merged_same)
    expect_eq("不同事件零误并", merged_diff, 0)


def test_tts_retry_behaviour():
    group("TTS 重试（实测失败是服务端随机抖动，必须给足重试次数）")

    check("重试次数不少于 8 次",
          generate.TTS_ATTEMPTS >= 8, "实际=%d" % generate.TTS_ATTEMPTS)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.mp3")
        sleeps = []

        # 前 3 次抛异常，第 4 次成功 → 必须成功且退出
        state = {"n": 0}
        original_save = generate._tts_save

        async def flaky_save(text, p, voice=None, rate=None):
            state["n"] += 1
            if state["n"] < 4:
                raise RuntimeError("演练：NoAudioReceived")
            with open(p, "wb") as handle:
                handle.write(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 200)

        generate._tts_save = flaky_save
        try:
            ok = generate.generate_tts("各位好，这是一段足够长的测试播报稿内容，用于验证语音合成的重试逻辑。", path,
                                       sleep=sleeps.append)
            check("前几次失败后仍能合成成功", ok is True)
            expect_eq("总共尝试 4 次", state["n"], 4)
            check("失败之间有退避等待", len(sleeps) >= 3, "实际=%d 次等待" % len(sleeps))
            check("退避时间是递增的",
                  all(sleeps[i] <= sleeps[i + 1] for i in range(len(sleeps) - 1)),
                  "实际=%r" % (sleeps,))
        finally:
            generate._tts_save = original_save

        # 全部失败 → 返回 False（上层降级为浏览器朗读），且不留残文件
        async def always_fail(text, p, voice=None, rate=None):
            raise RuntimeError("演练：403 Invalid response status")

        original_save = generate._tts_save
        generate._tts_save = always_fail
        try:
            ok = generate.generate_tts("各位好，这是一段足够长的测试播报稿内容，用于验证语音合成的重试逻辑。", path,
                                       sleep=lambda _s: None)
            check("全部失败时返回 False（触发降级而非崩溃）", ok is False)
            check("失败后不留残缺音频文件", not os.path.exists(path))
        finally:
            generate._tts_save = original_save

        # 拿到 0 字节 / 非法文件也必须判为失败
        async def empty_save(text, p, voice=None, rate=None):
            with open(p, "wb") as handle:
                handle.write(b"")

        original_save = generate._tts_save
        generate._tts_save = empty_save
        try:
            ok = generate.generate_tts("各位好，这是一段足够长的测试播报稿内容，用于验证语音合成的重试逻辑。", path,
                                       sleep=lambda _s: None)
            check("拿到 0 字节文件判为失败", ok is False)
        finally:
            generate._tts_save = original_save

        # 播报稿过短直接跳过（不浪费重试）
        ok = generate.generate_tts("太短", path, sleep=lambda _s: None)
        check("播报稿过短直接跳过合成", ok is False)


def test_requirements_pins():
    group("依赖版本约束（edge-tts 必须有下限且不加上界）")

    req_path = os.path.join(ROOT, "requirements.txt")
    content = open(req_path, encoding="utf-8").read()

    check("edge-tts 下限不低于 7.0（6.1.19 会 403 握手失败）",
          "edge-tts>=7" in content.replace(" ", ""),
          "实际内容片段=%r" % [
              l for l in content.splitlines() if "edge-tts" in l and not l.strip().startswith("#")])
    tts_line = next((l for l in content.splitlines()
                     if l.strip().startswith("edge-tts")), "")
    check("edge-tts 不设置上界（微软收紧 token 时只有新版能跟上）",
          "<" not in tts_line, "实际=%r" % tts_line)


def test_broadcast_not_truncated():
    group("播报稿完整性（线上故障：硬切在「追踪巴勒」，页面和音频同时残缺）")

    # ---- 1. 句子边界截断，绝不切断词 ----
    # 直接复现线上那段被切断的文本
    real = (
        "此外，在南非两起大规模枪击案导致17人死亡，"
        "以色列在打击恐怖势力行动的同时，因利用面部识别平台追踪巴勒斯坦人的行动引发争议。"
    )
    old_style = real[:51]  # 「巴勒」在第 49–50 位，切到 51 正好停在半个词上
    check("旧逻辑 text[:51] 确实会切断词（故障复现）",
          old_style.endswith("巴勒"), "实际=%r" % old_style[-8:])

    new_style = generate.truncate_at_sentence(real, 51)
    check("新逻辑不会停在半个词上",
          not new_style.endswith(("巴勒", "巴", "勒")), "实际=%r" % new_style[-8:])
    check("新逻辑结果不比硬切长", len(new_style) <= 51, "实际=%d" % len(new_style))
    check("新逻辑保留了实质内容",
          bool(re.search(r"[\u4e00-\u9fff]", new_style)))

    # 短于上限时原样返回
    short = "各位好，今天没有新闻。"
    expect_eq("短文本原样返回", generate.truncate_at_sentence(short, 900), short)

    # 优先在句号处断开（而不是逗号）
    multi = "第一句话结束。第二句话也很长，但是还没说完就超了上限了。"
    cut = generate.truncate_at_sentence(multi, 20)
    check("优先在句号处断开", cut.endswith("。"), "实际=%r" % cut[-10:])
    check("优先断开时保留了第一句",
          cut.startswith("第一句话结束"), "实际=%r" % cut)

    # 全是标点不能截出空内容
    expect_eq("全标点输入返回空串", generate.truncate_at_sentence("，" * 300, 100), "")

    # ---- 2. 播报稿绝不超字数、且必定完整收尾 ----
    class OverlongAI:
        """模拟线上模型：要求 900 字以内，实际狂写几千字。"""

        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            return "国内经济运行稳健，国际局势持续紧张，各方保持关注。" * 60

    def make_events(count, prefix):
        return [
            {"cid": "%s%d" % (prefix, i),
             "zh": "第%d条事件概括内容大约二十个字左右" % i,
             "importance": "重要性说明", "source_count": 3, "image": "", "cluster": []}
            for i in range(1, count + 1)
        ]

    sections = [("国内", make_events(6, "c")), ("国际", make_events(6, "i"))]
    text = generate.build_broadcast(sections, OverlongAI(), generate.today_bj())

    check("播报稿不超字数上限",
          len(text) <= generate.BROADCAST_CHAR_LIMIT,
          "实际=%d 上限=%d" % (len(text), generate.BROADCAST_CHAR_LIMIT))
    check("播报稿以完整句子收尾",
          text[-1] in "。！？…!?", "实际结尾=%r" % text[-6:])
    check("播报稿带完整结束语（含『明天同一时间再见』）",
          "明天同一时间再见" in text[-40:], "实际结尾=%r" % text[-30:])
    check("结束语不重复",
          text.count("明天同一时间再见") == 1,
          "出现 %d 次" % text.count("明天同一时间再见"))

    # ---- 2b. 模型自己写了半截结束语时，要补全而不是再加一整句 ----
    half_ended = generate.ensure_broadcast_ending(
        "各位好，今天有新闻。以上就是今天的要闻。", "2026年11月17日", 900)
    check("半截结束语被补全为完整版",
          half_ended.endswith("以上就是今天的要闻，明天同一时间再见。"),
          "实际=%r" % half_ended[-30:])
    expect_eq("补全后不重复出现『以上就是今天的要闻』",
              half_ended.count("以上就是今天的要闻"), 1)

    # 模型写了完整结束语时保持不变
    full_ended = "各位好，今天有新闻。以上就是今天的要闻，明天同一时间再见。"
    expect_eq("完整结束语原样保留",
              generate.ensure_broadcast_ending(full_ended, "d", 900), full_ended)

    # ---- 3. 模型自己收尾时不重复加结束语 ----
    class NaturalAI:
        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            return ("各位好，今天是2026年11月17日，欢迎收听今日国际大事。"
                    "国内经济稳健，国际局势紧张。以上就是今天的要闻，明天同一时间再见。")

    natural = generate.build_broadcast(sections, NaturalAI(), generate.today_bj())
    expect_eq("模型已收尾时不重复加结束语",
              natural.count("明天同一时间再见"), 1)

    # ---- 4. AI 失败降级稿同样完整 ----
    class FailAI:
        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            raise ai_client.AIError("演练：AI 失败")

    fallback = generate.build_broadcast(sections, FailAI(), generate.today_bj())
    check("降级稿也完整收尾",
          fallback[-1] in "。！？…!?" and "明天同一时间再见" in fallback[-40:],
          "实际结尾=%r" % fallback[-30:])
    check("降级稿不超字数", len(fallback) <= generate.BROADCAST_CHAR_LIMIT)

    # ---- 5. 字数契约：提示词与代码共用同一个常量 ----
    captured = {}

    class CaptureAI:
        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            captured["prompt"] = prompt
            return "各位好，今天的播报到此结束。以上就是今天的要闻，明天同一时间再见。"

    generate.build_broadcast(sections, CaptureAI(), generate.today_bj())
    prompt = captured.get("prompt", "")
    check("提示词里的字数上限与代码常量一致",
          ("%d 字以内" % generate.BROADCAST_CHAR_LIMIT) in prompt,
          "提示词里没找到『%d 字以内』" % generate.BROADCAST_CHAR_LIMIT)
    check("提示词明确要求写完、不能停一半",
          "必须" in prompt and "完整结尾" in prompt,
          "提示词缺少『必须写完』的约束")
    check("提示词说明不必覆盖全部要点",
          "不必全部覆盖" in prompt)
    check("旧的不一致文案已移除（不再写死 600 字）",
          "600 字以内" not in prompt)

    # ---- 6. 事件过多时只挑最重要的几条进播报 ----
    many = [("国内", make_events(12, "c")), ("国际", make_events(20, "i"))]
    captured.clear()
    generate.build_broadcast(many, CaptureAI(), generate.today_bj())
    lines = [l for l in captured.get("prompt", "").splitlines()
             if re.match(r"^\d+\. ", l.strip())]
    limit = generate.BROADCAST_MAX_EVENTS_PER_SECTION * 2
    check("32 条事件时只选材不超过上限",
          len(lines) <= limit, "实际选了 %d 条，上限 %d" % (len(lines), limit))
    check("选材数量大于 0", len(lines) > 0)

    # ---- 7. 版块事件数硬上限 ----
    check("每版块保留事件数不超过 8 条（契约 5–8）",
          generate.MAX_EVENTS_PER_SECTION <= 8,
          "实际=%d" % generate.MAX_EVENTS_PER_SECTION)

    def make_cluster(cid, title, count):
        return {
            "cid": cid, "section": "international", "source_count": count,
            "image": "", "primary_title_zh": title,
            "published": datetime.now(BJ),
            "items": [{
                "source": "BBC", "publisher": "BBC", "title_zh": title, "title": title,
                "title_en": "", "summary": "",
                "url": "https://www.bbc.com/news/%s" % cid,
                "image": "", "published": datetime.now(BJ), "lang": "zh",
            }],
        }

    class ManyEventsAI:
        """模拟线上：AI 返回的条数远超 5–8 条契约。"""

        def complete(self, prompt, system_prompt=None, max_tokens=4096):
            cids = re.findall(r"^\[(i\d+)\]", prompt, re.MULTILINE)
            return "\n".join("[%s] 事件概括%d——重要性" % (cid, i)
                             for i, cid in enumerate(cids, 1))

    big = [make_cluster("i%d" % i, "互不相同的事件标题编号%d好多个字" % i, 1)
           for i in range(1, 21)]
    events, _log = generate.build_section_events(big, "国际", ManyEventsAI())
    check("AI 返回 20 条时被截到上限内",
          len(events) <= generate.MAX_EVENTS_PER_SECTION,
          "实际保留 %d 条" % len(events))
    check("截断后仍有足够条数（≥3）", len(events) >= 3)


def test_domestic_classification():
    group("国内源分类（线上故障：「伊朗油轮触雷」出现在国内热点版块）")

    names = [f["name"] for f in generate.DOMESTIC_FEEDS]
    check("国内源里没有「中新网·国际」频道",
          not any("国际" in n for n in names),
          "实际源=%r" % names)

    # 每个国内源都必须声明 publisher（否则会被当成独立媒体，虚增家数）
    check("国内源都声明了 publisher",
          all(f.get("publisher") for f in generate.DOMESTIC_FEEDS),
          "缺 publisher 的源：%r" % [
              f["name"] for f in generate.DOMESTIC_FEEDS if not f.get("publisher")])

    # 同一媒体的多频道 publisher 必须一致
    for feed in generate.DOMESTIC_FEEDS:
        check("源 %s 的 publisher 是媒体名而非频道名" % feed["name"],
              "·" not in feed["publisher"],
              "publisher=%r 看起来是频道名" % feed["publisher"])


def test_publisher_seat_quota():
    group("媒体席位配额（热点覆盖优先，配额只做兜底）")

    now = datetime.now(BJ)

    def cluster(cid, publisher, count=1, hours_ago=1):
        title = "事件标题编号%s内容大约二十个字" % cid
        return {
            "cid": cid, "section": "international", "source_count": count,
            "image": "", "primary_publisher": publisher, "primary_title_zh": title,
            "published": now - timedelta(hours=hours_ago),
            "items": [{
                "source": publisher, "publisher": publisher, "title_zh": title,
                "title": title, "title_en": "", "summary": "",
                "url": "https://example.org/%s" % cid, "image": "",
                "published": now, "lang": "zh",
            }],
        }

    # 25 条素材，France 24 占 20 条，另有 5 家小媒体各 1 条。
    # 结果会是：France 24 拿 2 席 + 5 家小媒体各 1 席 = 7 席，
    # 第 8 席只能再给 France 24（其他媒体已经没素材了）→ 3 席。
    # 这是「热点优先」下的正确取舍：宁可第 8 条重复来源，也不要空着。
    clusters = [cluster("f%d" % i, "France 24", hours_ago=i) for i in range(20)]
    clusters += [cluster("s%d" % i, "小媒体%d" % i, hours_ago=i + 30)
                 for i in range(5)]
    clusters.sort(key=lambda c: (c["source_count"], c["published"]), reverse=True)

    ordered, dropped = generate.apply_publisher_quota(clusters)

    check("席位必须填满（热点优先于多样性）",
          len(ordered) == generate.MAX_EVENTS_PER_SECTION,
          "实际 %d 条，上限 %d" % (len(ordered), generate.MAX_EVENTS_PER_SECTION))
    check("入选 + 舍去 = 总数",
          len(ordered) + len(dropped) == len(clusters),
          "入选 %d + 舍去 %d != %d" % (len(ordered), len(dropped), len(clusters)))

    pub_counts = {}
    for c in ordered:
        pub_counts[c["primary_publisher"]] = pub_counts.get(
            c["primary_publisher"], 0) + 1
    # 关键：其他媒体都用尽后才允许超配额，最多超出「席位余数」
    check("单一媒体占比不超过配额 + 少量溢出",
          pub_counts.get("France 24", 0) <= generate.PUBLISHER_SEAT_QUOTA + 2,
          "France 24 占了 %d 席" % pub_counts.get("France 24", 0))
    check("所有有素材的小媒体都露脸了",
          sum(1 for p in pub_counts if p.startswith("小媒体")) == 5,
          "只有 %d 家小媒体入选" % sum(
              1 for p in pub_counts if p.startswith("小媒体")))

    # 强验证：素材充足时（远多于席位），配额必须严格生效
    rich = [cluster("r%d" % i, "France 24", hours_ago=i) for i in range(20)]
    rich += [cluster("q%d_%d" % (i, j), "媒体%d" % i, hours_ago=i * 2 + j)
             for i in range(6) for j in range(3)]
    rich.sort(key=lambda c: (c["source_count"], c["published"]), reverse=True)
    ordered_rich, _ = generate.apply_publisher_quota(rich)
    rich_counts = {}
    for c in ordered_rich:
        rich_counts[c["primary_publisher"]] = rich_counts.get(
            c["primary_publisher"], 0) + 1
    check("素材充足时配额严格生效（不超配额）",
          rich_counts.get("France 24", 0) <= generate.PUBLISHER_SEAT_QUOTA,
          "France 24 占了 %d 席（上限 %d）" % (
              rich_counts.get("France 24", 0), generate.PUBLISHER_SEAT_QUOTA))
    check("素材充足时席位依然填满",
          len(ordered_rich) == generate.MAX_EVENTS_PER_SECTION,
          "实际 %d 条" % len(ordered_rich))

    # 关键取舍：素材刚好够、一家占大头时，热点覆盖优先。
    # 读者是来知道「今天发生了什么」，不是来检查媒体清单的。
    mono = [cluster("m%d" % i, "France 24", hours_ago=i) for i in range(6)]
    mono += [cluster("b1", "BBC", hours_ago=7), cluster("n1", "NPR", hours_ago=8)]
    picked_mono, _ = generate.apply_publisher_quota(mono)
    check("素材刚好够时优先填满席位（不为配额牺牲热点）",
          len(picked_mono) == len(mono),
          "8 条素材只入选 %d 条" % len(picked_mono))

    # 多源事件优先入选（caller 传进来时已按家数排好序）
    multi = [cluster("mm1", "France 24", count=4, hours_ago=20)]
    single = [cluster("ss1", "France 24", hours_ago=1)]
    picked, _ = generate.apply_publisher_quota(multi + single)
    expect_eq("多源事件优先入选", picked[0]["cid"], "mm1")

    expect_eq("空输入返回两个空列表",
              generate.apply_publisher_quota([]), ([], []))

    only = [cluster("o%d" % i, "中新网", hours_ago=i) for i in range(10)]
    picked_only, _ = generate.apply_publisher_quota(only)
    check("只有一家媒体时版块不空掉",
          len(picked_only) >= generate.MIN_EVENTS_PER_SECTION,
          "实际 %d 条" % len(picked_only))
def test_og_image_extraction():
    group("从正文页取 og:image（国内源 RSS 完全不带图，导致 0/8 有图）")

    html_with_og = (
        '<html><head><meta property="og:image" '
        'content="http://ichef.bbci.co.uk/ace/standard/240/pic.jpg">'
        "</head><body><p>正文</p></body></html>"
    )
    found = generate.extract_og_image(html_with_og)
    check("能取到 og:image",
          "pic.jpg" in found, "实际=%r" % found)
    check("og:image 也做了分辨率升级（BBC 240→1024）",
          "/1024/" in found and "/240/" not in found, "实际=%r" % found)
    check("og:image 也做了 http→https 升级",
          found.startswith("https://"), "实际=%r" % found)

    # twitter:image 兜底
    html_tw = ('<html><head><meta name="twitter:image" '
               'content="https://news.example-site.com/media/2026/11/17/tw-photo.jpg"></head></html>')
    expect_eq("twitter:image 兜底", generate.extract_og_image(html_tw),
              "https://news.example-site.com/media/2026/11/17/tw-photo.jpg")

    # 正文里第一张图兜底，且要过滤 logo / 追踪像素
    html_img = (
        '<html><body>'
        '<img src="https://news.example-site.com/logo.png">'
        '<img src="https://news.example-site.com/pixel/1x1.gif">'
        '<img src="https://news.example-site.com/media/2026/11/17/photo-real.jpg">'
        "</body></html>"
    )
    # 正文里第一张图**不再作为兜底**：实测中文站点（中新网）页面上全是
    # 导航箭头/站点 logo/广告图，任何兜底都会取到错图，而取错图比没图更糟。
    expect_eq("不再从正文 <img> 兜底取图（避免取到导航/广告图）",
              generate.extract_og_image(html_img), "")

    # 没有任何图时返回空串，不能报错
    expect_eq("无图返回空串", generate.extract_og_image("<html><body>x</body></html>"), "")
    expect_eq("空 HTML 返回空串", generate.extract_og_image(""), "")

    # fetch_article 必须同时返回正文和图片
    def fake_http(url, timeout=20):
        return (
            '<html><head><meta property="og:image" '
            'content="https://news.example-site.com/media/2026/11/17/a-photo.jpg"></head>'
            "<body><article>"
            + "".join("<p>%s</p>" % ("这是一段足够长的中文正文内容用来测试提取。" * 2)
                      for _ in range(6))
            + "</article></body></html>"
        ).encode("utf-8")

    body, image = generate.fetch_article("https://news.example-site.com/a", http_get=fake_http)
    check("fetch_article 同时返回正文", len(body) >= 200, "正文长度=%d" % len(body))
    expect_eq("fetch_article 同时返回主图", image, "https://news.example-site.com/media/2026/11/17/a-photo.jpg")

    # 兼容旧接口
    only_body = generate.fetch_article_text(
        "https://news.example-site.com/a", http_get=fake_http)
    expect_eq("fetch_article_text 仍然只返回正文", only_body, body)


def test_backfill_images():
    group("补图：给入选但无图的事件补上 og:image")

    now = datetime.now(BJ)

    def make_cluster(cid, url):
        title = "事件标题%s" % cid
        return {
            "cid": cid, "section": "domestic", "source_count": 1,
            "image": "", "primary_publisher": "中新网", "primary_title_zh": title,
            "published": now,
            "items": [{
                "source": "中新网", "publisher": "中新网", "title_zh": title,
                "title": title, "title_en": "", "summary": "",
                "url": url, "image": "", "published": now, "lang": "zh",
            }],
        }

    clusters = [
        make_cluster("c1", "https://www.chinanews.com.cn/a"),
        make_cluster("c2", "https://www.chinanews.com.cn/b"),
    ]
    day_entry = {
        "date": "2026-11-17", "broadcast": "x", "tts_ok": False,
        "sections": {
            "domestic": [
                {"cid": "c1", "zh": "事件一", "importance": "i",
                 "source_count": 1, "image": "", "cluster": []},
                {"cid": "c2", "zh": "事件二", "importance": "i",
                 "source_count": 1, "image": "", "cluster": []},
            ],
            "international": [],
        },
        "raw_news": [],
    }
    meta = {"clusters": {"domestic": clusters, "international": []}}

    def fake_http(url, timeout=20):
        name = url.rsplit("/", 1)[-1]
        return (
            '<html><head><meta property="og:image" '
            'content="https://news.example-site.com/media/2026/11/17/%s-photo.jpg"></head>'
            "<body><p>%s</p></body></html>"
            % (name, "正文内容" * 50)
        ).encode("utf-8")

    filled = generate.backfill_images(day_entry, meta, http_get=fake_http)
    expect_eq("两个无图事件都补到了图", filled, 2)
    check("事件一有了图片", bool(day_entry["sections"]["domestic"][0]["image"]))
    check("事件二有了图片", bool(day_entry["sections"]["domestic"][1]["image"]))
    check("补到的图是 https", all(
        e["image"].startswith("https://") for e in day_entry["sections"]["domestic"]))

    # 全部已有图后再跑一次：应当一个都不抓，返回 0
    filled_again = generate.backfill_images(day_entry, meta, http_get=fake_http)
    expect_eq("全部有图时不再抓取", filled_again, 0)

    # 已有图的事件不能被覆盖
    keep_url = "https://news.example-site.com/media/2026/11/17/keep-photo.jpg"
    day_entry["sections"]["domestic"][0]["image"] = keep_url
    generate.backfill_images(day_entry, meta, http_get=fake_http)
    expect_eq("已有图不被覆盖",
              day_entry["sections"]["domestic"][0]["image"], keep_url)

    # 抓取失败时静默跳过，不影响主流程
    def broken_http(url, timeout=20):
        raise OSError("演练：网络中断")

    day_entry["sections"]["domestic"][1]["image"] = ""
    filled3 = generate.backfill_images(day_entry, meta, http_get=broken_http)
    expect_eq("抓取失败时返回 0 而不是抛异常", filled3, 0)


def test_overseas_chinese_feeds():
    group("海外中文源（国内版块原先只有中新网一家，多源簇为 0）")

    check("OVERSEAS_ZH_FEEDS 存在且非空",
          isinstance(generate.OVERSEAS_ZH_FEEDS, list)
          and len(generate.OVERSEAS_ZH_FEEDS) >= 1,
          "实际=%r" % (generate.OVERSEAS_ZH_FEEDS,))

    for feed in generate.OVERSEAS_ZH_FEEDS:
        check("海外源 %s 有 name" % feed.get("name"), bool(feed.get("name")))
        check("海外源 %s 有 url" % feed.get("name"), bool(feed.get("url")))
        check("海外源 %s 有 publisher" % feed.get("name"),
              bool(feed.get("publisher")))
        check("海外源 %s 的 url 合法" % feed.get("name"),
              generate.is_valid_url(feed["url"]))

    # 关键的多样性检查：国内源 + 海外中文源必须来自 3 家以上不同媒体，
    # 否则「看不同观点」在国内版块仍然是空话。
    publishers = {f["publisher"] for f in generate.DOMESTIC_FEEDS}
    publishers |= {f["publisher"] for f in generate.OVERSEAS_ZH_FEEDS}
    check("国内版块可用媒体数 >= 3 家",
          len(publishers) >= 3,
          "只有 %d 家：%r" % (len(publishers), sorted(publishers)))

    # BBC 中文刻意不采用：它返回 100% 繁体，本站面向简体读者
    all_names = {f["name"] for f in generate.DOMESTIC_FEEDS}
    all_names |= {f["name"] for f in generate.OVERSEAS_ZH_FEEDS}
    check("没有采用 BBC 中文（实测返回繁体）",
          not any("BBC" in n for n in all_names),
          "实际源=%r" % sorted(all_names))


def test_dry_run_end_to_end():
    group("端到端 dry-run（零网络、零 CF 调用、只写 _dryrun/）")

    started = datetime.now()
    code = generate.run(dry_run=True, root=ROOT)
    elapsed = (datetime.now() - started).total_seconds()

    expect_eq("dry-run 退出码为 0", code, 0)
    check("dry-run 足够快（说明没发网络请求）", elapsed < 60, "实际 %.1f 秒" % elapsed)

    dry = os.path.join(ROOT, "_dryrun")
    check("_dryrun 目录被创建", os.path.isdir(dry))
    check("生成了 index.html", os.path.exists(os.path.join(dry, "index.html")))
    check("生成了 data/entries.json",
          os.path.exists(os.path.join(dry, "data", "entries.json")))
    check("生成了运行报告",
          os.path.isdir(os.path.join(dry, "data", "runs"))
          and len(os.listdir(os.path.join(dry, "data", "runs"))) >= 1)
    check("生成了归档目录", os.path.isdir(os.path.join(dry, "archive")))

    # 关键：绝不能覆盖真实站点文件
    real_index = os.path.join(ROOT, "index.html")
    if os.path.exists(real_index):
        with open(real_index, "r", encoding="utf-8") as handle:
            real_head = handle.read(200)
        check("dry-run 没有覆盖真实 index.html", real_head.strip() != "")

    with open(os.path.join(dry, "index.html"), "r", encoding="utf-8") as handle:
        html = handle.read()
    check("演练页面含国内版块", "国内热点" in html)
    check("演练页面含国际版块", "国际热点" in html)
    check("演练页面含播报区", "今日播报" in html)
    check("演练默认走无音频降级分支", "朗读播报" in html)

    with open(os.path.join(dry, "data", "entries.json"), "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    days = payload["days"]
    check("entries.json 结构正确", isinstance(days, list) and len(days) >= 1)
    today = generate.today_bj().strftime("%Y-%m-%d")
    check("今天的日期在数据里", any(d["date"] == today for d in days))

    # 31 天前的残留必须被清掉
    stale = (generate.today_bj() - timedelta(days=31)).strftime("%Y-%m-%d")
    check("31 天前的数据被清理", all(d["date"] != stale for d in days))
    check("31 天前的归档文件被删除",
          not os.path.exists(os.path.join(dry, "archive", "%s.html" % stale)))

    # 数据契约字段完整性
    today_day = next(d for d in days if d["date"] == today)
    for field in ("date", "broadcast", "tts_ok", "sections", "raw_news"):
        check("今日数据包含字段 %s" % field, field in today_day)
    for key in ("domestic", "international"):
        check("sections 包含 %s" % key, key in today_day["sections"])
    for key in ("domestic", "international"):
        events = today_day["sections"][key]
        check("%s 版块有事件" % key, len(events) >= 1, "实际=%d" % len(events))
        for event in events:
            for field in ("cid", "zh", "importance", "source_count", "image", "cluster"):
                check("%s 事件含字段 %s" % (key, field), field in event)
            check("%s 事件 cid 非空" % key, bool(event["cid"]))
            check("%s 事件 cluster 非空" % key, len(event["cluster"]) >= 1)
            for media in event["cluster"]:
                for field in ("source", "title_zh", "url", "is_primary"):
                    check("%s 媒体含字段 %s" % (key, field), field in media)
                check("%s 媒体链接合法" % key, generate.is_valid_url(media["url"]))

    # 多源簇必须体现出来（假数据里油轮事件有 2–3 家）
    intl = today_day["sections"]["international"]
    check("演练数据里存在多源簇",
          any(e["source_count"] >= 2 for e in intl),
          "实际 source_count=%r" % [e["source_count"] for e in intl])

    # 软新闻必须被过滤掉（假数据里的足球新闻）
    all_zh = " ".join(e["zh"] for e in intl)
    check("软新闻（足球）没有进入页面", "球员" not in all_zh and "足球" not in all_zh)

    # 国内版块必须真的产出（假数据里有中文源）
    dom = today_day["sections"]["domestic"]
    check("国内版块有事件", len(dom) >= 1, "实际=%d" % len(dom))
    check("国内事件是中文", all(generate.chinese_ratio(e["zh"]) > 0.5 for e in dom))

    # 归档页
    rest = [d for d in days if d["date"] != today]
    for day in rest:
        check("归档文件存在：%s" % day["date"],
              os.path.exists(os.path.join(dry, "archive", "%s.html" % day["date"])))


def test_dry_run_tts_ok_branch():
    group("端到端 dry-run：模拟语音合成成功（验证有音频渲染分支）")

    code = generate.run(dry_run=True, dry_run_tts_ok=True, root=ROOT)
    expect_eq("dry-run（tts_ok）退出码为 0", code, 0)

    dry = os.path.join(ROOT, "_dryrun")
    with open(os.path.join(dry, "index.html"), "r", encoding="utf-8") as handle:
        html = handle.read()
    check("有音频分支输出 <audio>",
          '<audio controls preload="none" src="audio/daily.mp3">' in html)
    check("有音频分支不输出朗读按钮", "dailyNewsSpeak()" not in html)

    with open(os.path.join(dry, "data", "entries.json"), "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    today = generate.today_bj().strftime("%Y-%m-%d")
    today_day = next(d for d in payload["days"] if d["date"] == today)
    check("数据里 tts_ok 为 true", today_day["tts_ok"] is True)

    mp3 = os.path.join(dry, "audio", "daily.mp3")
    check("生成了音频文件", os.path.exists(mp3))
    check("音频文件头合法", generate.is_valid_mp3(mp3))


# ---------------------------------------------------------------- 17. 守卫


def test_workflow_guard():
    group("workflow 仓库名守卫（绝不碰主页仓库）")

    workflow = os.path.join(ROOT, ".github", "workflows", "daily.yml")
    check("workflow 文件存在", os.path.exists(workflow))
    if not os.path.exists(workflow):
        return

    with open(workflow, "r", encoding="utf-8") as handle:
        content = handle.read()

    check("含仓库名守卫",
          "tyuecode/daily-news" in content and "GITHUB_REPOSITORY" in content)
    check("守卫会 exit 1",
          "exit 1" in content)
    check("定时触发 cron 为 0 0 * * *", "0 0 * * *" in content)
    check("支持手动触发", "workflow_dispatch" in content)
    check("并发组设置为 pages 且不取消进行中任务",
          "group: pages" in content and "cancel-in-progress: false" in content)
    check("权限只给 contents: write", "contents: write" in content)
    check("不再使用 GitHub Pages 的 configure-pages",
          "configure-pages" not in content, "仍残留 configure-pages")
    check("不再使用 upload-pages-artifact",
          "upload-pages-artifact" not in content)
    check("不再使用 deploy-pages", "deploy-pages" not in content)
    check("使用 python 3.11", "python-version: '3.11'" in content)
    check("先跑 smoke_test.py", "python smoke_test.py" in content)
    check("再跑 generate.py", "python scripts/generate.py" in content)
    check("用 wrangler 部署 Cloudflare Pages", "wrangler" in content.lower())
    check("部署到 _site 目录", "_site" in content)
    check("注入 CF_ACCOUNT_ID", "CF_ACCOUNT_ID" in content)
    check("注入 CF_API_TOKEN", "CF_API_TOKEN" in content)
    check("注入 CF_PAGES_PROJECT", "CF_PAGES_PROJECT" in content)
    check("配置 bot 提交身份", "daily-news-bot" in content)
    check("有 git diff 守卫（无变更不提交）",
          "git diff --cached --quiet" in content)

    # smoke_test 必须排在 generate 之前
    smoke_pos = content.find("python smoke_test.py")
    gen_pos = content.find("python scripts/generate.py")
    check("smoke_test 在 generate 之前执行", 0 <= smoke_pos < gen_pos)

    # 不允许把密钥「值」回显到日志；但允许在提示文案里提到密钥「名字」。
    # 判断标准：出现在 echo 里的必须只是变量名/中文说明，不能是 ${{ secrets.X }} 的实际取值。
    for line in content.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("echo") or stripped.startswith("run: echo")):
            continue
        # 真正的泄漏形态：把 secrets 直接 echo 出去
        check("echo 里没有直接输出 secrets 值",
              "${{ secrets." not in stripped, stripped)
        # 也不太可能在 echo 里出现 $CF_API_TOKEN 这类变量解引用
        check("echo 里没有解引用密钥变量",
              "$CF_API_TOKEN" not in stripped and "$CF_ACCOUNT_ID" not in stripped,
              stripped)

    # 整体扫描：文件里不应出现形如 40 位十六进制/字母数字的硬编码 token
    hardcoded = re.findall(r"\b[A-Za-z0-9_\-]{40}\b", content)
    check("workflow 里没有硬编码的长 token",
          not hardcoded, "疑似硬编码：%r" % hardcoded[:3])


def test_readme():
    group("README（必须改成 edge-tts + Cloudflare Pages）")

    readme = os.path.join(ROOT, "README.md")
    check("README 存在", os.path.exists(readme))
    if not os.path.exists(readme):
        return

    with open(readme, "r", encoding="utf-8") as handle:
        content = handle.read()

    check("README 使用 edge-tts", "edge-tts" in content)
    check("README 提到 Cloudflare Pages", "Cloudflare Pages" in content)
    check("README 不再推荐 melotts 作为方案",
          "melotts" not in content.lower() or "弃用" in content.lower())
    check("README 说明三个 Secrets",
          all(s in content for s in ("CF_ACCOUNT_ID", "CF_API_TOKEN", "CF_PAGES_PROJECT")))
    check("README 说明 dry-run 用法", "--dry-run" in content)
    check("README 说明归档保留 30 天", "30 天" in content)
    check("README 说明国内/国际双版块",
          "国内热点" in content and "国际热点" in content)


# ---------------------------------------------------------------- 主入口


ALL_TESTS = [
    test_ai_payloads,
    test_code_fence,
    test_ai_retry,
    test_ai_missing_credentials,
    test_word_boundary,
    test_bigram_jaccard,
    test_clustering_rules,
    test_cid_stability,
    test_translation_parsing,
    test_translate_titles_fallback,
    test_batch_size,
    test_segment_split,
    test_html_escape,
    test_image_normalize,
    test_url_validation,
    test_country_entities,
    test_event_redo_and_fallback,
    test_render_with_audio,
    test_render_without_audio,
    test_render_archive,
    test_render_empty,
    test_render_with_recent_days,
    test_mp3_header,
    test_prune,
    test_publish_gate,
    test_normalize_items,
    test_china_related_filter,
    test_source_status_collection,
    test_prompts,
    test_config_integrity,
    test_stale_source_guard,
    test_collapse_repeated_prefix,
    test_split_summary_importance,
    test_media_title_not_duplicated,
    test_chinese_source_not_translated,
    test_publisher_voting,
    test_entity_conflict_guard,
    test_cluster_threshold_calibration,
    test_tts_retry_behaviour,
    test_requirements_pins,
    test_broadcast_not_truncated,
    test_domestic_classification,
    test_publisher_seat_quota,
    test_og_image_extraction,
    test_backfill_images,
    test_overseas_chinese_feeds,
    test_dry_run_end_to_end,
    test_dry_run_tts_ok_branch,
    test_workflow_guard,
    test_readme,
]


def main():
    print("=" * 70)
    print("每日新闻播报站 · 运行时冒烟测试")
    print("零网络 / 零 Cloudflare 调用 / 零额度消耗")
    print("=" * 70)

    crashed = []
    for test in ALL_TESTS:
        try:
            test()
        except Exception:  # noqa: BLE001 单个用例崩溃不影响其它用例
            crashed.append(test.__name__)
            print("\n❌ 用例 %s 崩溃：" % test.__name__)
            traceback.print_exc()

    print()
    print("=" * 70)
    if CASE.failures:
        print("失败明细：")
        for name, detail in CASE.failures:
            print("  ❌ %s  →  %s" % (name, detail))
        print("-" * 70)
    if crashed:
        print("崩溃的用例：%s" % ", ".join(crashed))
        print("-" * 70)

    ok = CASE.passed
    total = CASE.total
    print("断言：%d / %d 通过" % (ok, total))
    if CASE.failures or crashed:
        print("结论：❌ 冒烟测试未通过，禁止继续执行 generate.py")
        print("=" * 70)
        return 1

    print("结论：✅ 冒烟测试全部通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
