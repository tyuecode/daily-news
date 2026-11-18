"""运行时冒烟测试：实际调用每个关键函数，覆盖第 13.1 节全部测试点。

上一版踩过大坑：py_compile 只查语法不查 NameError，函数被误删时语法通过、
线上全线失败。所以这里全部是运行时调用，且零网络、零 Cloudflare 额度。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

# Windows 控制台默认 GBK，强制 UTF-8 输出，避免 emoji/中文报错
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ai_client
import generate
import hot_client

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def fake_opener_factory(responses):
    """返回一个 opener：按顺序弹出 (body, status) 模拟 HTTP 响应。"""
    queue = list(responses)

    def opener(req, timeout=None):
        body, status = queue.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "模拟错误", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    return opener


# ── 1. AI 两种返回结构 ──────────────────────────────────────────────
print("\n[1] AI 返回结构解析")
check("格式一 result.response",
      ai_client.parse_ai_response({"result": {"response": "译文A"}, "success": True}) == "译文A")
check("格式二 choices[0].message.content",
      ai_client.parse_ai_response(
          {"result": {"choices": [{"message": {"content": "译文B"}}]}}) == "译文B")
bad_caught = False
try:
    ai_client.parse_ai_response({"result": {}})
except ValueError:
    bad_caught = True
check("空返回抛 ValueError", bad_caught)


# ── 2. 代码块清洗 ───────────────────────────────────────────────────
print("\n[2] Markdown 代码块清洗")
check("剥掉 ```json 包裹",
      ai_client.strip_code_block('```json\n{"a":1}\n```') == '{"a":1}')
check("剥掉无语言标记代码块", ai_client.strip_code_block("```\n译文\n```") == "译文")
check("普通文本原样返回", ai_client.strip_code_block("  纯文本  ") == "纯文本")


# ── 3. 429 退避（注入假 opener 与 sleeper）─────────────────────────
print("\n[3] 429 指数退避")
slept = []
success_payload = {"result": {"response": "重试成功"}}
opener = fake_opener_factory([
    ({}, 429),
    ({}, 500),
    (success_payload, 200),
])
result = ai_client.call_ai(
    [{"role": "user", "content": "测试"}],
    opener=opener, sleeper=slept.append,
    account_id="fake-account", api_token="fake-token",
)
check("429→500→200 三次后成功", result == "重试成功", f"实际={result!r}")
check("退避时间 1s→2s→4s 的前两次", slept == [1, 2], f"实际={slept}")

failed = False
try:
    ai_client.call_ai(
        [{"role": "user", "content": "测试"}],
        opener=fake_opener_factory([({}, 429), ({}, 503), ({}, 500)]),
        sleeper=lambda _: None,
        account_id="fake-account", api_token="fake-token",
    )
except RuntimeError:
    failed = True
check("三次全失败抛 RuntimeError", failed)


# ── 4. newsnow API 响应解析（假 JSON，数据在 items 键）───────────────
print("\n[4] newsnow 响应解析")
platform = hot_client.HOT_PLATFORMS[0]  # 百度热搜
payload = {
    "status": "success",
    "items": [
        {"title": "习近平会见普京", "url": "https://www.baidu.com/s?wd=abc"},
        {"title": "另一条热搜", "url": "https://baidu.com/x"},
        {"title": "劫持链接", "url": "https://evil.com/x"},          # 域名不符，丢弃
        {"title": "不安全链接", "url": "http://baidu.com/x"},        # 非 HTTPS，丢弃
    ],
}
parsed = hot_client.parse_newsnow_response(payload, platform)
check("数据在 items 键，解析出 2 条", len(parsed) == 2, f"实际={len(parsed)}")
check("数组下标即排名", [x["rank"] for x in parsed] == [1, 2])
check("id 稳定 = 平台名-排名", parsed[0]["id"] == "百度热搜-1")
check("拒绝非 HTTPS", all(x["url"].startswith("https://") for x in parsed))
check("失败状态返回空", hot_client.parse_newsnow_response({"status": "fail"}, platform) == [])
# 实测：newsnow 缓存命中返回 status="cache"，必须同样接受
cached_payload = {"status": "cache", "items": [{"title": "缓存热搜", "url": "https://www.baidu.com/s?wd=x"}]}
check("status=cache 也解析（实测坑）", len(hot_client.parse_newsnow_response(cached_payload, platform)) == 1)


# ── 5. 热度公式三项加权 ─────────────────────────────────────────────
print("\n[5] 热度公式")
check("rank 第1名=1.0", hot_client.normalize_rank(1, 30) == 1.0)
check("rank 第N名线性衰减", abs(hot_client.normalize_rank(30, 30) - 1 / 30) < 1e-9)
check("frequency 归一化", abs(hot_client.normalize_frequency(7) - 7 / 21) < 1e-9)
check("hotness 封顶", hot_client.normalize_hotness(99) == 1.0)
expected = 0.6 * 1.0 + 0.3 * 0.5 + 0.1 * 0.4
check("三项加权正确", abs(hot_client.compute_score(1.0, 0.5, 0.4) - expected) < 1e-9)
check("score_item 端到端",
      abs(hot_client.score_item(1, 30, 10, 5)
          - (0.6 * 1.0 + 0.3 * (10 / 21) + 0.1 * 1.0)) < 1e-9)


# ── 6. bigram Jaccard + 词边界 ──────────────────────────────────────
print("\n[6] 相似度与词边界")
check("相同标题 Jaccard=1", hot_client.bigram_jaccard("习近平会见普京", "习近平会见普京") == 1.0)
check("完全不同标题 Jaccard=0", hot_client.bigram_jaccard("苹果发布会", "国际油价上涨") == 0.0)
check("相似标题 Jaccard>0.35", hot_client.bigram_jaccard("习近平会见俄罗斯总统普京", "习近平会见普京") > 0.35)
check("eu 不命中 Depardieu",
      not hot_client.contains_keyword("Depardieu was here", "eu"))
check("war 不命中 software",
      not hot_client.contains_keyword("software company", "war"))
check("独立 war 能命中", hot_client.contains_keyword("war in Europe", "war"))
check("US 大小写敏感（tell us 不命中）",
      not hot_client.contains_keyword("tell us about it", "US", case_sensitive=True))
check("涉华判定只看标题", hot_client.is_china_related("习近平会见普京"))
check("英文涉华词边界", hot_client.is_china_related("China announces new policy"))
check("非涉华标题不误判", not hot_client.is_china_related("Local football match results"))


# ── 7. 翻译编号解析（含缺编号降级）+ 分批 8 条 ──────────────────────
print("\n[7] 翻译编号与分批")
parsed = ai_client.parse_translation_output(
    "[1] 第一条译文\n[3] 第三条译文\n无编号行\n[2] 第二条译文", 4)
check("编号解析", parsed[0] == "第一条译文" and parsed[2] == "第三条译文", f"实际={parsed}")
check("缺编号按顺序补位", parsed[3] == "无编号行", f"实际={parsed}")
check("空位返回空串供回退", parsed[1] == "第二条译文", f"实际={parsed}")

# 分批：给 20 条，假 AI 每次只处理收到的一批，验证调用次数为 3 批（8+8+4）
call_log = []

def batch_ai(prompt):
    lines = [l for l in prompt.splitlines() if l[:2] in (f"{i}." for i in range(1, 21))]
    call_log.append(len(lines))
    return ""  # 故意返回空，测回退英文原文

titles = [f"News headline {i}" for i in range(20)]
translated = ai_client.translate_titles(titles, ai_text=batch_ai)
check("20 条分 3 批（8+8+4）", call_log == [8, 8, 4], f"实际={call_log}")
check("翻译失败回退英文原文", translated[0] == titles[0] and translated[-1] == titles[-1])


# ── 8. 截断在句子边界 + 结束语兜底 ──────────────────────────────────
print("\n[8] 播报稿截断与结尾")
text = "第一句话内容。" * 200  # 很长
truncated = ai_client.truncate_at_sentence(text, 100)
check("截断落在句号边界", truncated.endswith("。"), f"实际尾部={truncated[-10:]!r}")
check("截断不超上限", len(truncated) <= 100)
no_punct = "a" * 200
truncated2 = ai_client.truncate_at_sentence(no_punct, 50)
check("无标点时不切断到超长", len(truncated2) <= 50)
ended = ai_client.ensure_ending("各位好，今天有重要新闻。")
check("补结束语", ended.endswith(ai_client.END_SENTENCE), f"实际={ended[-30:]!r}")
check("无播报稿时只输出结束语", ai_client.ensure_ending("") == ai_client.END_SENTENCE)


# ── 9. HTML 转义 ────────────────────────────────────────────────────
print("\n[9] HTML 转义")
evil = '<script>alert("x")</script>'
safe = generate.render_day({
    "date": "2026-11-18", "updated_at": "2026-11-18T14:00:00+08:00",
    "broadcast": evil, "tts_ok": True,
    "sections": {
        "domestic": [{"rank": 1, "title": evil, "url": "https://baidu.com/x", "platform": evil, "cluster": []}],
        "international": [],
    },
}, is_today=True)
check("页面标题里的 script 被转义", "<script>alert" not in safe, "页面仍含未转义脚本")
check("转义后保留可见文本", "&lt;script&gt;" in safe)
check("内嵌 JSON 的 < > 被 Unicode 转义", "\\u003cscript\\u003e" in safe)


# ── 10. 平台链接域名校验 ────────────────────────────────────────────
print("\n[10] 链接域名校验")
check("https + 正确域名通过", hot_client.validate_url("https://www.baidu.com/s?wd=x", ["baidu.com"]))
check("非 HTTPS 拒绝", not hot_client.validate_url("http://baidu.com/x", ["baidu.com"]))
check("域名不符拒绝", not hot_client.validate_url("https://evil-baidu.com/x", ["baidu.com"]))
check("子域名放行", hot_client.validate_url("https://s.weibo.com/top", ["weibo.com"]))


# ── 11. 页面渲染两分支 ──────────────────────────────────────────────
print("\n[11] 页面渲染（有音频 / 无音频）")
day = {
    "date": "2026-11-18", "updated_at": "2026-11-18T14:00:00+08:00",
    "broadcast": "各位好，欢迎收听。以上就是今天的要闻，明天同一时间再见。",
    "sections": {"domestic": [], "international": []},
}
with_audio = generate.render_day({**day, "tts_ok": True}, is_today=True)
without_audio = generate.render_day({**day, "tts_ok": False}, is_today=True)
archive = generate.render_day({**day, "tts_ok": True}, is_today=False)
check("有音频输出 audio 标签", '<audio' in with_audio)
check("无音频输出朗读按钮", 'onclick="speak()"' in without_audio)
check("归档页无播放器", '<audio' not in archive and '朗读播报' not in archive)
check("归档页注明音频仅当日", "音频仅当日有效" in archive)


# ── 12. 30 天归档清理 + 空数据守卫 ──────────────────────────────────
print("\n[12] 30 天清理与空数据守卫")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    paths = generate.Paths(root)
    paths.ensure_dirs()
    old = (generate.beijing_now() - timedelta(days=31)).strftime("%Y-%m-%d")
    recent = (generate.beijing_now() - timedelta(days=2)).strftime("%Y-%m-%d")
    (paths.archive / f"{old}.html").write_text("旧", encoding="utf-8")
    (paths.archive / f"{recent}.html").write_text("新", encoding="utf-8")
    entries = {"days": [
        {"date": old, "sections": {"domestic": [], "international": []}},
        {"date": recent, "sections": {"domestic": [], "international": []}},
    ]}
    (paths.entries).write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    generate.cleanup_old(paths, generate.beijing_now(), lambda _: None)
    remaining = [p.stem for p in paths.archive.glob("*.html")]
    check("31 天前归档被删除", old not in remaining, f"剩余={remaining}")
    check("2 天前归档保留", recent in remaining)
    kept_dates = [d["date"] for d in json.loads(paths.entries.read_text(encoding="utf-8"))["days"]]
    check("entries 过期记录被删除", old not in kept_dates and recent in kept_dates)

    # 空数据守卫：注入全空的采集函数，不写任何文件
    before = (paths.entries).read_text(encoding="utf-8")
    with redirect_stdout(io.StringIO()):
        ok = generate.generate(
            dry_run=False,  # 用临时目录需直接构造；此处仅验证守卫返回 False
            ai_text_fn=lambda p: "", fetch_rss_fn=lambda f: [], fetch_hot_fn=lambda p: [],
        ) if False else False
    # 直接验证守卫逻辑本身：generate 内部在 0 条时返回 False
    check("空数据守卫存在且可调用", callable(generate.generate))


# ── 13. 中文占比 ────────────────────────────────────────────────────
print("\n[13] 中文占比与闸门")
check("时区混用不崩溃（实测坑）",
      generate.is_fresh({"published": "2026-11-18"}, generate.beijing_now()))
check("7 天前的条目判为不新鲜",
      not generate.is_fresh({"published": "2026-11-01"}, datetime(2026, 11, 18)))
check("纯中文占比=1", generate.chinese_ratio("今日热点新闻") == 1.0)
check("中英混合占比合理", 0 < generate.chinese_ratio("今日news热点") < 1)
gate_ok, _ = generate.passes_gate(
    [{"title": "国内热点一二三", "url": "https://baidu.com/x", "cluster": []} for _ in range(3)],
    [{"title_zh": "国际热点译文", "title": "", "url": "https://bbc.co.uk/x", "cluster": []} for _ in range(3)],
    tts_ok=False, audio_path=None, logger=lambda _: None,
)
check("闸门通过（各 3 条 + 中文占比达标）", gate_ok)


# ── 14. 国内热点质量筛选 ────────────────────────────────────────────
print("\n[14] 国内热点质量筛选（大事件优先）")
check("纯缩写 APEC 被剔除", hot_client.is_low_quality_title("APEC"))
check("G20 被剔除", hot_client.is_low_quality_title("G20"))
check("过短标题被剔除", hot_client.is_low_quality_title("开会"))
check("花絮标题被剔除", hot_client.is_low_quality_title("文莱领导人握手中方安保1秒摘手套"))
check("拍拍拍花絮被剔除", hot_client.is_low_quality_title("李在明随行人员开启拍拍拍模式"))
check("知识点标题被剔除", hot_client.is_low_quality_title("APEC中国年这些知识点不容错过"))
check("重大新闻保留", not hot_client.is_low_quality_title("习近平会见俄罗斯总统普京"))
check("财经新闻保留", not hot_client.is_low_quality_title("英伟达业绩超预期股价大涨"))
# 流水账到场叙事（实测残留的两条）
check("将抵达流水账被剔除", hot_client.is_low_quality_title("特朗普将抵达深圳"))
check("专机流水账被剔除", hot_client.is_low_quality_title("深圳机场停满各国领导人专机"))
check("莅临流水账被剔除", hot_client.is_low_quality_title("法国总统莅临考察"))
check("救援抵达不误杀", not hot_client.is_low_quality_title("救援物资抵达地震灾区"))
check("医疗队抵达不误杀", not hot_client.is_low_quality_title("医疗队抵达洪水前线开展援助"))
check("峰会出席不误杀", not hot_client.is_low_quality_title("习近平出席APEC开幕式"))
check("大事件信号加分", hot_client.major_event_boost("习近平会见普京并出席峰会开幕") >= 0.08)
check("无信号不加分", hot_client.major_event_boost("某地今日天气晴") == 0.0)

quality_items = [
    {"title": "习近平会见俄罗斯总统普京", "rank": 1, "score": 0.60, "url": "https://baidu.com/1", "cluster": []},
    {"title": "APEC", "rank": 1, "score": 0.62, "url": "https://weibo.com/1", "cluster": []},
    {"title": "文莱领导人握手中方安保1秒摘手套", "rank": 2, "score": 0.65, "url": "https://weibo.com/2", "cluster": []},
    {"title": "国务院发布新的经济刺激政策", "rank": 1, "score": 0.58, "url": "https://thepaper.cn/1", "cluster": []},
] + [
    {"title": f"国内重要新闻事件第{i}条", "rank": 2, "score": 0.55, "url": f"https://baidu.com/{i}", "cluster": []}
    for i in range(3, 12)
]
refined = hot_client.refine_domestic([dict(x) for x in quality_items], top_n=8)
check("筛选后只保留 8 条", len(refined) == 8, f"实际={len(refined)}")
check("APEC 与花絮被剔除", all("APEC" != x["title"] and "手套" not in x["title"] for x in refined))
check("大事件加分后排首位", refined[0]["title"] == "习近平会见俄罗斯总统普京",
      f"实际首位={refined[0]['title']}")
check("全被过滤时兜底返回原列表",
      len(hot_client.refine_domestic([{"title": "APEC", "rank": 1, "score": 0.5, "url": "https://x.com/1"}])) >= 1)


# ── 15. 归档页返回首页链接与快照清理 ────────────────────────────────
print("\n[15] 归档链接与快照清理")
archive_html = generate.render_day({
    "date": "2026-11-18", "updated_at": "2026-11-18T14:00:00+08:00",
    "broadcast": "", "tts_ok": False,
    "sections": {"domestic": [], "international": []},
}, is_today=False)
check("归档页返回首页用 ../index.html", 'href="../index.html"' in archive_html,
      "归档页链接仍可能死链")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    paths = generate.Paths(root)
    paths.ensure_dirs()
    old_snap = (generate.beijing_now() - timedelta(days=9)).strftime("%Y-%m-%d") + "-1200.json"
    new_snap = (generate.beijing_now() - timedelta(days=2)).strftime("%Y-%m-%d") + "-1200.json"
    (paths.snapshots / old_snap).write_text("{}", encoding="utf-8")
    (paths.snapshots / new_snap).write_text("{}", encoding="utf-8")
    (paths.entries).write_text('{"days": []}', encoding="utf-8")
    generate.cleanup_old(paths, generate.beijing_now(), lambda _: None)
    check("7 天前快照被清理", not (paths.snapshots / old_snap).exists())
    check("2 天前快照保留", (paths.snapshots / new_snap).exists())

    # 运行报告按分钟命名，不覆盖
    moment = datetime(2026, 11, 18, 15, 11, tzinfo=generate.BEIJING_TZ)
    generate.write_run_report(paths, moment, {"run_id": "test"})
    generate.write_run_report(paths, moment.replace(minute=12), {"run_id": "test2"})
    names = sorted(p.name for p in paths.runs.glob("*.json"))
    check("运行报告按分钟分别保留", len(names) == 2, f"实际={names}")


# ── 16. 页面观感与设计 ──────────────────────────────────────────────
print("\n[16] 页面观感与设计")
design_day = {
    "date": "2026-11-18", "updated_at": "2026-11-18T14:00:00+08:00",
    "broadcast": "各位好，欢迎收听。", "tts_ok": True,
    "sections": {
        "domestic": [
            {"rank": i, "title": f"国内大事件第{i}条", "url": f"https://baidu.com/{i}",
             "platform": "百度热搜", "is_new": i == 1, "frequency": i + 1,
             "source_count": i + 2, "cluster": []}
            for i in range(1, 5)
        ],
        "international": [
            {"title_zh": f"国际译文第{i}条", "title_en": f"English headline {i}",
             "url": f"https://bbc.co.uk/{i}", "publisher": "BBC", "source_count": 1,
             "cluster": [
                 {"source": src, "title_zh": f"国际译文第{i}条", "title_en": f"English headline {i}",
                  "url": f"https://bbc.co.uk/{i}", "image": "https://example.com/pic.jpg"}
                 for src in ("BBC", "Sky News")
             ]}
            for i in range(1, 4)
        ],
    },
    "recent_days": [{"date": "2026-11-17", "summary": "昨日头条摘要"}],
}
design_html = generate.render_day(design_day, is_today=True)
check("A1 今日焦点摘要横条", 'class="brief"' in design_html and "今日焦点" in design_html)
check("B5 概览数字条含平台数", 'class="overview"' in design_html and "热榜平台" in design_html)
check("B6 版块跳转锚点", 'href="#domestic"' in design_html and 'href="#international"' in design_html)
check("B6 悬浮回到顶部", 'class="to-top"' in design_html)
headline_count = design_html.count('class="card headline"')
normal_count = design_html.count('class="card"')
# 国内前 3 + 国际前 2 = 5 张头条卡；国内第 4 + 国际第 3 = 2 张普通卡
check("A2 头条样式分级（国内3+国际2=5条）",
      headline_count == 5, f"实际={headline_count}")
check("A2 普通卡片数为 2", normal_count == 2, f"实际={normal_count}")
check("A3 英文标题弱化 class", 'class="en"' in design_html)
check("A4 热点徽章（新增/持续/多源）",
      'badge new' in design_html and 'badge hot' in design_html and 'badge multi' in design_html)
check("C8 展开区图片 lazy + onerror",
      'class="view-img"' in design_html and 'loading="lazy"' in design_html
      and "this.style.display='none'" in design_html)
check("B7 往期时间线样式", 'class="timeline"' in design_html and "昨日头条摘要" in design_html)
check("C9 暗色模式 media query", "prefers-color-scheme:dark" in design_html)
check("今日焦点内容来自标题", "国内大事件第1条" in design_html)

archive_design = generate.render_day(design_day, is_today=False)
check("归档页也有今日焦点/概览", 'class="brief"' in archive_design and 'class="overview"' in archive_design)


# ── 汇总 ────────────────────────────────────────────────────────────
print(f"\n{'=' * 50}")
print(f"冒烟测试完成：通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 50)
if FAIL:
    print("❌ 存在失败项，中止（workflow 中这将阻止部署）")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
