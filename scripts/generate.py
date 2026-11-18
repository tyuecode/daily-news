#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日新闻播报站 —— 核心生成脚本。

流程分层（每层都有降级，任何一层出问题都不许把整站搞空）：
    L0 准备    读历史 entries.json、生成 run_id、清掉今日临时产物
    L1 采集    并发抓「国际源 + 国内中文源」，单源超时 20 秒
    L2 规范化  时间过滤(近 36h) / URL 去重 / 软新闻词边界过滤 / 图片 URL 归一
    L3 理解    分批翻译英文标题(8 条/批) / 中文 bigram Jaccard 聚类 / AI 生成今日大事
    L4 校验闸门 国家实体对账 / 链接归属 / 中文占比 / 条数校验，不合格往回打（≤2 次）
    L5 深挖    选 6 篇抓正文（403 回退 RSS 摘要），按句子边界分段（约 900 字符）翻译
    L6 播报    AI 改写 600 字播报稿 → 先删旧 mp3 → edge-tts 合成（3 次重试）
    L7 发布闸门 每版块 ≥3 条 / 链接合法 / 中文占比 >60% / 音频头合法
    L8 输出    写 JSON → 渲染首页与归档 → 清理 30 天前 → 打印待提交提示

设计原则：
  1. 易用优先：默认视图只给「一句话概括 + 一句重要性 + N 家媒体报道」，
     多家媒体的观点对比默认折叠，用户点开才展开。
  2. 观点并存：同一事件并列多家媒体（cluster），来源标注清楚，让读者自己判断。
  3. 一次写对：本文件所有关键函数都被 smoke_test.py 实际调用过，不靠人肉试错。

密钥约定：CF_ACCOUNT_ID / CF_API_TOKEN 只从环境变量读取，绝不写进代码、注释或日志。

用法：
    python scripts/generate.py               正式运行（会写真实站点文件）
    python scripts/generate.py --dry-run     假数据演练（零网络、零 CF 调用，只写 _dryrun/）
    python scripts/generate.py --dry-run --dry-run-tts-ok
                                            演练时额外验证「有音频」那条渲染分支
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# 让 `python scripts/generate.py` 与 `import scripts.generate` 都能找到同目录的 ai_client
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_client import AIError, AIClient, clean_model_output_lines  # noqa: E402

# ============================================================================
# 一、常量与配置
# ============================================================================

# 北京时间（页面日期、播报日期都按这个算）
BJ_TZ = timezone(timedelta(hours=8))

# 新闻时效窗口：近 36 小时
WINDOW_HOURS = 36

# 归档保留天数（音频只留最新一天）
KEEP_DAYS = 30

# 首页展示「最近 N 天」摘要
RECENT_DAYS = 7

# 深挖全文翻译的文章数量
DEEP_DIVE_COUNT = 6

# ----------------------------------------------------------------------------
# 播报稿字数上限与选材条数
# ----------------------------------------------------------------------------
# 历史故障：原本提示词写「600 字以内」，代码却 return text[:900] 硬切。
# 线上实际出现了 32 条事件（国内 12 + 国际 20），光罗列就需约 885 字，
# 模型只能写到 900+，再被硬切，句子断在「追踪巴勒」——页面文字和 mp3 音频同时残缺。
#
# 现在的做法：
#   1. 播报只挑最重要的若干条（不需要覆盖全部事件），保证能在字数内讲完
#   2. 提示词与代码共用同一个字数上限，不再各写各的
#   3. 万一还是超长，按句子边界截断，绝不切断词
BROADCAST_CHAR_LIMIT = 900

# 结束语预留字数：截断时要先扣掉这么多，否则「截到 900 + 追加 18 字结束语」会超限
BROADCAST_ENDING_RESERVE = 24

# 播报稿最多覆盖多少条事件（每个版块各取前 N 条，按家数/时间排序后的顺序）
BROADCAST_MAX_EVENTS_PER_SECTION = 6

# 每个版块最多保留几条大事。契约写的是 5–8 条，但实测 AI 会返回 12–20 条，
# 这里做硬性上限，避免页面和播报稿被稀释。
MAX_EVENTS_PER_SECTION = 8

# 单源事件被配额挤下去之后，入选数若低于这个下限，就放宽配额补齐。
# 理由：版块空掉的危害 > 一家媒体多占几席。今天确实只有一两家发了稿时，
# 硬守配额会让「国内热点」变成空白，那还不如让它单调一点。
MIN_EVENTS_PER_SECTION = 3

# 单个媒体在一个版块里最多占几个「单源席位」。
# 线上真实数据：国际版块 8 条里 France 24 独占 5 条，NPR / Sky News 一条没进，
# 配置里 7 个源最后只有 5 家露面。设成 2 之后，小媒体也能稳定露脸。
# 多源事件（source_count >= 2）不受此限制——它们本身就值得靠前。
PUBLISHER_SEAT_QUOTA = 2

# ----------------------------------------------------------------------------
# 同主题归并
# ----------------------------------------------------------------------------
# 问题：线上有一天的国内版块 8 条里有 4 条是「习近平会见X国领导人」，
#       读者打开页面会觉得「今天只有一件事」。
#
# 关键认识：这 4 条是**不同事件**（会见的对象不同），不能像 cluster 那样
# 合并成一个事件——那会丢失信息。它们应该归到**同一主题**下：
#       主卡片：习近平会见四国领导人（AI 概括）
#       展开后：4 条各自的事件
# 于是形成两级结构：theme（同系列事件） → event（单条事件） → cluster（多家报道）。
#
# 识别方式：用「标题公共前缀」而不是相似度。
#   实测这 4 条两两 bigram 相似度只有 0.17–0.23，任何合理阈值都抓不到；
#   但它们的公共前缀都恰好是「习近平会见」（5 字）。
#   前缀规则可预测、可解释，且不会把无关新闻误合并。
THEME_PREFIX_MIN = 5      # 公共前缀至少这么长才算同主题
THEME_MIN_EVENTS = 2      # 至少这么多条才成组（1 条不成组）

# 每批翻译的标题数量：一次给 20 条模型只会翻前 5 条（实测），必须分批 8 条
TRANSLATE_BATCH = 8

# 聚类相似度阈值。
# 0.35 是初版凭经验定的值，实测太保守：在真实抓取数据上，6 组「明显同一事件、
# 不同媒体不同措辞」的标题对里只有 1 组能达到 0.35，导致 11 条国际大事中
# 只有 1 条是多源簇——「看不同观点」这个核心目标基本落空。
# 用真实标题对做阈值扫描后：0.25 时同事件能合并 3/6，且不同事件误并为 0/6，
# 是准确率与召回率的最佳平衡点。
CLUSTER_THRESHOLD = 0.25

# 单个源抓取超时（秒）
SOURCE_TIMEOUT = 20

# 抓取用的 UA：部分站点会拒绝 python-urllib 默认 UA
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 daily-news-bot"
)

# TTS 音色与重试次数。
#
# 重试次数必须给足：实测 edge-tts 的失败是**服务端随机抖动**，与文本长度无关
# （同一段文字可能连续 5 次全失败，也可能连续成功）。单次成功率约 20%–60%，
# 用「8 次重试 + 线性退避」实测可达 5/5 成功，平均只需 2 次尝试。
# 早期版本只重试 3 次，线上真的出现过 3 次全挂、当天没有音频的情况。
TTS_VOICE = "zh-CN-XiaoxiaoNeural"
TTS_ATTEMPTS = 8

# 站点地址（用于页面里的绝对链接与分享信息）；可在环境变量里覆盖
SITE_URL = os.environ.get("SITE_URL", "https://daily-news.pages.dev").rstrip("/")

# ----------------------------------------------------------------------------
# 1.1 新闻源
# ----------------------------------------------------------------------------
# 国际源：观点各异，保留原始多元化，全部实测可用。
# ❌ 勿添加：CBC（经常超时）、NHK（HTTP 错误）、UN News（XML 解析失败）。
# scope="intl" 表示这个源整源都是国际新闻，抓到的条目一律进国际版块。
INTL_FEEDS = [
    {"name": "BBC", "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
     "scope": "intl"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss",
     "scope": "intl"},
    # 半岛电视台：提供与非西方媒体不同的视角，重点保留
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml",
     "scope": "intl"},
    {"name": "France 24", "url": "https://www.france24.com/en/rss",
     "scope": "intl"},
    {"name": "NPR", "url": "https://feeds.npr.org/1004/rss.xml",
     "scope": "intl"},
    {"name": "Sky News", "url": "https://feeds.skynews.com/feeds/rss/world.xml",
     "scope": "intl"},
    {
        "name": "Google News",
        "url": (
            "https://news.google.com/rss/headlines/section/topic/WORLD"
            "?hl=en-US&gl=US&ceid=US:en"
        ),
        "scope": "intl",
    },
]

# ----------------------------------------------------------------------------
# 源级分类声明（这是「国内版块混进国外新闻」的根本解法）
# ----------------------------------------------------------------------------
# 问题根源：
#   中新网的「即时」「要闻」「社会」频道**不是纯国内新闻**——它把国际稿
#   混在里面。实测 106 条中新网条目里有 38 条完全不涉华
#   （「韩国高考防作弊升级」「日本隼鸟2号探测器」「欧盟经济增长好于预期」）。
#   只靠「源属于谁」决定版块，必然泄漏。
#
# 两种解法，本方案用第二种：
#   ❌ 事后按内容猜（is_china_related）——猜错就泄漏，且中新网条目从来
#      不在「需要检查」的名单里，根本没被检查过。
#   ✅ 源级声明 scope，再对 scope="mixed" 的源做内容判定。
#
# scope 取值：
#   "domestic" 整源都是国内新闻（理论上不该混国际新闻）
#   "intl"     整源都是国际新闻
#   "mixed"    综合媒体，需要按内容逐条判定（中新网各频道、法广、纽时中文）
#
# 每条源都必须声明 scope，smoke_test 会强制检查，漏写会直接失败。

# 国内热点源：中文，零翻译成本。
# 写成配置项而不是硬编码在逻辑里，某个源哪天挂了，改这里就行，不用动逻辑。
#
# 本表为 2026-11 实测结论（境外出口 + 中国大陆直连双向实测），改源前请先读这几条：
#   ✅ 中新网系列：唯一「境外可访问 + 稳定 200 + 条目新鲜 + 时间字段齐全」的官方源。
#      各频道互不重复，合计约 150 条/轮，足以撑起国内版块。
#      ⚠️ 但它们是 **mixed**：各频道都混有国际新闻，必须逐条判定。
#   ❌ 联合早报官方 RSS 已下线（.sg 域名返回 SPA 网页，直连 404），不要再用。
#   ❌ 人民网 / 新华网 / 新浪 RSS 全部停更（最新条目停在 2016–2025 年），
#      形式上是合法 RSS、bozo=False、条目上百条，会静默喂进一堆旧闻——这是本项目最大的坑。
#      已在下方的「停更源守卫」里做拦截，新增源也受保护。
#   ❌ 央视 RSS 全部 404；rsshub.app 公共实例已被 Cloudflare 拦截（403）。
#   ⚠️ BBC 中文 simp 会被 302 到 trad（拿到繁体），且多次实测超 20s 超时，故不采用。
DOMESTIC_FEEDS = [
    # 中新网：官方源，境外实测畅通、0.1s 级响应、30 条/源、时间字段完整。
    # publisher 字段是关键：这些频道同属「中新网」一家媒体，
    # 同一家媒体的不同频道绝不能互相充数成「多家媒体报道」。
    # scope="mixed"：频道内容里混有国际新闻，需逐条判定是否涉华。
    {"name": "中新网·即时", "url": "https://www.chinanews.com.cn/rss/scroll-news.xml",
     "publisher": "中新网", "scope": "mixed"},
    {"name": "中新网·要闻", "url": "https://www.chinanews.com.cn/rss/importnews.xml",
     "publisher": "中新网", "scope": "mixed"},
    {"name": "中新网·时政", "url": "https://www.chinanews.com.cn/rss/china.xml",
     "publisher": "中新网", "scope": "mixed"},
    {"name": "中新网·社会", "url": "https://www.chinanews.com.cn/rss/society.xml",
     "publisher": "中新网", "scope": "mixed"},
    # ⚠️ 注意：`中新网·国际`（world.xml）**刻意不放在这里**。
    # 它是国际新闻频道，放进国内源会导致「伊朗油轮触雷」这类新闻出现在
    # 「国内热点」版块里（线上真的发生过）。若需要中文国际新闻，
    # 应该按 publisher 归到国际版块，而不是混进国内。
    # 联合早报（第三方镜像，非官方）：唯一实测「有图 + 有全文」的中文源，用来给卡片配图。
    # 它是公益镜像，随时可能变慢或失效，所以只做补充；失败会被静默跳过，不影响主流程。
    {"name": "联合早报", "url": "https://plink.anyfeeder.com/zaobao/realtime/china",
     "publisher": "联合早报", "scope": "mixed"},
]

# 海外中文媒体：用中文报道中国与世界的媒体，是补充「不同观点」的主力。
#
# 为什么必须补这批源：
#   体检线上数据发现，国内版块 8 条事件全部来自「中新网」一家媒体，多源簇为 0。
#   而「看到不同观点」正是这个项目的核心目标之一。配额机制只能防垄断，
#   不能凭空创造多样性——必须真的有多家媒体。
#
# 这三个源都用美国出口（≈GitHub Actions 网络环境）实测过：
#   ✅ 纽约时报中文网  20 条、最新当天、15/15 有配图、简体、大量涉华深度报道
#   ✅ 法广 RFI 中文    30 条、最新当天、15/15 有配图、简体、涉华议题丰富
#   ❌ 端传媒          连不上（RemoteDisconnected，美国出口也失败，换代理再试仍失败）
#
# ⚠️ BBC 中文**刻意不采用**：虽然境外可访问、42 条、有图，
#    但实测它返回的是**100% 繁体**内容（「特朗普訪華之際 美國封堵中國公司」），
#    本站面向简体读者，混入繁体体验很差。宁缺毋滥。
#
# ⚠️ 德国之声中文可用（55 条、简体、当天）但 **0/15 没有任何配图**，
#    卡片会变成纯文字，与其他版块观感不一致，故暂不加入。
#    若以后能接受无图卡片，可以打开下面被注释的那一行。
# ⚠️ 这两个源都是 **mixed**：它们用中文报道中国与世界，
#    既有涉华深度报道，也有纯国际新闻（「乌克兰袭击克里米亚大桥」）。
#    必须逐条判定，不能整源归入国内版块。
OVERSEAS_ZH_FEEDS = [
    {"name": "纽约时报中文网", "url": "https://cn.nytimes.com/rss/",
     "publisher": "纽约时报中文网", "scope": "mixed"},
    {"name": "法广RFI", "url": "https://www.rfi.fr/cn/rss",
     "publisher": "法广RFI", "scope": "mixed"},
    # {"name": "德国之声中文", "url": "https://rss.dw.com/rdf/rss-chi-all",
    #  "publisher": "德国之声", "scope": "mixed"},
]

# ----------------------------------------------------------------------------
# 热榜源：让「热度」而不是「发布时间」驱动排序
# ----------------------------------------------------------------------------
# 为什么需要热榜源（项目最核心的错配）：
#   普通 RSS 告诉你「媒体按时间发了什么」，是流水账；
#   用户要的是「大家此刻在关注什么」，是热点。
#   实测国内版块常年是「某国领导人抵达深圳」这类流水账，
#   因为排序只看发布时间，谁发得新谁排前面。
#
# 实测结论（2026-11-18，美国出口代理，连续 3 次全部成功）：
#   ✅ 澎湃精选 rsshub.ktachibana.party/thepaper/featured
#       21 条、编辑精选的真正热点（习近平会见普京、APEC、社会案件），质量最高
#   ✅ 虎嗅精选 20 条（商业科技为主，标题带大段正文，质量中等）
#   ✅ 36氪快讯 20 条（纯财经快讯）
#   ❌ 百度/微博/头条热搜：rsshub.app 403、其他实例 404/503/SSL 错误
#   ❌ 知乎热榜：虽然能通，但是问答形式且娱乐化（NBA/仅退款/游戏），不适合新闻站
#
# 所以只加澎湃精选一个热榜源，作为「热度信号」——它的条目在排序时获得加权。
# ⚠️ 它是 RSSHub 公共实例，有严格限流：并发请求会 429，
#    抓取时必须串行 + 间隔（collect_all 对 HOT_FEEDS 单独串行处理）。
HOT_FEEDS = [
    {"name": "澎湃精选", "url": "https://rsshub.ktachibana.party/thepaper/featured",
     "publisher": "澎湃新闻", "scope": "mixed", "is_hot": True},
]

# 热榜条目在排序时获得的加权分（相当于「多 2 家媒体报道」的分量）
HOT_BOOST = 2

# RSSHub 公共实例限流：每次请求后的等待秒数
HOT_FEED_DELAY = 4

# ----------------------------------------------------------------------------
# 中文源探测结论（2026-11 第二轮实测，中国大陆直连，20 秒超时）
# ----------------------------------------------------------------------------
# 为什么记在这：国内版块只有中新网一家是当前最大的产品短板，但补源时反复踩坑。
# 下面这些结论可以直接省掉一轮试错。
#
# ✅ 可用且适合：
#     中新网各频道（官方，境外畅通，无图）
#     联合早报（第三方镜像 plink.anyfeeder.com，境外约 6 秒，**有真实配图**）
#
# ⚠️ 可用但**不适合「国内热点」**：
#     界面新闻 https://a.jiemian.com/index.php?m=article&a=rss
#       —— 本机可访问、30 条、时间新鲜、bozo=False，但它是**财经媒体**：
#          内容以公司公告/IPO/股价为主（"某公司拟回购股份"），
#          放进「国内热点」会严重稀释时政与社会新闻。而且 RSS 里**没有任何图片字段**。
#
# ❌ 本机（中国大陆）全部超时，但在 GitHub Actions（境外）**很可能可用**，
#    值得在线上试一次再决定（这些是补多样性的最佳候选）：
#     端传媒        https://theinitium.com/feed/
#     纽约时报中文网 https://cn.nytimes.com/rss/
#     德国之声中文   https://rss.dw.com/rdf/rss-chi-all
#     法广 RFI 中文  https://www.rfi.fr/cn/rss
#     BBC 中文       https://feeds.bbci.co.uk/zhongwen/simp/rss.xml
#        （注意：simp 会被 302 到 trad，实际拿到繁体）
#     关键评论网     https://www.thenewslens.com/feed
#   验证方法：在 Actions 里跑 `python scripts/live_check.py`，看「国内源」那一节的
#   状态表——状态为「正常」的就是境外可用，直接加进 DOMESTIC_FEEDS 即可。
#
# ❌ 确定不可用，别再试：
#     RSSHub 公共实例（rsshub.app 403 / rssforever 503）
#     中国日报中文 https://cn.chinadaily.com.cn/rss/china_rss.xml（404）
#     联合早报官方 RSS（已下线，.sg 返回 SPA 网页）
#     人民网 / 新华网 / 新浪（全部停更数年，是「僵尸源」）

# 停更源守卫：一个源的最新条目若比「现在」还老这么多天，判定为僵尸源并整源丢弃。
# 这条守卫是实测踩坑的产物——人民网/新华网/新浪都会返回 HTTP 200、bozo=False、
# 上百条条目，但内容停在几年前，不拦截就会静默把旧闻当新闻发出去。
STALE_SOURCE_DAYS = 7

# 聚合器来源：簇内有具体媒体时不计票；单独成簇时才计 1 家
AGGREGATOR_SOURCES = {"Google News"}

# 判断「涉华报道」用的关键词。
# 英文那组必须用词边界匹配（否则 "us" 会命中 "business"）；
# 中文那组直接子串匹配即可（中文没有词边界问题）。
CHINA_KEYWORDS_EN = [
    "china", "chinese", "beijing", "shanghai", "xi jinping",
    "hong kong", "taiwan", "shenzhen", "guangzhou", "ccp",
    "sino", "renminbi", "yuan", "prc",
]

CHINA_KEYWORDS_ZH = [
    # ---- 国家与官方机构 ----
    "中国", "中方", "我国", "北京", "上海", "习近平", "中共", "国务院",
    "内地", "大陆", "人民币", "对外",
    # 部委与官方身份（「王毅会见加拿大外长」这种必须算中国新闻）
    "外交部", "商务部", "工信部", "发改委", "央行", "解放军", "国防部",
    "教育部", "文旅部", "财政部", "农业农村部", "生态环境部", "交通运输部",
    "外长", "副总理", "国务委员", "发言人", "主席", "总理", "人大代表", "政协",
    # 常见中国领导人/高官姓名
    "王毅", "李强", "赵乐际", "丁薛祥", "韩正", "王沪宁", "蔡奇", "李希",
    # ---- 港澳台 ----
    "香港", "台湾", "澳门",
    # ---- 重要城市与省份（中新网大量地方新闻）----
    "深圳", "广州", "天津", "重庆", "杭州", "南京", "武汉", "成都", "西安",
    "昆明", "长春", "哈尔滨", "沈阳", "济南", "郑州", "长沙", "合肥",
    "福州", "厦门", "南昌", "贵阳", "南宁", "海口", "兰州", "西宁",
    "云南", "广东", "江苏", "浙江", "山东", "河南", "河北", "湖南", "湖北",
    "四川", "福建", "安徽", "江西", "陕西", "甘肃", "青海", "贵州", "广西",
    "辽宁", "吉林", "黑龙江", "山西", "海南", "宁夏", "新疆", "西藏", "内蒙古",
    # ---- 对华关系简称 ----
    "涉华", "访华", "对华", "在华", "赴华", "来华", "对华政策",
    "中加", "中美", "中俄", "中欧", "中日", "中韩",
    "中印", "中澳", "中英", "中法", "中德", "中越", "中菲", "中拉",
    "中印尼", "中新", "中泰", "中马", "中巴", "中以", "中伊", "中沙",
    # ---- 中国主办/深度参与的国际场合 ----
    "APEC", "亚太经合", "二十国集团", "G20", "金砖", "上合组织", "博鳌",
    "进博会", "广交会", "服贸会", "共建“一带一路”", "一带一路",
    # ---- 中国特有议题 ----
    # 注意：不要放「高考」「春运」「全国」这类词——
    #   别国也有高考（实测「韩国高考防作弊」因含「高考」被误判成中国新闻）；
    #   「全国」过于宽泛，任何国家新闻都可能用。
    # 只保留真正中国独有的表述。
    "脱贫", "乡村振兴", "十四五", "十五五", "两会", "双一流",
    "国企", "央企", "A股", "沪深", "港股", "人民币汇率",
    # ---- 中国航天/重大科技工程（实测「神舟二十四号」因不含地名被误判）----
    "神舟", "嫦娥", "天宫", "天问", "北斗", "长征五号", "长征二号",
    "空间站", "航天员", "载人航天", "探月工程", "火星探测",
    "国家航天局", "中国航天", "酒泉", "西昌", "文昌", "太原卫星",
    # ---- 中国特有文化/社会现象（中新网大量此类稿件）----
    "妈祖", "长征胜利", "抗战胜利", "非遗", "中医药", "紫禁城", "故宫",
    "兵马俑", "敦煌", "长城", "春节", "中秋", "端午", "元宵",
]

# 兼容旧名字（退化方案那处代码在用）
CHINA_KEYWORDS = CHINA_KEYWORDS_EN

# ----------------------------------------------------------------------------
# 1.2 软新闻过滤词（必须用词边界匹配，否则 "software" 会被 "war" 误杀）
# ----------------------------------------------------------------------------
SOFT_NEWS_WORDS_EN = [
    # 娱乐 / 明星
    "celebrity", "celebrities", "showbiz", "box office", "movie star",
    "music award", "grammy", "grammys", "oscars", "oscar", "eurovision",
    "reality tv", "soap opera", "film festival", "red carpet", "talent show",
    "influencer", "royal baby",
    # 体育
    "football", "soccer", "basketball", "baseball", "cricket", "tennis",
    "golf", "olympic", "olympics", "fifa", "uefa", "premier league",
    "champions league", "nba", "nfl", "world cup", "formula 1", "grand prix",
    "tour de france", "wimbledon", "super bowl",
    # 奇趣 / 生活
    "horoscope", "crossword", "recipe", "viral video", "cat stuck",
    "zoo escape", "lottery winner", "weather forecast",
]

SOFT_NEWS_WORDS_ZH = [
    "娱乐", "明星", "综艺", "球赛", "球星", "演唱会", "八卦", "奇闻",
    "网红", "票房", "剧集", "热搜第一", "综艺节目", "选秀",
]

# ----------------------------------------------------------------------------
# 1.3 国家/地区实体别名表（防编造对账用，30–50 个，含简称）
#     英文别名走词边界匹配（"eu" 不会命中 "Depardieu"），中文别名走子串匹配。
# ----------------------------------------------------------------------------
COUNTRY_ALIASES = [
    ("美国", ["美国", "美方", "白宫", "united states", "U.S.", "US", "usa", "america", "american", "washington"]),
    ("中国", ["中国", "中方", "北京", "china", "chinese", "beijing", "xi jinping"]),
    ("俄罗斯", ["俄罗斯", "俄方", "莫斯科", "russia", "russian", "moscow", "putin"]),
    ("乌克兰", ["乌克兰", "基辅", "ukraine", "ukrainian", "kyiv", "kiev", "zelensky"]),
    ("以色列", ["以色列", "特拉维夫", "israel", "israeli", "tel aviv", "netanyahu"]),
    ("巴勒斯坦", ["巴勒斯坦", "加沙", "palestine", "palestinian", "gaza", "west bank"]),
    ("伊朗", ["伊朗", "德黑兰", "iran", "iranian", "tehran"]),
    ("伊拉克", ["伊拉克", "巴格达", "iraq", "iraqi", "baghdad"]),
    ("叙利亚", ["叙利亚", "大马士革", "syria", "syrian", "damascus"]),
    ("黎巴嫩", ["黎巴嫩", "贝鲁特", "lebanon", "lebanese", "beirut", "hezbollah"]),
    ("也门", ["也门", "yemen", "yemeni", "houthi", "houthis"]),
    ("沙特阿拉伯", ["沙特", "saudi", "saudi arabia", "riyadh"]),
    ("阿联酋", ["阿联酋", "迪拜", "UAE", "emirates", "dubai", "abu dhabi"]),
    ("卡塔尔", ["卡塔尔", "多哈", "qatar", "doha"]),
    ("土耳其", ["土耳其", "安卡拉", "turkey", "turkish", "ankara", "erdogan"]),
    ("埃及", ["埃及", "开罗", "egypt", "egyptian", "cairo"]),
    ("利比亚", ["利比亚", "libya", "libyan", "tripoli"]),
    ("苏丹", ["苏丹", "sudan", "sudanese", "khartoum"]),
    ("索马里", ["索马里", "somalia", "somali", "mogadishu"]),
    ("尼日利亚", ["尼日利亚", "nigeria", "nigerian", "lagos", "abuja"]),
    ("肯尼亚", ["肯尼亚", "kenya", "kenyan", "nairobi"]),
    ("埃塞俄比亚", ["埃塞俄比亚", "ethiopia", "ethiopian", "addis ababa"]),
    ("南非", ["南非", "south africa", "south african", "johannesburg", "cape town"]),
    ("刚果", ["刚果", "congo", "congolese", "kinshasa"]),
    ("印度", ["印度", "新德里", "india", "indian", "delhi", "modi", "mumbai"]),
    ("巴基斯坦", ["巴基斯坦", "伊斯兰堡", "pakistan", "pakistani", "islamabad", "karachi"]),
    ("阿富汗", ["阿富汗", "喀布尔", "afghanistan", "afghan", "kabul", "taliban"]),
    ("孟加拉国", ["孟加拉", "bangladesh", "bangladeshi", "dhaka"]),
    ("朝鲜", ["朝鲜", "平壤", "north korea", "north korean", "pyongyang", "kim jong un"]),
    ("韩国", ["韩国", "首尔", "south korea", "south korean", "seoul"]),
    ("日本", ["日本", "东京", "japan", "japanese", "tokyo"]),
    ("台湾", ["台湾", "台北", "taiwan", "taiwanese", "taipei"]),
    ("香港", ["香港", "hong kong"]),
    ("菲律宾", ["菲律宾", "马尼拉", "philippines", "filipino", "manila"]),
    ("越南", ["越南", "河内", "vietnam", "vietnamese", "hanoi"]),
    ("泰国", ["泰国", "曼谷", "thailand", "thai", "bangkok"]),
    ("缅甸", ["缅甸", "仰光", "myanmar", "burma", "yangon"]),
    ("印度尼西亚", ["印尼", "印度尼西亚", "indonesia", "indonesian", "jakarta"]),
    ("马来西亚", ["马来西亚", "malaysia", "malaysian", "kuala lumpur"]),
    ("新加坡", ["新加坡", "singapore"]),
    ("澳大利亚", ["澳大利亚", "澳洲", "australia", "australian", "sydney", "canberra"]),
    ("新西兰", ["新西兰", "new zealand", "wellington", "auckland"]),
    ("巴布亚新几内亚", ["巴布亚新几内亚", "巴新", "papua new guinea", "png"]),
    ("斐济", ["斐济", "fiji", "suva"]),
    ("柬埔寨", ["柬埔寨", "金边", "cambodia", "cambodian", "phnom penh"]),
    ("老挝", ["老挝", "万象", "laos", "lao", "vientiane"]),
    ("蒙古", ["蒙古", "乌兰巴托", "mongolia", "mongolian", "ulaanbaatar"]),
    ("尼泊尔", ["尼泊尔", "加德满都", "nepal", "nepalese", "kathmandu"]),
    ("斯里兰卡", ["斯里兰卡", "科伦坡", "sri lanka", "colombo"]),
    ("英国", ["英国", "伦敦", "britain", "british", "UK", "U.K.", "london", "england"]),
    ("法国", ["法国", "巴黎", "france", "french", "paris", "macron"]),
    ("德国", ["德国", "柏林", "germany", "german", "berlin"]),
    ("意大利", ["意大利", "罗马", "italy", "italian", "rome"]),
    ("西班牙", ["西班牙", "马德里", "spain", "spanish", "madrid"]),
    ("荷兰", ["荷兰", "netherlands", "dutch", "amsterdam", "the hague"]),
    ("波兰", ["波兰", "poland", "polish", "warsaw"]),
    ("匈牙利", ["匈牙利", "hungary", "hungarian", "budapest"]),
    ("欧盟", ["欧盟", "欧洲联盟", "european union", "EU", "brussels", "european commission"]),
    ("加拿大", ["加拿大", "canada", "canadian", "ottawa", "toronto"]),
    ("墨西哥", ["墨西哥", "mexico", "mexican", "mexico city"]),
    ("巴西", ["巴西", "brazil", "brazilian", "brasilia", "sao paulo"]),
    ("阿根廷", ["阿根廷", "argentina", "argentine", "buenos aires"]),
    ("委内瑞拉", ["委内瑞拉", "venezuela", "venezuelan", "caracas"]),
    ("智利", ["智利", "chile", "chilean", "santiago"]),
    ("哥伦比亚", ["哥伦比亚", "colombia", "colombian", "bogota"]),
    ("秘鲁", ["秘鲁", "peru", "peruvian", "lima"]),
    ("海地", ["海地", "haiti", "haitian", "port-au-prince"]),
    ("古巴", ["古巴", "cuba", "cuban", "havana"]),
    ("塞尔维亚", ["塞尔维亚", "serbia", "serbian", "belgrade"]),
    ("科索沃", ["科索沃", "kosovo"]),
    ("格鲁吉亚", ["格鲁吉亚", "georgia", "tbilisi"]),
    ("亚美尼亚", ["亚美尼亚", "armenia", "armenian", "yerevan"]),
    ("阿塞拜疆", ["阿塞拜疆", "azerbaijan", "baku"]),
    ("白俄罗斯", ["白俄罗斯", "belarus", "belarusian", "minsk"]),
    ("哈萨克斯坦", ["哈萨克斯坦", "kazakhstan", "astana"]),
    ("瑞士", ["瑞士", "switzerland", "swiss", "geneva", "zurich"]),
    ("挪威", ["挪威", "norway", "norwegian", "oslo"]),
    ("瑞典", ["瑞典", "sweden", "swedish", "stockholm"]),
    ("希腊", ["希腊", "greece", "greek", "athens"]),
]

# 中文源判定
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# ----------------------------------------------------------------------------
# 1.4 AI 提示词（措辞实测有效，只把「编号」换成稳定 cid）
# ----------------------------------------------------------------------------

TITLE_TRANSLATE_PROMPT = """把下列英文新闻标题逐条翻译成简体中文。

输出格式（严格遵守，每个标题一行，不得有多余内容）：
[编号] 中文译文

禁止事项：
- 不要输出任何开场白（如「以下是」「我将为您」「好的」等）
- 不要把英文原文再抄一遍
- 不要输出解释、说明、总结或思考过程
- 不要漏掉任何一条，编号必须与输入对应

翻译要求：
- 人名、地名、机构名用中文媒体常用译法；无通用译法可保留原文
- 数字忠实原文：hundreds of thousands→数十万，dozens→数十

输出示例（只输出这两行，不要任何其他文字）：
[1] 南非同日发生两起大规模枪击致至少17人死亡
[2] 超级油轮在霍尔木兹海峡触雷后沉没

现在开始翻译这 {count} 条：
{items}
"""

# 当日大事总结：本方案用稳定 cid 代替数组下标，杜绝摘要与链接错位。
EVENTS_SUMMARY_PROMPT = """任务：把下面来自多家{scope}媒体的新闻标题，合并去重后总结成中文的「今日{scope}大事」。

硬性规则：
1. 只输出 5 到 8 条，每条一行。
2. 每行格式：[编号] 中文事件概括——中文重要性说明
   编号是下方列表方括号里的稳定编号（如 c1、c7），必须原样照抄，用方括号括起来。
3. 每条内容只能来自下面给出的标题，禁止编造标题里没有的事件、人物或时间。
4. 概括不超过 40 个汉字，重要性说明不超过 15 个汉字。
5. 除列表本身外，不要输出任何开场白、结尾、标题或解释。
6. 人名、地名、国家名必须忠实原文，不要替换成别的国家或人物。
7. 数量必须忠实原文，不要夸大。
8. 优先选择多国媒体共同报道的事件。标【3家媒体报道】的最重要，其次【2家】。
9. 多条标题讲同一件事时合并成一条。

输出示例：
[c13] 南非同日发生两起大规模枪击致至少17人死亡——当地治安形势严峻
[c7] 一艘载油200万桶的超级油轮在霍尔木兹海峡触雷沉没——或冲击国际油价

现在，请处理以下 {count} 条新闻标题：
{items}
"""

FULLTEXT_TRANSLATE_PROMPT = """把下面这篇英文新闻翻译成简体中文。

输出要求：
- 只输出中文译文正文，不要任何开场白、说明或结尾
- 不要把英文原文抄一遍
- 保持段落结构，段落之间空一行
- 完整翻译，不省略、不概括、不加评论
- 人名地名机构名用中文常用译法；数字、日期、引语忠实原文

文章标题：{title}

英文正文：
{body}
"""

BROADCAST_PROMPT = """你是一名电台早间新闻主播。请把下面这些新闻要点，改写成一段适合朗读的中文播报稿。

要求：
- 开头用「各位好，今天是{date}，欢迎收听今日国际大事。」引入
- 把要点串联成连贯的段落，不要用 1.2.3. 编号
- 口语化、句子短、便于朗读，不要出现括号、破折号等符号
- 不要添加要点里没有的信息
- 总长度控制在 {max_chars} 字以内
- **必须在字数上限内写完整，最后一句话要有完整结尾**（比如「以上就是今天的要闻」），
  绝对不能写到一半停住
- 要点很多时，只需覆盖最重要的事，不必逐条罗列
- 只输出播报稿正文，不要任何说明

新闻要点（共 {point_count} 条，不必全部覆盖）：
{points}
"""

# 单条事件重做（国家实体对账失败、往回打 AI 时用）
EVENT_REDO_PROMPT = """任务：根据下面这几条同一事件的新闻报道，写一条中文事件概括。

硬性规则：
1. 只输出一行，格式：[编号] 中文事件概括——中文重要性说明
   编号必须原样照抄下面的编号。
2. 概括不超过 40 个汉字，重要性说明不超过 15 个汉字。
3. 只能使用下面标题里出现过的国家、人物、地点和数字，禁止编造。
4. 除这一行外不要输出任何其他内容。
5. 如果下面标题里提到的国家只有一两个，概括里就只提这一两个国家，不要添加别的国家。

编号：{cid}
标题：
{items}
"""


# ============================================================================
# 二、通用小工具
# ============================================================================

def log(msg):
    """统一的日志输出（全中文，带时间戳）。"""
    print("[%s] %s" % (datetime.now(BJ_TZ).strftime("%H:%M:%S"), msg), flush=True)


def warn(msg):
    """警告输出（GitHub Actions 里会显示成黄色提示）。"""
    print("[%s] ⚠️  %s" % (datetime.now(BJ_TZ).strftime("%H:%M:%S"), msg), flush=True)


def today_bj():
    """北京时间的今天（只取日期）。"""
    return datetime.now(BJ_TZ).date()


def now_bj():
    """北京时间的当前时刻。"""
    return datetime.now(BJ_TZ)


def parse_date(value):
    """把 'YYYY-MM-DD' 解析成 date；解析不了返回 None。"""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def to_bj(dt):
    """把任意 aware/naive datetime 转成北京时间。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJ_TZ)


# ----------------------------------------------------------------------------
# 2.1 词边界匹配（软新闻过滤的关键，否则 "software" 会被 "war" 误杀）
# ----------------------------------------------------------------------------

def _word_pattern(word):
    """把关键词编译成「前后不能是字母数字」的正则。

    用前后断言而不是 \\b：\\b 对 "u.s." 这类含点的缩写不可靠。
    """
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


# 关键词正则只编译一次
_SOFT_EN_PATTERNS = [_word_pattern(w) for w in SOFT_NEWS_WORDS_EN]


def contains_word(text, word):
    """判断 word 是否作为一个完整的词出现在 text 里。

    反例（必须返回 False）：
      contains_word("Gerard Depardieu went on trial", "eu")  → False（"Depardieu" 里没有独立的 eu）
      contains_word("New software released", "war")         → False（"software" 里的 war 不是独立词）
    """
    if not text or not word:
        return False
    return _word_pattern(word).search(text) is not None


def is_soft_news(title):
    """判断标题是否属于娱乐/体育/奇趣等软新闻，需要剔除。"""
    if not title:
        return False
    for pattern in _SOFT_EN_PATTERNS:
        if pattern.search(title):
            return True
    for word in SOFT_NEWS_WORDS_ZH:
        if word in title:
            return True
    return False


# ----------------------------------------------------------------------------
# 2.2 国家实体识别
# ----------------------------------------------------------------------------

def _alias_is_ascii(alias):
    """别名是否全为 ASCII（决定用词边界匹配还是子串匹配）。"""
    return all(ord(ch) < 128 for ch in alias)


# 这些缩写既可当国家简称、又是极常见的英文单词（us / eu / uk），
# 必须**区分大小写**匹配：只认大写的 US / EU / UK，否则 "tell us" 会被当成"美国"。
# 注意表里这类别名必须直接写成大写形式（US、EU），区分大小写的正则会原样匹配它。
CASE_SENSITIVE_ALIASES = {"US", "EU", "UK", "UAE", "CCP", "U.S.", "U.K.", "U.N."}


def _alias_pattern(alias):
    """把单个别名编译成正则：默认忽略大小写，歧义缩写区分大小写。"""
    flags = 0 if alias in CASE_SENSITIVE_ALIASES else re.IGNORECASE
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])",
        flags,
    )


_COUNTRY_PATTERNS = {}
for _canonical, _aliases in COUNTRY_ALIASES:
    _pat = []
    for _alias in _aliases:
        if _alias_is_ascii(_alias):
            _pat.append(("re", _alias_pattern(_alias)))
        else:
            _pat.append(("sub", _alias))
    _COUNTRY_PATTERNS[_canonical] = _pat


def find_countries(text):
    """从一段文字里找出提到的国家/地区（返回规范名集合）。"""
    found = set()
    if not text:
        return found
    for canonical, patterns in _COUNTRY_PATTERNS.items():
        for kind, pat in patterns:
            if kind == "re":
                if pat.search(text):
                    found.add(canonical)
                    break
            else:
                if pat in text:
                    found.add(canonical)
                    break
    return found


def country_match_report(summary_zh, candidate_texts):
    """把摘要里提到的国家与候选原文标题做对账。

    返回 (matched, unmatched)：matched 是原文里确实出现的国家，
    unmatched 是摘要里提了、但原文标题里找不到的国家（编造嫌疑）。
    """
    mentioned = find_countries(summary_zh)
    if not mentioned:
        return set(), set()
    pool = " \n ".join(t for t in candidate_texts if t)
    pool_countries = find_countries(pool)
    matched = mentioned & pool_countries
    unmatched = mentioned - pool_countries
    return matched, unmatched


# ----------------------------------------------------------------------------
# 2.3 中文处理：bigram 与 Jaccard
# ----------------------------------------------------------------------------

def zh_bigrams(text):
    """把一段中文切成连续两字的 bigram 集合。

    英文/数字/标点一律丢弃，只看汉字，避免中英混排互相干扰。
    """
    grams = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        if len(run) == 1:
            grams.add(run)
        for i in range(len(run) - 1):
            grams.add(run[i:i + 2])
    return grams


def jaccard(set_a, set_b):
    """Jaccard 相似度 = |A∩B| / |A∪B|。两边都空时返回 0。"""
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def text_similarity(text_a, text_b):
    """两段中文的相似度（bigram Jaccard）。"""
    return jaccard(zh_bigrams(text_a), zh_bigrams(text_b))


def chinese_ratio(text):
    """中文字符占比 = 汉字数 / (汉字数 + 拉丁字母数)。

    用于发布闸门的「中文占比 > 60%」校验，防止整站退化成英文站。
    """
    if not text:
        return 0.0
    cjk = len(CJK_RE.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    total = cjk + latin
    return 1.0 if total == 0 else cjk / total


def is_chinese_text(text):
    """粗略判断一段文本是不是中文（汉字占比超过 30%）。"""
    return chinese_ratio(text) > 0.3


# ----------------------------------------------------------------------------
# 2.4 URL 与图片
# ----------------------------------------------------------------------------

def is_valid_url(url):
    """结构化校验一个链接是否可用。

    只做结构与白名单级别的检查（http/https、有域名、不是占位域名）；
    发布前不对每条链接做 HEAD 请求，因为那会让 workflow 又慢又脆。
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc or "." not in parsed.netloc:
        return False
    host = parsed.netloc.lower()
    for bad in ("example.com", "localhost", "127.0.0.1"):
        if bad in host:
            return False
    return True


def normalize_url(url):
    """去掉链接里的追踪参数，保证同一篇文章只出现一次。"""
    if not is_valid_url(url):
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return ""
    drop = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "ocid", "cmpid", "at_medium", "at_campaign", "fbclid", "gclid",
        "CMP", "ns_mchannel", "ns_campaign", "ns_source", "ito",
    }
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in drop]
    query = urlencode(kept)
    return urlunparse((
        parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
        parsed.params, query, "",
    ))


def normalize_image_url(url):
    """图片 URL 归一：升级分辨率、去掉 resize 参数、http 升 https。

    实测规则：
      - 一律 http → https
      - BBC 的 ichef.bbci.co.uk 路径里带尺寸段（240），换成 1024 才够当头条大图
      - Al Jazeera / France 24 的 ?resize=xxx 参数要去掉
      - Guardian 的 ?width= 改成 1200
    """
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    if not u.startswith("https://"):
        return ""

    try:
        parsed = urlparse(u)
    except ValueError:
        return ""
    host = parsed.netloc.lower()
    path = parsed.path

    # BBC：把路径里第一个两位数以上的尺寸段换成 1024
    if "bbci.co.uk" in host or "bbc.com" in host:
        path = re.sub(r"/(\d{2,4})/", "/1024/", path, count=1)

    # 查询参数处理
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    new_pairs = []
    for key, value in pairs:
        low = key.lower()
        if low == "resize":
            continue  # Al Jazeera / France 24：尺寸参数直接丢掉
        if low == "width":
            value = "1200"  # Guardian：宽度拉满
        new_pairs.append((key, value))
    if "theguardian.com" in host and not any(k.lower() == "width" for k, _ in new_pairs):
        new_pairs.append(("width", "1200"))

    return urlunparse((
        "https", parsed.netloc, path, parsed.params, urlencode(new_pairs), "",
    ))


def strip_html(raw):
    """把 HTML 片段抽成纯文本（用 BeautifulSoup，不手写正则硬解）。"""
    if not raw:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", str(raw))
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_image_from_html(raw):
    """从 description 内嵌 HTML 里找第一张 <img src>。"""
    if not raw:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return normalize_image_url(img.get("src"))
    except Exception:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', str(raw), re.IGNORECASE)
        if m:
            return normalize_image_url(m.group(1))
    return ""


# ----------------------------------------------------------------------------
# 2.5 HTML 转义（所有外部文本进 HTML 前必须过一遍）
# ----------------------------------------------------------------------------

def esc(text):
    """HTML 转义。RSS 里的恶意标题不能破坏整页结构。"""
    return html_escape("" if text is None else str(text), quote=True)


def safe_json_for_script(obj):
    """把对象序列化成可安全内嵌到 <script> 的 JSON。

    必须转义 "</"，否则标题里出现 </script> 会提前闭合脚本块。
    """
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


# ============================================================================
# 三、L1 采集
# ============================================================================

def http_get_bytes(url, timeout=SOURCE_TIMEOUT):
    """抓取一个 URL 的原始字节。

    Accept-Encoding: identity 是刻意的：urllib 不会自动解 gzip，
    很多源默认 gzip，不给 identity 会拿到乱码。
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Encoding": "identity",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def collapse_repeated_prefix(text):
    """去掉摘要里重复的开头。

    实测：中新网的 summary 会把 description 与 content 拼在一起，
    变成「中新社香港11月17日电 题：xxx 中新社香港11月17日电 题：xxx」的叠字。
    做法：取前 24 字作为探针，若它在后文再次出现，就从第二次出现处截断。
    """
    text = (text or "").strip()
    if len(text) < 40:
        return text
    probe = text[:24]
    second = text.find(probe, 24)
    if second > 0:
        return text[:second].strip()
    return text


def is_stale_source(items, now=None, max_days=STALE_SOURCE_DAYS):
    """判断一个源是不是「僵尸源」（都返回 HTTP 200，但内容停在几年前）。

    实测踩坑：人民网主流频道停在 2025-06-05，新华网停在 2022 年疫情稿，
    新浪停在 2018 年。它们全都 bozo=False、条目上百条，不拦截就会静默发旧闻。
    """
    if not items:
        return False
    now = now or now_bj()
    times = [it.get("published") for it in items if it.get("published")]
    if not times:
        # 完全没有时间字段的源（如新华网）无法判断时效，按停更处理：
        # 无法排序、无法去重，宁可不要也不能把无法验证的旧闻当新闻发。
        return True
    newest = max(times)
    return (now - to_bj(newest)).days > max_days


def entry_published(entry):
    """取条目的发布时间（UTC aware datetime），取不到返回 None。"""
    import calendar

    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                return datetime.fromtimestamp(
                    calendar.timegm(struct), tz=timezone.utc
                )
            except (ValueError, OverflowError, TypeError):
                continue
    return None


# 明显不是文章配图的图片特征（导航箭头、图标、广告位、二维码、站点通用分享图）。
# 实测教训：
#   1. 中新网正文页**没有 og:image**，页面上全是导航图和广告图，
#      不加过滤会取到 `toparr.png`（导航箭头）当头条大图——取到错图比没有图更糟。
#   2. 联合早报 RSS 第一条给的是站点通用 `social-share.png`，不是文章配图。
NON_ARTICLE_IMAGE_HINTS = (
    # 通用图标类
    "logo", "icon", "avatar", "pixel", "1x1", "blank", "spacer",
    "placeholder", "banner", "advert", "ad_", "/ad/", "qrcode", "qr_code",
    # 实测遇到的具体坏图
    "toparr", "arr.png", "arrow", "btn", "button", "share", "weixin", "weibo",
    "default/", "/nav", "menu", "loading", "transparent",
    # 站点通用社交分享图（不是文章配图）
    "social-share", "social_share", "og-default", "default-og",
)


def looks_like_article_image(url):
    """判断一个图片 URL 是否可能是「文章配图」。

    宁可漏掉一张真图，也不要把导航箭头/广告图放进头条大图位置。

    注意：**不能强制要求 URL 带图片扩展名**。
    实测联合早报的真实文章配图长这样（没有扩展名，但确实是照片）：
        https://cassette.sphdigital.com.sg/image/zaobao/c3f3a6b10f79
    强制要求 .jpg/.png 会把这类 CDN 图全部误杀。
    所以判断以「黑名单 + 长度 + 明显非图片特征」为主。
    """
    if not url:
        return False
    lowered = url.lower()
    if any(hint in lowered for hint in NON_ARTICLE_IMAGE_HINTS):
        return False

    path = lowered.split("?")[0]

    # 明确不是图片的扩展名直接排除
    if path.endswith((".svg", ".ico", ".css", ".js", ".woff", ".woff2", ".ttf")):
        return False

    # 太短的路径多半是站点资源图，不是文章图
    if len(url) < 40:
        return False

    return True


def entry_image(entry):
    """从条目里抽图片，按可靠性依次尝试。

    顺序：media:thumbnail / media:content → enclosure（按 type 过滤音频）
         → description 内嵌 <img>。

    所有候选都要过 looks_like_article_image()：
    实测联合早报 RSS 第一条给的是站点通用 social-share.png，不是文章配图，
    直接拿来当头条大图很难看。
    """
    def pick(url):
        found = normalize_image_url(url or "")
        return found if looks_like_article_image(found) else ""

    # 1) media:thumbnail / media:content
    for key in ("media_thumbnail", "media_content", "media_content_b64"):
        media = entry.get(key)
        if isinstance(media, list):
            for item in media:
                if isinstance(item, dict):
                    url = item.get("url") or ""
                    if url and not str(item.get("type", "")).startswith("audio"):
                        found = pick(url)
                        if found:
                            return found
    # 2) enclosure（必须过滤音频，否则会把 mp3 当图片）
    for encl in entry.get("enclosures") or []:
        if not isinstance(encl, dict):
            continue
        mime = str(encl.get("type") or "")
        if mime.startswith("audio") or mime.startswith("video"):
            continue
        href = encl.get("href") or encl.get("url") or ""
        if href:
            found = pick(href)
            if found:
                return found
    # 3) 兜底：description / content 里的 <img>
    for key in ("summary", "description"):
        found = pick(extract_image_from_html(entry.get(key)))
        if found:
            return found
    contents = entry.get("content")
    if isinstance(contents, list):
        for item in contents:
            if isinstance(item, dict):
                found = pick(extract_image_from_html(item.get("value")))
                if found:
                    return found
    return ""


def entry_summary(entry):
    """取条目摘要纯文本。"""
    for key in ("summary", "description"):
        text = strip_html(entry.get(key))
        if text:
            return text
    contents = entry.get("content")
    if isinstance(contents, list):
        for item in contents:
            if isinstance(item, dict):
                text = strip_html(item.get("value"))
                if text:
                    return text
    return ""


def entry_link(entry):
    """取条目链接。"""
    link = (entry.get("link") or "").strip()
    if link:
        return link
    for item in entry.get("links") or []:
        if isinstance(item, dict) and item.get("rel") in (None, "alternate") and item.get("href"):
            return item["href"].strip()
    return ""


def fetch_source(source, section, http_get=http_get_bytes, timeout=SOURCE_TIMEOUT):
    """抓一个源并解析，返回 (原始条目列表, 错误说明或空串)。

    任何异常都在这里被吃掉并记录，绝不让单个源拖垮整轮。
    """
    import feedparser

    name = source["name"]
    try:
        raw = http_get(source["url"], timeout)
    except urllib.error.HTTPError as exc:
        return [], "HTTP %s" % exc.code
    except urllib.error.URLError as exc:
        return [], "网络错误 %s" % exc.reason
    except TimeoutError:
        return [], "超时 %ss" % timeout
    except Exception as exc:  # noqa: BLE001 单个源任何异常都只记录
        return [], "%s: %s" % (type(exc).__name__, exc)

    if not raw:
        return [], "空响应"

    try:
        parsed = feedparser.parse(raw)
    except Exception as exc:  # noqa: BLE001
        return [], "解析异常 %s" % exc

    entries = list(parsed.get("entries") or [])
    if not entries:
        reason = "解析出 0 条"
        if getattr(parsed, "bozo", False):
            reason += "（XML 畸形：%s）" % str(getattr(parsed, "bozo_exception", ""))[:80]
        return [], reason

    items = []
    publisher = source.get("publisher") or name
    for entry in entries:
        title = strip_html(entry.get("title"))
        url = entry_link(entry)
        if not title or not is_valid_url(url):
            continue
        items.append({
            "source": name,
            # publisher 用于「家数」计票：同一家媒体的多个频道只能算 1 家。
            # 实测教训：中新网 5 个频道都是「中新网」，不分 publisher 会
            # 把同一家媒体的两篇稿子算成「2 家媒体报道」，直接违反多源计票规则。
            "publisher": publisher,
            # scope 在抓取阶段就带上，供后续按源属性分流
            "scope": source.get("scope") or "",
            # 热榜源标记：排序时获得热度加权
            "is_hot": bool(source.get("is_hot")),
            # section 是「源声明的版块」，不等于最终归属；
            # 最终归属由 assign_sections() 按 scope + 内容判定。
            "section": section,
            "title": title,
            "summary": entry_summary(entry),
            "url": normalize_url(url),
            "image": entry_image(entry),
            "published": entry_published(entry),
        })
    if not items:
        return [], "%d 条全部缺标题或链接" % len(entries)
    return items, ""


def collect_all(sources, section, http_get=http_get_bytes, timeout=SOURCE_TIMEOUT):
    """并发抓一组源。返回 (全部条目, 源状态列表)。

    单个源失败只记录不阻断；停更源（僵尸源）整源丢弃并记入运行报告。
    """
    statuses = []
    all_items = []

    def work(source):
        items, error = fetch_source(source, section, http_get=http_get, timeout=timeout)
        return source["name"], items, error

    if not sources:
        return [], statuses

    workers = max(1, min(8, len(sources)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, items, error in pool.map(work, sources):
            # 停更源守卫：HTTP 200 但内容停在几年前，是最容易静默出错的坑
            if items and is_stale_source(items):
                newest = max(
                    (to_bj(it["published"]) for it in items if it.get("published")),
                    default=None,
                )
                newest_text = newest.strftime("%Y-%m-%d") if newest else "无时间字段"
                warn("源 %s 判定为停更源（最新条目 %s），整源丢弃" % (name, newest_text))
                statuses.append({
                    "源": name,
                    "版块": section,
                    "条目数": 0,
                    "状态": "停更源",
                    "说明": "最新条目 %s，超过 %d 天，整源丢弃" % (
                        newest_text, STALE_SOURCE_DAYS),
                })
                continue

            statuses.append({
                "源": name,
                "版块": section,
                "条目数": len(items),
                "状态": "正常" if items else "失败",
                "说明": error,
            })
            if items:
                all_items.extend(items)
            else:
                warn("源 %s 抓取失败：%s" % (name, error))
    return all_items, statuses


# ============================================================================
# 四、L2 规范化
# ============================================================================

def normalize_items(items, now=None, window_hours=WINDOW_HOURS):
    """时间过滤 → 去重 → 软新闻过滤 → 字段归一。

    返回 (保留条目, 统计字典)。
    """
    now = now or now_bj()
    cutoff = now - timedelta(hours=window_hours)
    stats = {
        "原始条数": len(items),
        "超出时间窗": 0,
        "软新闻": 0,
        "链接重复": 0,
        "无时间(保留)": 0,
        "保留条数": 0,
    }
    seen_urls = set()
    kept = []

    for item in items:
        published = item.get("published")
        if published is not None:
            published_bj = to_bj(published)
            if published_bj < cutoff:
                stats["超出时间窗"] += 1
                continue
            item["published"] = published_bj
        else:
            stats["无时间(保留)"] += 1
            item["published"] = now

        url = item.get("url") or ""
        if not is_valid_url(url):
            continue
        if url in seen_urls:
            stats["链接重复"] += 1
            continue
        seen_urls.add(url)

        if is_soft_news(item.get("title")):
            stats["软新闻"] += 1
            continue

        item["lang"] = "zh" if is_chinese_text(item.get("title")) else "en"
        item["image"] = normalize_image_url(item.get("image") or "")
        item["summary"] = collapse_repeated_prefix(
            re.sub(r"\s+", " ", (item.get("summary") or "")).strip()
        )
        item["title"] = re.sub(r"\s+", " ", item.get("title") or "").strip()
        if not item["title"]:
            continue
        kept.append(item)

    # 时间倒序
    kept.sort(key=lambda it: it["published"], reverse=True)
    stats["保留条数"] = len(kept)
    return kept, stats


def is_china_related(item, include_summary=True):
    """判断一条新闻是否与中国相关（用于把综合媒体的条目分到正确版块）。

    背景：法广RFI、纽约时报中文网是**综合媒体**，RSS 里既有中国新闻
    也有纯国际新闻。如果把整个源都塞进「国内热点」，就会出现
    「乌克兰再次袭击克里米亚大桥」出现在国内版块这种荒谬情况
    （线上真实发生过两次）。

    判定依据：标题或摘要里出现中国相关关键词。

    ⚠️ include_summary 参数很重要（线上第二次泄漏的根因）：
        2026-11-18 线上又出现「乌克兰再次袭击克里米亚大桥」在国内版块。
        追查发现：分流时标题判定正确（False），但该条经过 L5 深挖后
        回填了中文译文 summary_zh，其中顺带提到
        「普京即将与**中美**两国领导人会晤」——「中美」命中关键词，
        使这条乌克兰新闻被误判为涉华。
        实际上「这条新闻讲什么」看标题就够了，摘要里的顺带提及不代表主题。
        所以分流调用必须传 include_summary=False，只看标题。
    """
    parts = [
        item.get("title") or "",
        item.get("title_zh") or "",
    ]
    if include_summary:
        parts.append(item.get("summary") or "")
    blob = " ".join(parts)
    if not blob.strip():
        return False
    # 中文关键词直接子串匹配
    for word in CHINA_KEYWORDS_ZH:
        if word in blob:
            return True
    # 英文关键词用词边界匹配（"us" 不能命中 "business"）
    for word in CHINA_KEYWORDS_EN:
        if contains_word(blob, word):
            return True
    return False


def assign_sections(items):
    """按「源声明的 scope + 内容判定」把条目分到国内/国际版块。

    这是「国内版块混进国外新闻」的根本解法。

    为什么必须按 scope 而不是按 publisher 白名单：
        早期实现维护一份「哪些 publisher 是综合媒体」的名单，
        结果中新网根本不在名单里——而中新网的「即时/要闻/社会」频道
        恰恰混着大量国际新闻（实测 106 条里 38 条不涉华）。
        只要漏掉一个源就泄漏，这是名单式方案的必然缺陷。

    规则（按 scope）：
        scope == "intl"      整源国际新闻 → 一律进国际版块
        scope == "domestic"  整源国内新闻 → 一律进国内版块
        scope == "mixed"     综合媒体 → 逐条判断是否涉华
        其他/缺失            保守按内容判断（宁可多判一次，也不放国际新闻进国内）

    返回 (国内条目, 国际条目)，一一对应输入，不重不漏。
    """
    domestic = []
    international = []
    for item in items:
        scope = (item.get("scope") or "").strip()
        clone = dict(item)

        if scope == "intl":
            clone["section"] = "international"
            international.append(clone)
        elif scope == "domestic":
            clone["section"] = "domestic"
            domestic.append(clone)
        else:
            # mixed 或未声明：按内容判定。
            # include_summary=False：只看标题，不看摘要——摘要里的顺带提及
            # 不代表新闻主题（线上泄漏根因，见 is_china_related 的注释）
            if is_china_related(item, include_summary=False):
                clone["section"] = "domestic"
                domestic.append(clone)
            else:
                clone["section"] = "international"
                international.append(clone)
    return domestic, international


def route_overseas_items(items, overseas_publishers):
    """兼容旧接口：等价于 assign_sections（后者不再依赖 publisher 名单）。

    保留这个函数是为了不破坏已有调用与测试；新代码请直接用 assign_sections。
    """
    return assign_sections(items)


def filter_china_related(items):
    """从国际源里筛出涉华报道（国内源全挂时的退化方案）。"""
    out = []
    for item in items:
        blob = (item.get("title") or "") + " " + (item.get("summary") or "")
        if any(contains_word(blob, kw) for kw in CHINA_KEYWORDS):
            clone = dict(item)
            clone["section"] = "domestic"
            out.append(clone)
    return out


# ============================================================================
# 五、L3 理解：翻译 / 聚类 / 大事总结
# ============================================================================

NUMBERED_LINE_RE = re.compile(r"^[\[\(【]?\s*(\d{1,3})\s*[\]\)】]?\s*[.、:：]?\s*(.*)$")
CID_LINE_RE = re.compile(r"^[\[\(【]?\s*([ci]\d{1,3})\s*[\]\)】]?\s*[.、:：]?\s*(.*)$")


def parse_indexed_translations(text, count):
    """解析「[编号] 中文译文」形式的模型输出。

    缺编号时降级：没带编号的行按出现顺序补到还没拿到的编号上；
    补不上的编号由调用方回退英文原文。
    """
    out = {}
    pending = []
    for line in clean_model_output_lines(text):
        match = NUMBERED_LINE_RE.match(line)
        if match:
            index = int(match.group(1))
            body = match.group(2).strip()
            if 1 <= index <= count and body and index not in out:
                out[index] = body
                continue
        pending.append(line)

    missing = [i for i in range(1, count + 1) if i not in out]
    for line, index in zip(pending, missing):
        out[index] = line
    return out


def translate_titles(items, client, batch_size=TRANSLATE_BATCH):
    """分批把英文标题翻成中文，原地写入 title_zh。

    中文源直接沿用原标题（零翻译）。翻译失败/漏译的条目回退英文原标题，
    绝不因为一条翻译失败把整条新闻丢掉。
    """
    if not items:
        return {"需要翻译": 0, "成功": 0, "回退原文": 0}

    stats = {"需要翻译": 0, "成功": 0, "回退原文": 0}

    # 中文源先处理掉
    for item in items:
        if item.get("lang") == "zh":
            item["title_zh"] = item["title"]
            item["title_en"] = ""
        else:
            item["title_zh"] = ""
            item["title_en"] = item["title"]

    english = [it for it in items if it.get("lang") != "zh"]
    stats["需要翻译"] = len(english)

    for start in range(0, len(english), batch_size):
        batch = english[start:start + batch_size]
        lines = "\n".join(
            "%d. %s" % (i + 1, it["title"]) for i, it in enumerate(batch)
        )
        prompt = TITLE_TRANSLATE_PROMPT.format(count=len(batch), items=lines)
        try:
            output = client.complete(prompt)
            mapping = parse_indexed_translations(output, len(batch))
        except AIError as exc:
            warn("标题翻译失败（第 %d 批）：%s，该批回退英文原文" % (start // batch_size + 1, exc))
            mapping = {}

        for i, item in enumerate(batch, start=1):
            zh = (mapping.get(i) or "").strip()
            if zh and is_chinese_text(zh):
                item["title_zh"] = zh
                stats["成功"] += 1
            else:
                item["title_zh"] = item["title"]  # 回退英文原文
                stats["回退原文"] += 1

    return stats


def item_publisher(item):
    """取条目所属的「媒体」（而不是「源」）。

    一家媒体可能有多个频道/多个 RSS（例如中新网的即时、时政、社会…），
    它们同属一家媒体，绝不能互相充数成「多家媒体报道」。
    """
    return (item.get("publisher") or item.get("source") or "").strip()


def entity_conflict(text_a, text_b):
    """两条标题是否「国家实体互斥」——即明显在讲不同国家的不同事。

    这是降低阈值之后必须配套的守卫。典型陷阱：
        「习近平会见加拿大总理卡尼」
        「习近平会见巴布亚新几内亚总理马拉佩」
    字面框架高度雷同，bigram 相似度可达 0.23，但它们是完全不同的两件事。
    规则：两边都提到了国家、且没有任何一个共同国家 → 判定冲突，不许合并。
    只在一边提到国家时无法判断（例如某条标题没写国名），按不冲突处理。
    """
    countries_a = find_countries(text_a)
    countries_b = find_countries(text_b)
    if not countries_a or not countries_b:
        return False
    return not (countries_a & countries_b)


def vote_count(cluster_items):
    """计算一个簇的「家数」。

    规则（实测教训）：
      - 按**媒体**计票，不按「源」计票。同一家媒体的多个频道不能互相充数。
      - 同一来源的条目不能互相充数（同一家媒体两篇报道不算多家）
      - Google News 是聚合器：簇内有具体媒体时它不计票，自己单独成簇时算 1 家
    """
    concrete = {
        item_publisher(it) for it in cluster_items
        if item_publisher(it) and item_publisher(it) not in AGGREGATOR_SOURCES
    }
    if concrete:
        return len(concrete)
    has_aggregator = any(
        item_publisher(it) in AGGREGATOR_SOURCES for it in cluster_items
    )
    return 1 if has_aggregator else 0


def cluster_items(items, threshold=CLUSTER_THRESHOLD):
    """按中文 bigram Jaccard 把「同一事件」的报道聚成一个簇。

    关键约束：
      - 同一媒体的条目之间绝不合并（否则同一家媒体的两篇稿子会变成「多家报道」）
      - 只用中文标题比较（英文层面不同措辞无重合，先翻译再比才靠谱）
    """
    clusters = []

    for item in items:
        title = item.get("title_zh") or item.get("title") or ""
        grams = zh_bigrams(title)
        publisher = item_publisher(item)

        best = None
        best_score = 0.0
        for cluster in clusters:
            # 同媒体已在该簇 → 不合并
            if any(item_publisher(member) == publisher for member in cluster["items"]):
                continue
            score = 0.0
            for member in cluster["items"]:
                member_title = member.get("title_zh") or member.get("title") or ""
                similarity = jaccard(grams, zh_bigrams(member_title))
                # 实体互斥的绝不合并（防止「习近平会见A国总理」并进「会见B国总理」）
                if similarity >= threshold and entity_conflict(title, member_title):
                    continue
                score = max(score, similarity)
            if score > best_score:
                best_score = score
                best = cluster

        if best is not None and best_score >= threshold:
            best["items"].append(item)
            best["score"] = max(best.get("score", 0.0), best_score)
        else:
            clusters.append({"items": [item], "score": 0.0})

    return clusters


def pick_primary(cluster):
    """在簇内挑一条做头条（大图）：优先有图、其次时间最新。"""
    def sort_key(item):
        return (
            1 if item.get("image") else 0,
            item.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        )

    return sorted(cluster["items"], key=sort_key, reverse=True)[0]


def build_clusters(raw_items, section_prefix):
    """把规范化后的条目聚成带 cid 的簇，并按「家数优先、时间倒序」排好。"""
    clusters = cluster_items(raw_items)
    built = []
    for cluster in clusters:
        primary = pick_primary(cluster)
        count = vote_count(cluster["items"])
        # 热度分：热榜源（澎湃精选）的条目给加权，让它排在流水账前面
        hot = any(it.get("is_hot") for it in cluster["items"])
        # 跨来源印证 + 热榜信号，两者相加作为「重要度」
        importance = count + (HOT_BOOST if hot else 0)
        built.append({
            "cid": "",  # 稍后按排序结果统一分配，保证稳定
            "section": primary.get("section"),
            "source_count": count,
            # 重要度用于排序（含热榜加权），source_count 仍表示真实家数
            "importance_score": importance,
            "is_hot": hot,
            "image": primary.get("image") or "",
            "is_primary_source": primary.get("source"),
            # 媒体名：席位配额按这个字段计数（同一媒体的多频道算一家）
            "primary_publisher": item_publisher(primary),
            "primary_title_zh": primary.get("title_zh") or primary.get("title"),
            "published": max(
                (it.get("published") or datetime.min.replace(tzinfo=timezone.utc)
                 for it in cluster["items"]),
            ),
            "items": sorted(
                cluster["items"],
                key=lambda it: (
                    0 if it is not primary else 1,
                    it.get("published") or datetime.min.replace(tzinfo=timezone.utc),
                ),
                reverse=True,
            ),
        })

    # 排序：重要度（家数 + 热榜加权）高的优先，其次时间新的优先。
    # 这样澎湃精选的热点能压过中新网的流水账，直接对准「了解今日热点」的需求。
    built.sort(key=lambda c: (c["importance_score"], c["published"]), reverse=True)

    # 分配稳定 cid（i1/i2… 国际榜，c1/c2… 国内榜）
    for index, cluster in enumerate(built, start=1):
        cluster["cid"] = "%s%d" % (section_prefix, index)

    return built


def apply_publisher_quota(clusters, limit=MAX_EVENTS_PER_SECTION,
                          quota=PUBLISHER_SEAT_QUOTA):
    """挑出最终入选的事件：**热点覆盖优先，配额只做兜底**。

    设计原则来自项目初衷：
        「核心诉求是快速了解新闻热点，看不同观点是深入需求而非默认需求。」
        所以第一步永远是「把最重要的热点排满」，而不是「保证每家媒体都露脸」。

    历史教训（两次都修过了头）：
      第一次：完全按（家数, 时间）排序 → France 24 独占 5 席，读者只能看到一家之言。
      第二次：加了「每个媒体保底 1 席」→ 反而更糟：
              · 国际版块 179 条素材最终只显示 5 条，3 个席位被浪费；
              · 为了给低频媒体腾位置，挤掉了更重要的热点。
              这和「了解热点」的初衷直接冲突。
      第三次（本函数要解决的新问题）：同主题的 3 条「习近平会见X」都来自中新网，
              配额按 publisher 限 2 席，第 3 条被砍掉——同主题组被硬生生拆散。
              症状是「主题卡片只有 2 条，而源里明明有 3 条」。

    现在的规则（简单、可预测）：
        1. 同主题的事件**共享一个席位名额**（它们最终会并成一张卡片，
           按条数占名额会让主题组残缺）
        2. 先按原排序（家数多 → 时间新）依次取，每个媒体最多 quota 席
        3. 席位没满 → 先补未达配额的其他媒体
        4. 还不够 → 允许超配额补齐（宁可重复来源，也不浪费席位）

    返回 (入选列表, 被舍去的列表)。
    """
    if not clusters:
        return [], []

    # 标记每条事件所属的主题前缀（用于「同主题共享名额」）
    themes = [theme_key_of(c.get("primary_title_zh") or "") for c in clusters]
    theme_seats = {}   # 主题前缀 → 已占用的「逻辑席位」

    picked = []
    picked_ids = set()
    used = {}

    def theme_of(cluster, index):
        return themes[index]

    def can_take(cluster, index, allow_overflow=False):
        publisher = cluster.get("primary_publisher") or ""
        if allow_overflow:
            return True
        if used.get(publisher, 0) >= quota:
            return False
        theme = theme_of(cluster, index)
        # 同主题的多条共享一个名额：只有该主题首次出现时才占位
        if theme and theme_seats.get(theme, 0) >= quota:
            return False
        return True

    def take(cluster, index):
        publisher = cluster.get("primary_publisher") or ""
        used[publisher] = used.get(publisher, 0) + 1
        theme = theme_of(cluster, index)
        if theme:
            theme_seats[theme] = theme_seats.get(theme, 0) + 1
        picked.append(cluster)
        picked_ids.add(id(cluster))

    # 第一轮：按原排序，遵守每媒体 quota 上限
    for index, cluster in enumerate(clusters):
        if len(picked) >= limit:
            break
        if can_take(cluster, index):
            take(cluster, index)

    # 第二轮：席位没用满 → 补齐到 limit。
    if len(picked) < limit:
        for index, cluster in enumerate(clusters):
            if len(picked) >= limit:
                break
            if id(cluster) in picked_ids:
                continue
            publisher = cluster.get("primary_publisher") or ""
            if used.get(publisher, 0) >= quota:
                continue
            take(cluster, index)

    # 第三轮：还不够 → 认了，把剩下的按排序补满，不让席位浪费
    if len(picked) < limit:
        for index, cluster in enumerate(clusters):
            if len(picked) >= limit:
                break
            if id(cluster) in picked_ids:
                continue
            take(cluster, index)

    dropped = [c for c in clusters if id(c) not in picked_ids]
    return picked, dropped


def summarize_events(clusters, section_label, client):
    """让 AI 把簇总结成 5–8 条「今日大事」，返回 {cid: (概括, 重要性)}。"""
    if not clusters:
        return {}

    lines = []
    for cluster in clusters:
        count = cluster["source_count"]
        tag = "【%d家媒体报道】" % count if count >= 2 else ""
        title = cluster["primary_title_zh"] or ""
        lines.append("[%s] %s%s" % (cluster["cid"], tag, title))
    items_text = "\n".join(lines)

    prompt = EVENTS_SUMMARY_PROMPT.format(
        scope=section_label, count=len(lines), items=items_text
    )
    try:
        output = client.complete(prompt)
    except AIError as exc:
        warn("「今日%s大事」总结失败：%s，改为按簇自动生成" % (section_label, exc))
        return {}

    valid_cids = {c["cid"] for c in clusters}
    title_by_cid = {c["cid"]: (c["primary_title_zh"] or "") for c in clusters}
    result = {}

    for line in clean_model_output_lines(output):
        match = CID_LINE_RE.match(line)
        body = ""
        cid = ""
        if match:
            cid = match.group(1)
            body = match.group(2).strip()
        if not body:
            body = line

        # 概括——重要性 拆开（中英破折号、单双破折号都认）
        summary, importance = split_summary_importance(body)

        if cid not in valid_cids:
            # AI 把编号写坏了：按文本相似度找回最可能的簇（防错位）
            best_cid, best_score = "", 0.0
            for candidate_cid, candidate_title in title_by_cid.items():
                if candidate_cid in result:
                    continue
                score = text_similarity(summary, candidate_title)
                if score > best_score:
                    best_score = score
                    best_cid = candidate_cid
            if best_score < 0.15:
                continue  # 找不回来就丢弃，宁缺毋滥
            cid = best_cid

        if cid in result:
            continue

        summary = truncate_zh(summary, 40)
        importance = truncate_zh(importance, 15)
        if not summary:
            continue
        result[cid] = (summary, importance)

    return result


def split_summary_importance(body):
    """把「概括——重要性」拆成两段，并清掉残留的破折号/连接符。

    实测模型输出里的破折号有全角、半角、单个、三个好几种写法，而且会写成
    「概括——或冲击——影响值得关注」这种多段式。所以不能只 split 一次：
    统一按所有破折号切开，第一段做概括，最后一段做重要性，
    中间若有残留碎片就并入重要性，保证两侧都不留破折号。
    """
    body = (body or "").strip()
    pieces = [p.strip(" \t：:，,。.、—-–") for p in
              re.split(r"——|—|--|–", body)]
    pieces = [p for p in pieces if p]
    if not pieces:
        return "", ""
    summary = pieces[0]
    importance = pieces[-1] if len(pieces) > 1 else ""
    return summary, importance


def truncate_zh(text, limit):
    """按汉字长度截断（用于事件概括/重要性这类有硬字数契约的短字段）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，、。；：,.;:")


def truncate_at_sentence(text, limit):
    """在句子边界截断，绝不把词切断。

    为什么不能用 text[:limit]：
        线上真实故障——播报稿被硬切在「追踪巴勒」，后面「斯坦」没了，
        既读不通，edge-tts 还会把「巴勒」当成完整的词念出来。
    策略（按优先级，宁可短一点也不切词）：
        1. 不足上限 → 原样返回
        2. 在 [limit*0.5, limit] 区间内从后往前找**句末标点**（。！？…）
        3. 找不到句末标点，才退到分号
        4. 再找不到才退到逗号（此时至少词是完整的）
        5. 实在没有任何标点边界，硬切并去掉半截标点
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text

    # 从宽到窄试，优先保证句子完整。关键顺序：
    #   第一轮只找句末标点（句号/问号/感叹号），只要窗口里有句号就用它；
    #   找不到句末标点，才在更宽的窗口里退到分号、逗号。
    # 这样「第一句结束。第二句还没说完」会断在句号后，而不是断在第二句的逗号上。
    for marks in ("。！？…!?", "；;", "，,、 "):
        for low_ratio in (0.5, 0.3):
            low = max(1, int(limit * low_ratio))
            window = text[low:limit]
            for index in range(len(window) - 1, -1, -1):
                if window[index] in marks:
                    cut = text[:low + index + 1].strip()
                    # 不能整段都是标点（例如「，，，，」），那样截出来毫无意义
                    if re.search(r"[\u4e00-\u9fffA-Za-z0-9]", cut):
                        return cut

    # 完全没有边界可退：硬切，并去掉结尾的半截标点与空白
    return text[:limit].rstrip("，、,；;：:—-– 。！？…!? ").strip()


def fallback_event(cluster):
    """AI 总结缺失时的兜底事件（用簇头条标题充数，保证版块条数）。"""
    title = (cluster["primary_title_zh"] or "").strip()
    return truncate_zh(title, 40), "重要事件"


# ============================================================================
# 六、L4 校验闸门（防编造 + 防错位）
# ============================================================================

def cluster_titles(cluster):
    """簇内所有可用来对账的标题文本。"""
    texts = []
    for item in cluster["items"]:
        texts.append(item.get("title_zh") or "")
        texts.append(item.get("title_en") or "")
        texts.append(item.get("title") or "")
    return [t for t in texts if t]


def reselect_link(cluster, summary_zh):
    """按国家实体在簇内重新挑一条最贴合摘要的报道做头条。"""
    mentioned = find_countries(summary_zh)
    if not mentioned:
        return pick_primary(cluster)

    best_item = None
    best_hits = -1
    for item in cluster["items"]:
        blob = " ".join([
            item.get("title_zh") or "",
            item.get("title_en") or "",
            item.get("title") or "",
            item.get("summary") or "",
        ])
        hits = len(mentioned & find_countries(blob))
        if hits > best_hits:
            best_hits = hits
            best_item = item
    return best_item or pick_primary(cluster)


def validate_event(summary_zh, cluster):
    """对一条 AI 生成的大事做国家实体对账。

    返回 (是否通过, 详情)。
    - 摘要里没提任何国家 → 直接通过（不是所有新闻都涉及国家）
    - 摘要提的国家里，有 ≥2 个在原文标题里找不到 → 判定为编造，需要重做
    """
    mentioned = find_countries(summary_zh)
    if not mentioned:
        return True, {"提到国家": [], "未匹配": [], "原因": "未涉及国家实体"}
    matched, unmatched = country_match_report(summary_zh, cluster_titles(cluster))
    detail = {
        "提到国家": sorted(mentioned),
        "已匹配": sorted(matched),
        "未匹配": sorted(unmatched),
    }
    if len(unmatched) >= 2:
        detail["原因"] = "有 %d 个国家在原文里找不到，疑似编造" % len(unmatched)
        return False, detail
    detail["原因"] = "通过"
    return True, detail


def redo_event(cluster, client):
    """把单条事件打回 AI 重做（只影响这一条，因为用稳定 cid 绑定）。"""
    lines = "\n".join("- %s" % t for t in cluster_titles(cluster)[:6])
    prompt = EVENT_REDO_PROMPT.format(cid=cluster["cid"], items=lines)
    try:
        output = client.complete(prompt, max_tokens=512)
    except AIError as exc:
        warn("单条事件重做失败（%s）：%s" % (cluster["cid"], exc))
        return None

    for line in clean_model_output_lines(output):
        match = CID_LINE_RE.match(line)
        body = match.group(2).strip() if match else line
        if not body:
            continue
        summary, importance = split_summary_importance(body)
        summary = truncate_zh(summary, 40)
        importance = truncate_zh(importance, 15)
        if summary:
            return summary, importance
    return None


def build_section_events(clusters, section_label, client, max_retry=2):
    """生成一个版块的大事列表，并逐条过校验闸门。

    传入的 clusters 应已经过 apply_publisher_quota() 排序，
    靠前的就是「多源优先 + 媒体不垄断」之后最该露出的事件。
    返回 (事件列表, 闸门日志列表)。
    """
    if not clusters:
        return [], []

    by_cid = {c["cid"]: c for c in clusters}
    events = {}
    gate_log = []

    ai_result = summarize_events(clusters, section_label, client)
    pending = dict(ai_result)

    # 逐条校验 + 允许 ≤2 次重做
    for attempt in range(max_retry + 1):
        retry_cids = []
        for cid, (summary, importance) in list(pending.items()):
            cluster = by_cid.get(cid)
            if cluster is None:
                continue
            ok, detail = validate_event(summary, cluster)

            # 校验不顺时先尝试在簇内重新选链接（可能只是头条挑错了）
            if not ok:
                new_primary = reselect_link(cluster, summary)
                candidate_texts = [
                    new_primary.get("title_zh") or "",
                    new_primary.get("title_en") or "",
                    new_primary.get("title") or "",
                ]
                matched, unmatched = country_match_report(summary, candidate_texts)
                if len(unmatched) < 2:
                    if new_primary is not cluster["items"][0]:
                        # 把纠正后的链接提到簇首，后续渲染以它为准
                        cluster["items"].remove(new_primary)
                        cluster["items"].insert(0, new_primary)
                        cluster["image"] = new_primary.get("image") or cluster["image"]
                    ok = True
                    detail["原因"] = "在簇内重选链接后通过"
                    detail["已匹配"] = sorted(matched)

            gate_log.append({
                "cid": cid,
                "第几次": attempt + 1,
                "结果": "通过" if ok else "打回",
                "详情": detail,
            })

            if ok:
                events[cid] = (summary, importance)
                pending.pop(cid, None)
            elif attempt >= max_retry:
                warn("事件 %s 连续 %d 次校验不通过，丢弃：%s" % (cid, max_retry + 1, summary[:30]))
                pending.pop(cid, None)
            else:
                retry_cids.append(cid)

        if not retry_cids:
            break

        # 只重做失败的那几条，其余条目不受影响
        for cid in retry_cids:
            redone = redo_event(by_cid[cid], client)
            if redone:
                pending[cid] = redone
            else:
                pending.pop(cid, None)

    # ---- 条数保底：AI 只给了 1–2 条时，用高分簇补齐，绝不让版块空着 ----
    target_min = 3
    if len(events) < target_min:
        for cluster in clusters:
            if len(events) >= target_min:
                break
            if cluster["cid"] in events:
                continue
            summary, importance = fallback_event(cluster)
            events[cluster["cid"]] = (summary, importance)
            gate_log.append({
                "cid": cluster["cid"],
                "第几次": "兜底",
                "结果": "兜底补齐",
                "详情": {"原因": "AI 返回条数不足，用簇标题兜底"},
            })

    # 按簇的排序输出（家数多、时间新的在前），并做硬性条数上限。
    # AI 实测会返回 12–20 条（契约明明写的是 5–8 条），不截断会让页面被稀释、
    # 播报稿也塞不进字数预算。按排序结果（家数优先）截断，保留最靠前的。
    ordered = [
        (cluster["cid"], events[cluster["cid"]])
        for cluster in clusters if cluster["cid"] in events
    ]
    # 这里的 clusters 已经过 apply_publisher_quota() 筛选与排序，
    # 长度本身通常就 <= MAX_EVENTS_PER_SECTION，这里只是兜底。
    if len(ordered) > MAX_EVENTS_PER_SECTION:
        dropped = len(ordered) - MAX_EVENTS_PER_SECTION
        log("  %s版块事件过多（%d 条），按重要性只保留前 %d 条，舍去 %d 条" % (
            section_label, len(ordered), MAX_EVENTS_PER_SECTION, dropped))
        ordered = ordered[:MAX_EVENTS_PER_SECTION]
    return ordered, gate_log


# ============================================================================
# 七、L5 深挖：抓正文 + 分段翻译
# ============================================================================

SENTENCE_END = "。！？!?."
SOFT_BREAK = "；;\n"


def common_prefix_len(text_a, text_b):
    """两个标题的公共前缀字数。"""
    count = 0
    for char_a, char_b in zip(text_a or "", text_b or ""):
        if char_a != char_b:
            break
        count += 1
    return count


def group_by_theme(events, prefix_min=THEME_PREFIX_MIN, min_events=THEME_MIN_EVENTS):
    """把同一系列的事件归成一个主题组。

    输入是已经排好序的事件列表（保持顺序，主题组放在其首条事件的位置）。

    返回列表，每项形如：
        {"theme": "习近平会见", "events": [ev1, ev2, ev3, ev4]}
    或者「单条主题」：
        {"theme": "", "events": [ev]}

    为什么用公共前缀而不是相似度：
        实测 4 条「习近平会见X国领导人」两两 bigram 相似度只有 0.17–0.23，
        因为国家名完全不同，任何合理阈值都抓不到。但它们的公共前缀
        恰好是「习近平会见」。前缀规则可预测、不会误伤无关新闻。
    """
    groups = []
    for event in events:
        title = (event.get("zh") or "").strip()
        if not title:
            groups.append({"theme": "", "events": [event]})
            continue

        placed = False
        for group in groups:
            if not group["theme"]:
                continue
            if common_prefix_len(group["theme"], title) >= prefix_min:
                group["events"].append(event)
                placed = True
                break
        if not placed:
            groups.append({"theme": title, "events": [event]})

    # 只保留真正成为「组」的（>= min_events），否则降级为单条
    result = []
    for group in groups:
        if len(group["events"]) >= min_events:
            # 主题名取组内公共前缀，而不是整条标题
            prefix = group["theme"]
            for event in group["events"][1:]:
                prefix = prefix[:common_prefix_len(prefix, event.get("zh") or "")]
            result.append({
                "theme": prefix.strip() or group["theme"],
                "events": group["events"],
            })
        else:
            for event in group["events"]:
                result.append({"theme": "", "events": [event]})
    return result


def summarize_theme(group, section_label, client):
    """为一个主题组生成一句话概括（不超过 40 字）。

    例如 4 条「习近平会见X国领导人」→「习近平在APEC期间会见四国领导人」。
    AI 失败时用「前缀 + 共 N 条」兜底，绝不空着。
    """
    events = group["events"]
    prefix = group["theme"]
    fallback = "%s等%d项动态" % (prefix, len(events)) if prefix else "多条相关动态"

    titles = "\n".join("- " + (e.get("zh") or "")[:60] for e in events[:8])
    prompt = (
        "下面是一组同一主题的中文新闻标题，请用一句不超过 30 个汉字的话概括它们"
        "共同讲的是什么事。\n\n"
        "要求：\n"
        "- 只输出这一句概括，不要任何开场白、编号或解释\n"
        "- 必须基于标题内容，禁止编造标题里没有的信息\n"
        "- 如果这些标题讲的都是同一类动作（例如同一人会见不同国家领导人），"
        "就点明是谁做了什么、涉及几方\n\n"
        "标题：\n%s\n" % titles
    )
    try:
        text = strip_wrapping(client.complete(prompt, max_tokens=256))
        text = text.strip().splitlines()[0].strip() if text.strip() else ""
        # 防御：AI 偶尔会回显提示词、输出「演练：」这类占位、或返回英文
        if text and not any(bad in text for bad in (
                "演练", "未知提示词", "标题：", "要求", "概括", "输出")):
            candidate = truncate_zh(text.strip("：: ，,"), 40)
            # 主题概括必须是中文，否则用兜底
            if candidate and chinese_ratio(candidate) > 0.5:
                return candidate
    except AIError as exc:
        warn("主题概括失败（%s）：%s" % (prefix or section_label, exc))
    return fallback


def theme_key_of(title, prefix_min=THEME_PREFIX_MIN):
    """取一条标题的「主题前缀键」，用于判断两条是否同主题。

    做法：取标题前 prefix_min 个字（用公共前缀的判定口径）。
    「习近平会见越南总书记苏林」→「习近平会见」
    「全国多地迎来寒潮」        →「全国多地迎」
    这两者前缀完全不同，不会误判成同主题。

    为什么不用「公共前缀比较」而是取固定长度：
        配额阶段需要一个「每条事件一个值」的键来做分组统计，
        取固定长度前缀即可，真正的成组判定仍由 group_by_theme 用
        逐对公共前缀长度来做，两者口径一致。
    """
    text = (title or "").strip()
    if len(text) < prefix_min:
        return ""
    return text[:prefix_min]


def group_events_by_theme(events, section_label, client):
    """把同系列事件归成主题卡片，返回新的「卡片列表」。

    输出结构（每个元素是一张卡片）：
        普通卡片：{"cid": "c1", "zh": ..., "theme": "", "events": [event]}
        主题卡片：{"cid": "c1", "zh": "习近平会见四国领导人",
                   "theme": "习近平会见", "events": [ev1, ev2, ev3, ev4]}

    为什么要包一层 events：
        页面用「主卡片 + 展开」渲染主题卡片，播报稿只需要读 zh 字段，
        两者都能直接消费这个结构，不必各写一套判断。

    卡片继承第一条事件的 cid，保证引用稳定。
    """
    groups = group_by_theme(events)
    cards = []
    for group in groups:
        members = group["events"]
        if len(members) <= 1:
            event = members[0]
            cards.append({
                "cid": event.get("cid"),
                "zh": event.get("zh") or "",
                "importance": event.get("importance") or "",
                "source_count": event.get("source_count") or 0,
                "image": event.get("image") or "",
                "cluster": event.get("cluster") or [],
                "theme": "",
                "events": members,
            })
            continue

        # 主题卡片：AI 生成一句共同概括，import 取组内最高
        theme_summary = summarize_theme(group, section_label, client)
        max_count = max((m.get("source_count") or 0) for m in members)
        picked_image = ""
        for member in members:
            if member.get("image"):
                picked_image = member["image"]
                break
        log("  %s版块主题归并：%r 共 %d 条 → 1 张卡片" % (
            section_label, group["theme"], len(members)))
        cards.append({
            "cid": members[0].get("cid"),
            "zh": theme_summary,
            "importance": "共 %d 条相关动态" % len(members),
            "source_count": max_count,
            "image": picked_image,
            "cluster": [],          # 主题卡片本身不再挂媒体列表
            "theme": group["theme"],
            "events": members,
        })
    return cards


def split_text_segments(text, target=900, low=800, high=950):
    """把长文按句子边界切成约 900 字符的段。

    规则：在 [low, high] 窗口内从后往前找句末标点；找不到退到分号/换行；
    再找不到退到空格；最后才硬切。这样模型不会被截断（实测 2400 字符只译出
    760 字就是这个原因）。
    """
    text = (text or "").strip()
    if not text:
        return []
    segments = []
    rest = text
    while len(rest) > high:
        window = rest[low:high]
        cut = -1
        for i in range(len(window) - 1, -1, -1):
            if window[i] in SENTENCE_END:
                cut = i + 1
                break
        if cut < 0:
            for i in range(len(window) - 1, -1, -1):
                if window[i] in SOFT_BREAK:
                    cut = i + 1
                    break
        if cut < 0:
            for i in range(len(window) - 1, -1, -1):
                if window[i] == " ":
                    cut = i + 1
                    break
        if cut < 0:
            cut = len(window)
        segments.append(rest[:low + cut].strip())
        rest = rest[low + cut:].lstrip()
    if rest:
        segments.append(rest.strip())
    return [s for s in segments if s]


def extract_og_image(html, page_url=""):
    """从 HTML 里取文章主图。

    为什么需要这个：
        中新网等中文源的 RSS **完全不带图**（没有 media:thumbnail，
        description 里也没 <img>），导致「国内热点」版块 0/8 有图，
        而国际版块 7/8 有图，两边观感割裂。

    取值顺序：og:image → twitter:image → 正文里第一张「像文章配图」的图。
    所有候选都必须过 looks_like_article_image()，避免取到导航箭头/广告图。

    返回空串表示「这篇文章确实没有可用配图」——这是可接受的结果，
    页面会渲染成纯文字卡片，比放一张错误的图好。
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""

    def resolve(src):
        """把相对路径补成绝对地址。"""
        src = (src or "").strip()
        if not src:
            return ""
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/") and page_url:
            try:
                from urllib.parse import urljoin

                src = urljoin(page_url, src)
            except Exception:
                return ""
        elif not src.startswith(("http://", "https://")) and page_url:
            try:
                from urllib.parse import urljoin

                src = urljoin(page_url, src)
            except Exception:
                return ""
        return normalize_image_url(src)

    # 1) og:image / og:image:secure_url
    for prop in ("og:image:secure_url", "og:image", "og:image:url"):
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            found = resolve(tag["content"])
            if found and looks_like_article_image(found):
                return found
    # 2) twitter:image
    for name in ("twitter:image", "twitter:image:src"):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            found = resolve(tag["content"])
            if found and looks_like_article_image(found):
                return found
    # 3) 不做「正文第一张 <img>」兜底。
    #
    #    实测结论：这个兜底对中文站点弊大于利。
    #    中新网正文页没有 og:image，页面上全是站点 logo、导航箭头、广告 banner，
    #    无论怎么加关键词过滤，都会取到错图（试过 toparr.png、站点 logo、
    #    广告位图三种）。而「取到错图」比「没有图」糟糕得多——
    #    读者看到头条大图是个导航箭头，会直接认为站点坏了。
    #
    #    所以：只信任站点自己声明的社交分享图（og:image / twitter:image）。
    #    没有就渲染纯文字卡片，这是可接受的降级。
    return ""


def fetch_article(url, http_get=None, timeout=SOURCE_TIMEOUT):
    """抓正文，同时返回纯文本与主图。

    返回 (正文文本, 主图 URL)。失败返回 ("", "")。
    http_get 可注入，dry-run 时必须传假抓取器，否则演练就会偷偷联网。
    """
    http_get = http_get or http_get_bytes
    try:
        raw = http_get(url, timeout)
    except Exception as exc:  # noqa: BLE001 正文抓取失败是常态（France24 必 403）
        log("正文抓取失败（%s）：%s" % (str(url)[:60], type(exc).__name__))
        return "", ""

    html = raw.decode("utf-8", "replace")
    # 传入页面 URL，让相对路径（如 /fileftp/xxx.jpg）能补成绝对地址
    image = extract_og_image(html, page_url=url)

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return "", image

    for tag in soup(["script", "style", "nav", "footer", "aside", "header", "form"]):
        tag.decompose()

    paragraphs = []
    for para in soup.find_all("p"):
        text = para.get_text(" ", strip=True)
        if len(text) >= 40:
            paragraphs.append(text)
    body = "\n".join(paragraphs)
    # 正文太短基本是抓到了 cookie 墙或 403 页面，按失败处理
    if len(body) < 200:
        return "", image
    return body, image


def fetch_article_text(url, http_get=None, timeout=SOURCE_TIMEOUT):
    """只要正文纯文本（兼容旧调用方）。"""
    body, _image = fetch_article(url, http_get=http_get, timeout=timeout)
    return body


def translate_fulltext(title, body, client):
    """分段翻译正文，返回中文全文（失败返回空串）。"""
    segments = split_text_segments(body)
    translated = []
    for index, segment in enumerate(segments, start=1):
        prompt = FULLTEXT_TRANSLATE_PROMPT.format(title=title, body=segment)
        try:
            part = client.complete(prompt)
            part = strip_wrapping(part)
            if part:
                translated.append(part)
        except AIError as exc:
            warn("第 %d/%d 段正文翻译失败：%s" % (index, len(segments), exc))
            return ""
    return "\n\n".join(translated)


def strip_wrapping(text):
    """去掉模型偶尔加的首尾引号/代码块残留。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip().strip('"').strip()


def deep_dive(clusters, client, limit=DEEP_DIVE_COUNT, http_get=None):
    """挑若干条做全文翻译：跨来源去重，优先多源簇。"""
    http_get = http_get or http_get_bytes

    # 排序优先级：多源簇（有多家媒体互相印证，值得深挖）在前，簇内时间新的在前。
    # 先排序再取前 limit 条，比「边挑边判断」简单且不会出现名额算错的问题。
    ordered = sorted(
        clusters,
        key=lambda c: (
            c.get("source_count", 0),
            c.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )

    picks = []
    seen_sources = set()
    for cluster in ordered:
        if len(picks) >= limit:
            break
        primary = cluster["items"][0]
        publisher = item_publisher(primary)
        # 跨媒体去重：同一家媒体不重复占名额（多源簇例外，它本身就是多家）
        if publisher in seen_sources and cluster["source_count"] < 2:
            continue
        seen_sources.add(publisher)
        picks.append(cluster)

    done = 0
    for cluster in picks:
        primary = cluster["items"][0]
        title_en = primary.get("title_en") or primary.get("title") or ""

        # 中文源的正文本来就能直接读，再「翻译」一遍等于把中文当中文翻，
        # 纯属浪费额度还会产出诡异结果。中文源只抓正文补全，不做翻译。
        if primary.get("lang") == "zh":
            body, og_image = fetch_article(primary.get("url") or "", http_get=http_get)
            # 中文源 RSS 普遍不带图，用正文页的 og:image 补上（国内版块原本 0/8 有图）
            if og_image and not primary.get("image"):
                primary["image"] = og_image
                cluster["image"] = cluster.get("image") or og_image
            if body:
                primary["summary_zh"] = body[:600]
                done += 1
            continue

        body, og_image = fetch_article(primary.get("url") or "", http_get=http_get)
        # 英文源若 RSS 也没给图，同样用 og:image 兜底
        if og_image and not primary.get("image"):
            primary["image"] = og_image
            cluster["image"] = cluster.get("image") or og_image
        if not body:
            # 403 / 超时 → 回退 RSS 摘要（France 24 正文页必 403）
            body = primary.get("summary") or ""
            if body:
                log("正文抓取受阻，回退 RSS 摘要：%s" % (primary.get("source") or "未知来源"))
        if len(body) < 120:
            continue
        zh = translate_fulltext(title_en, body, client)
        if zh:
            primary["summary_zh"] = zh
            # summary_en 只放 RSS 原文摘要（短），供折叠区对照。
            # 不要把整篇英文正文塞进来——那是几千字的信息噪音，用户点开卡片会被淹没。
            primary["summary_en"] = primary.get("summary") or ""
            done += 1
        else:
            # 翻译失败显示英文摘要（降级，不空着）
            primary["summary_zh"] = primary.get("summary") or ""
            primary["summary_en"] = primary.get("summary") or ""
    return done


# ============================================================================
# 八、L6 播报与语音
# ============================================================================

def build_broadcast(events_by_section, client, today):
    """AI 改写播报稿（与 TTS 同源）。

    events_by_section 形如 [("国内", [event, ...]), ("国际", [event, ...])]，
    其中 event 是数据契约里的那条大事（含 zh / importance）。

    关键约束（都是线上故障换来的）：
      - 只挑每个版块最前面的若干条要点，不把全部事件塞给模型。
        实测 32 条全塞进去，光罗列就要 885 字，模型只能写超，最后被硬切。
      - 字数上限由 BROADCAST_CHAR_LIMIT 统一控制，提示词和截断共用同一个值。
      - 截断走 truncate_at_sentence，绝不切断词。
    """
    points = []
    summary_only = []
    for section_label, events in events_by_section:
        # 只取靠前的若干条：调用方已按（家数, 时间）排序，靠前的就是最重要的
        picked = [e for e in (events or []) if (e.get("zh") or "").strip()]
        picked = picked[:BROADCAST_MAX_EVENTS_PER_SECTION]
        for event in picked:
            summary = (event.get("zh") or "").strip()
            importance = (event.get("importance") or "").strip()
            line = summary
            if importance:
                line += "，" + importance
            points.append("%d. 【%s】%s" % (len(points) + 1, section_label, line))
            summary_only.append(summary)

    # 手工拼接日期：不能用 strftime("%-m")，那个写法在 Windows 上会直接抛异常
    date_text = "%d年%d月%d日" % (today.year, today.month, today.day)

    if not points:
        return "各位好，今天是%s，欢迎收听今日国际大事。" % date_text

    prompt = BROADCAST_PROMPT.format(
        date=date_text,
        max_chars=BROADCAST_CHAR_LIMIT,
        point_count=len(points),
        points="\n".join(points),
    )

    try:
        text = strip_wrapping(client.complete(prompt))
    except AIError as exc:
        warn("播报稿改写失败：%s，用事件概括直接拼接" % exc)
        text = ""

    if not text:
        # 降级：手工拼一段能直接朗读的稿子，绝不空着
        text = "各位好，今天是%s，欢迎收听今日国际大事。" % date_text
        text += "".join(s + "。" for s in summary_only)

    # 统一在句子边界收尾，绝不再用 text[:900] 硬切。
    # 必须先给结束语预留字数，否则「截断到 900 + 追加结束语 18 字」会变成 918 超限。
    text = truncate_at_sentence(text, BROADCAST_CHAR_LIMIT - BROADCAST_ENDING_RESERVE)
    return ensure_broadcast_ending(text, date_text, BROADCAST_CHAR_LIMIT)


def ensure_broadcast_ending(text, date_text, limit=None):
    """确保播报稿有完整的收尾句，避免突然停住。

    即使截断发生在句子边界，整篇也可能停在某条新闻中间，
    听感上仍然是「话没说完」。这里补一句结束语，顺便清掉重复的结束语。
    补完后若超过 limit，就回退到「只保证句子完整、不加结束语」。
    """
    text = (text or "").strip()
    if not text:
        return "各位好，今天是%s，欢迎收听今日国际大事。" % date_text

    ending = "以上就是今天的要闻，明天同一时间再见。"

    # 已经自然收尾的就不动（模型自己写了完整结尾）
    for natural in ("明天同一时间再见", "感谢收听", "我们明天再见"):
        if natural in text[-40:]:
            return text

    # 模型写了半截结束语（如「以上就是今天的要闻。」）→ 补全，而不是再加一整句
    for half in ("以上就是今天的要闻", "以上就是今天的播报", "以上就是今天的内容"):
        if half in text[-40:]:
            full = half + "，明天同一时间再见。"
            if limit is None or len(text) - len(half) + len(full) <= limit:
                # 把结尾的半截版替换成完整版
                head = text[:text.rfind(half)]
                return (head + full).strip()
            return text

    # 结尾不是句末标点，补个句号再接收尾语
    if text[-1] not in "。！？…!?":
        text += "。"

    candidate = text + ending
    if limit is not None and len(candidate) > limit:
        # 加结束语会超限：退而求其次，只保证以完整句子收尾
        return text
    return candidate


def is_valid_mp3(path):
    """按文件头判断是不是真的 MP3。

    教训：按文件大小猜会把 5.5MB 的 JSON 当音频写进文件，页面放出来是乱码。
    合法头：前 3 字节 "ID3"，或首字节 0xFF 且 (byte[1] & 0xE0) == 0xE0。
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    if len(head) < 3:
        return False
    if head[:3] == b"ID3":
        return True
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    return False


async def _tts_save(text, path, voice=None, rate=None):
    """真正调用 edge-tts 的协程。"""
    import edge_tts

    kwargs = {"voice": voice or TTS_VOICE}
    if rate:
        kwargs["rate"] = rate
    communicate = edge_tts.Communicate(text, **kwargs)
    await asyncio.wait_for(communicate.save(path), timeout=180)


def generate_tts(text, path, attempts=TTS_ATTEMPTS, sleep=time.sleep):
    """合成 mp3，成功返回 True。

    为什么要这么多次重试：
      实测失败是服务端随机抖动，与文本长度无关。单次成功率只有 20%–60%，
      8 次重试 + 线性退避可稳定到 5/5，平均约 2 次尝试即可成功。
      另外 7.x 的失败模式是 NoAudioReceived（拿到 0 字节），
      6.x 的失败模式是 403 握手错误——两种都必须当失败处理并重试。
    """
    if not text or len(text) < 20:
        warn("播报稿太短（%d 字），跳过语音合成" % len(text or ""))
        return False

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    for attempt in range(1, attempts + 1):
        try:
            asyncio.run(_tts_save(text, path))
            if is_valid_mp3(path):
                size = os.path.getsize(path)
                log("语音合成成功：%s（%.1f KB，第 %d 次尝试）" % (
                    path, size / 1024.0, attempt))
                return True
            # 拿到文件但不是合法 MP3（可能是 0 字节或 JSON）
            got = os.path.getsize(path) if os.path.exists(path) else 0
            warn("第 %d 次合成未拿到合法 MP3（%d 字节），重试" % (attempt, got))
            _remove_quietly(path)
        except Exception as exc:  # noqa: BLE001 edge-tts 的异常类型很杂
            warn("第 %d/%d 次语音合成失败：%s: %s" % (
                attempt, attempts, type(exc).__name__, str(exc)[:120]))
            _remove_quietly(path)

        if attempt < attempts:
            # 线性退避：2s、4s、6s… 实测服务端抖动通常在几秒内恢复
            sleep(min(2 * attempt, 10))

    _remove_quietly(path)
    warn("语音合成连续 %d 次失败，降级为浏览器朗读" % attempts)
    return False


def _remove_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ============================================================================
# 九、L7 发布闸门
# ============================================================================

def publish_gate(day_entry, sections_meta):
    """发布前总闸门。全部通过才允许写文件。

    检查项：
      1. 每个有数据来源的版块 ≥3 条大事
      2. 所有对外链接都是合法 URL
      3. 每条大事的中文占比 > 60%
      4. 播报稿非空
    返回 (是否通过, 检查明细列表)。
    """
    problems = []
    checks = []

    sections = day_entry.get("sections") or {}
    for key, label in (("domestic", "国内热点"), ("international", "国际热点")):
        events = sections.get(key) or []
        available = (sections_meta.get(key) or {}).get("可用条目数", 0)
        ok = len(events) >= 3
        degraded = available < 3
        checks.append({
            "检查": "%s条数" % label,
            "值": len(events),
            "要求": "≥3 条（本版块可用素材 %d 条）" % available,
            "结果": "通过" if (ok or degraded) else "不通过",
        })
        if not ok and not degraded:
            problems.append("%s 只有 %d 条（要求 ≥3 条）" % (label, len(events)))

    # 链接合法性
    bad_links = []
    for key in ("domestic", "international"):
        for event in sections.get(key) or []:
            for media in event.get("cluster") or []:
                if not is_valid_url(media.get("url")):
                    bad_links.append(media.get("url") or "(空)")
    checks.append({
        "检查": "对外链接合法性",
        "值": "%d 个非法" % len(bad_links),
        "要求": "0 个",
        "结果": "通过" if not bad_links else "不通过",
    })
    if bad_links:
        problems.append("存在 %d 个非法链接" % len(bad_links))

    # 中文占比
    zh_texts = []
    for key in ("domestic", "international"):
        for event in sections.get(key) or []:
            zh_texts.append(event.get("zh") or "")
            zh_texts.append(event.get("importance") or "")
    joined = " ".join(zh_texts)
    ratio = chinese_ratio(joined)
    checks.append({
        "检查": "中文占比",
        "值": "%.1f%%" % (ratio * 100),
        "要求": ">60%",
        "结果": "通过" if ratio > 0.6 else "不通过",
    })
    if zh_texts and ratio <= 0.6:
        problems.append("中文占比只有 %.1f%%（要求 >60%%）" % (ratio * 100))

    # 播报稿
    has_broadcast = bool((day_entry.get("broadcast") or "").strip())
    checks.append({
        "检查": "播报稿",
        "值": "有" if has_broadcast else "空",
        "要求": "非空",
        "结果": "通过" if has_broadcast else "不通过",
    })
    if not has_broadcast:
        problems.append("播报稿为空")

    # 音频（只有声明 tts_ok 时才要求文件头合法）
    if day_entry.get("tts_ok"):
        audio_path = sections_meta.get("音频路径")
        valid = bool(audio_path) and is_valid_mp3(audio_path)
        checks.append({
            "检查": "音频文件头",
            "值": "合法" if valid else "非法",
            "要求": "ID3 或 0xFF 帧同步",
            "结果": "通过" if valid else "不通过",
        })
        if not valid:
            problems.append("声明了 tts_ok 但音频文件头不合法")
    else:
        checks.append({
            "检查": "音频文件头",
            "值": "无音频（已降级为浏览器朗读）",
            "要求": "—",
            "结果": "通过",
        })

    return (not problems), checks, problems


# ============================================================================
# 十、渲染（首页与归档页共用同一个函数，避免两套模板不一致）
# ============================================================================

BASE_CSS = """
:root{
  --bg:#f6f7f9; --card:#ffffff; --ink:#1a1d21; --muted:#6b7280;
  --line:#e5e7eb; --accent:#c62828; --accent-soft:#fdeaea; --radius:16px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;padding:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei",sans-serif;
  font-size:17px;line-height:1.65;
}
a{color:var(--accent);text-decoration:none}
.wrap{max-width:720px;margin:0 auto;padding:0 14px 64px}
header.top{padding:20px 0 12px;border-bottom:1px solid var(--line);margin-bottom:18px}
header.top h1{margin:0;font-size:23px;letter-spacing:.5px}
header.top .date{color:var(--muted);font-size:15px;margin-top:4px}
.brand-note{color:var(--muted);font-size:13px;margin-top:8px;line-height:1.5}

/* 播报区 */
.broadcast{background:var(--card);border-radius:var(--radius);padding:16px;
  margin-bottom:22px;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.broadcast h2{margin:0 0 12px;font-size:18px;display:flex;align-items:center;gap:8px}
.broadcast h2::before{content:"";width:4px;height:18px;background:var(--accent);
  border-radius:2px;display:inline-block}
audio{width:100%;height:44px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;
  min-height:48px;padding:0 20px;border:0;border-radius:12px;background:var(--accent);
  color:#fff;font-size:17px;font-weight:600;cursor:pointer;width:100%}
.btn:active{opacity:.85}
details.script{margin-top:12px;border-top:1px dashed var(--line);padding-top:10px}
details.script summary{cursor:pointer;color:var(--muted);font-size:15px;
  list-style:none;padding:6px 0}
details.script summary::-webkit-details-marker{display:none}
details.script summary::after{content:" ▾";font-size:12px}
details.script[open] summary::after{content:" ▴"}
.script-body{white-space:pre-wrap;font-size:16px;color:#333;margin-top:8px;
  background:var(--bg);border-radius:10px;padding:12px}

/* 版块 */
section.block{margin-bottom:30px}
section.block > h2{font-size:20px;margin:0 0 14px;padding-left:10px;
  border-left:5px solid var(--accent);line-height:1.3}
.empty-note{background:var(--accent-soft);color:#7a2222;border-radius:12px;
  padding:14px;font-size:15px;line-height:1.6}

/* 卡片 */
article.card{background:var(--card);border-radius:var(--radius);overflow:hidden;
  margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.card-img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#e9eaee}
.card-body{padding:14px 16px 16px}
.card-body h3{margin:0 0 8px;font-size:19px;line-height:1.45;font-weight:700}
.card-body .imp{margin:0 0 10px;color:var(--muted);font-size:15px}
.card-meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:4px}
.chip{background:var(--accent-soft);color:var(--accent);border-radius:999px;
  padding:3px 11px;font-size:13px;font-weight:600}
.toggle{width:100%;min-height:46px;margin-top:8px;border:1px solid var(--line);
  background:#fff;border-radius:12px;color:var(--ink);font-size:16px;cursor:pointer;
  font-family:inherit}
.toggle:active{background:var(--bg)}

/* 多观点展开区 */
.cluster{margin-top:8px;border-top:1px dashed var(--line);padding-top:12px}
.cluster-note{background:#fff8e1;color:#7a5c00;border-radius:10px;padding:10px 12px;
  font-size:14px;line-height:1.55;margin:0 0 12px}
.media{border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:10px}
.media.primary{border-color:#ffd0d0;background:#fffafa}
.media .src{font-size:13px;color:var(--muted);margin-bottom:6px;display:flex;
  gap:8px;align-items:center;flex-wrap:wrap}
.media .src .tag{background:var(--accent);color:#fff;border-radius:6px;
  padding:1px 8px;font-size:12px}
.media h4{margin:0 0 8px;font-size:17px;line-height:1.5}
.media h4 a{color:var(--ink)}
.media .en-title{font-size:14px;color:var(--muted);margin:0 0 8px;line-height:1.5}
.media .sum{font-size:15px;color:#333;margin:0 0 8px;line-height:1.65}
.media .sum-en{font-size:13px;color:var(--muted);border-left:3px solid var(--line);
  padding-left:10px;margin:8px 0 0;line-height:1.55}
.media .thumb{width:100%;border-radius:10px;margin-bottom:8px;display:block;
  aspect-ratio:16/9;object-fit:cover;background:#e9eaee}
.media .go{display:inline-block;min-height:44px;line-height:44px;padding:0 16px;
  border-radius:10px;background:var(--bg);font-size:15px;font-weight:600}

/* 近7天 */
section.recent{background:var(--card);border-radius:var(--radius);padding:16px;
  margin-bottom:26px}
section.recent h2{margin:0 0 10px;font-size:18px}
.recent-item{border-bottom:1px solid var(--line);padding:10px 0}
.recent-item:last-child{border-bottom:0;padding-bottom:0}
.recent-item .d{font-size:14px;color:var(--muted)}
.recent-item .t{font-size:15px;color:#333;line-height:1.55;margin-top:2px}

/* 原始新闻 */
details.raw{background:var(--card);border-radius:var(--radius);padding:16px;
  margin-bottom:20px}
details.raw summary{cursor:pointer;font-size:17px;font-weight:600;list-style:none}
details.raw summary::-webkit-details-marker{display:none}
details.raw summary::after{content:" ▾";font-size:13px;color:var(--muted)}
details.raw[open] summary::after{content:" ▴"}
.raw-item{padding:11px 0;border-bottom:1px solid var(--line)}
.raw-item:last-child{border-bottom:0}
.raw-item .zh{font-size:15px;line-height:1.6}
.raw-item .en{font-size:13px;color:var(--muted);margin-top:3px;line-height:1.5}
.raw-item .meta{font-size:12px;color:var(--muted);margin-top:3px}
footer{border-top:1px solid var(--line);padding:22px 0 10px;color:var(--muted);
  font-size:13px;line-height:1.8;text-align:center}
footer a{color:var(--muted);text-decoration:underline}
@media (min-width:600px){
  body{font-size:18px}
  .wrap{padding:0 20px 72px}
  .card-img{aspect-ratio:2/1}
  .btn{width:auto}
}
"""

BASE_JS = """
(function () {
  'use strict';

  /* 展开/收起「查看各家报道」 */
  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('.toggle') : null;
    if (!button) { return; }
    var panel = document.getElementById(button.getAttribute('data-target'));
    if (!panel) { return; }
    var open = panel.hasAttribute('hidden') === false;
    if (open) {
      panel.setAttribute('hidden', '');
      button.textContent = button.getAttribute('data-label-closed');
    } else {
      panel.removeAttribute('hidden');
      button.textContent = '收起各家报道 ▴';
    }
  });

  /* 无 mp3 时用浏览器自带的 SpeechSynthesis 朗读播报稿 */
  var speaking = false;
  window.dailyNewsSpeak = function () {
    if (!('speechSynthesis' in window)) {
      alert('当前浏览器不支持语音朗读，请直接阅读下方文字播报稿。');
      return;
    }
    var holder = document.getElementById('broadcast-text');
    if (!holder) { return; }
    var text = (holder.textContent || '').trim();
    if (!text) { return; }

    if (speaking) {
      window.speechSynthesis.cancel();
      speaking = false;
      return;
    }

    var utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'zh-CN';
    utterance.rate = 1.0;

    function pickVoiceAndSpeak() {
      var voices = window.speechSynthesis.getVoices() || [];
      for (var i = 0; i < voices.length; i += 1) {
        if (/^zh[-_]CN/i.test(voices[i].lang) || /^zh/i.test(voices[i].lang)) {
          utterance.voice = voices[i];
          break;
        }
      }
      speaking = true;
      utterance.onend = function () { speaking = false; };
      utterance.onerror = function () { speaking = false; };
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(utterance);
    }

    var ready = window.speechSynthesis.getVoices() || [];
    if (ready.length) {
      pickVoiceAndSpeak();
      return;
    }
    window.speechSynthesis.onvoiceschanged = function () {
      window.speechSynthesis.onvoiceschanged = null;
      pickVoiceAndSpeak();
    };
    setTimeout(function () {
      if (!speaking && !utterance.voice) { pickVoiceAndSpeak(); }
    }, 900);
  };
}());
"""


def _render_media(media, is_primary, event_summary=""):
    """渲染一条媒体报道。

    event_summary 是卡片顶部已展示的事件概括。若这条媒体的标题与它几乎一样，
    展开后就不再重复显示一遍标题，只保留来源、英文原文与链接——
    否则用户点开「看各家报道」看到的还是刚才那句话，等于没有新信息。
    """
    source = esc(media.get("publisher") or media.get("source") or "未知来源")
    title_zh = (media.get("title_zh") or "").strip()
    title_en = (media.get("title_en") or "").strip()
    summary_zh = esc(media.get("summary_zh") or "")
    summary_en = (media.get("summary_en") or "").strip()
    url = media.get("url") or ""
    image = media.get("image") or ""

    # 标题与事件概括高度重合（相似度 > 0.9）时不重复展示
    show_title = bool(title_zh) and text_similarity(title_zh, event_summary) <= 0.9

    parts = ['<div class="media%s">' % (" primary" if is_primary else "")]
    parts.append('<div class="src">')
    if is_primary:
        parts.append('<span class="tag">头条</span>')
    parts.append("<span>%s</span></div>" % source)

    if image:
        classes = "thumb" if not is_primary else ""
        lazy = ' loading="lazy"' if not is_primary else ""
        parts.append(
            '<img class="%s" src="%s" alt="%s" referrerpolicy="no-referrer"%s '
            'onerror="this.style.display=\'none\'">'
            % (classes, esc(image), esc(title_zh), lazy)
        )

    if show_title:
        if is_valid_url(url):
            parts.append(
                '<h4><a href="%s" target="_blank" rel="noopener noreferrer">%s</a></h4>'
                % (esc(url), esc(title_zh))
            )
        else:
            parts.append("<h4>%s</h4>" % esc(title_zh))

    if title_en:
        parts.append('<p class="en-title" lang="en">%s</p>' % esc(title_en))
    if summary_zh:
        parts.append('<p class="sum">%s</p>' % summary_zh)
    # 只有确实是英文原文时才标 lang="en"，否则中文摘要被标成英文会误导浏览器翻译
    if summary_en and summary_en != media.get("summary_zh") and \
            not is_chinese_text(summary_en):
        parts.append('<p class="sum-en" lang="en">%s</p>' % esc(summary_en))
    elif summary_en and summary_en != media.get("summary_zh"):
        # 中文原文摘要（中新网等），去掉 lang="en" 标记
        parts.append('<p class="sum-en">%s</p>' % esc(summary_en))
    if is_valid_url(url):
        parts.append(
            '<a class="go" href="%s" target="_blank" rel="noopener noreferrer">'
            "阅读原文 ↗</a>" % esc(url)
        )
    parts.append("</div>")
    return "".join(parts)


def _render_theme_member(member):
    """渲染主题组里的单条事件（二级展示）。

    这是「同一系列的多个事件」，不是「同一事件的多家报道」，
    所以每条都要显示自己的标题，并附上它的来源列表。
    """
    title = esc(member.get("zh") or "")
    importance = esc(member.get("importance") or "")
    count = member.get("source_count") or 0
    cluster = member.get("cluster") or []

    parts = ['<div class="media">']
    parts.append('<div class="src">')
    sources = []
    for media in cluster[:4]:
        name = media.get("publisher") or media.get("source") or ""
        if name and name not in sources:
            sources.append(name)
    if sources:
        parts.append("<span>%s</span>" % esc(" · ".join(sources)))
    elif count:
        parts.append("<span>%d 家媒体报道</span>" % count)
    parts.append("</div>")
    parts.append("<h4>%s</h4>" % title)
    if importance:
        parts.append('<p class="sum">%s</p>' % importance)

    # 每条事件挑一个主链接
    link = ""
    for media in cluster:
        if is_valid_url(media.get("url")):
            link = media["url"]
            break
    if link:
        parts.append(
            '<a class="go" href="%s" target="_blank" rel="noopener noreferrer">'
            "阅读原文 ↗</a>" % esc(link)
        )
    parts.append("</div>")
    return "".join(parts)


def _render_card(event, index):
    """渲染一条大事卡片：默认极简，多观点折叠。

    支持两种卡片：
      · 普通卡片：一个事件 + 多家媒体的报道（cluster 折叠）
      · 主题卡片：同一系列的多个事件（events 折叠），主卡片显示共同概括
    """
    cid = esc(event.get("cid") or ("e%d" % index))
    zh = esc(event.get("zh") or "")
    importance = esc(event.get("importance") or "")
    image = event.get("image") or ""
    cluster = event.get("cluster") or []
    count = event.get("source_count") or len(cluster) or 0
    theme_members = event.get("events") or []
    is_theme = bool(event.get("theme")) and len(theme_members) > 1
    panel_id = "cluster-%s" % cid

    parts = ['<article class="card">']
    if image:
        parts.append(
            '<img class="card-img" src="%s" alt="%s" referrerpolicy="no-referrer" '
            'onerror="this.style.display=\'none\'">' % (esc(image), zh)
        )
    parts.append('<div class="card-body">')
    parts.append("<h3>%s</h3>" % zh)
    if importance:
        parts.append('<p class="imp">%s</p>' % importance)

    parts.append('<div class="card-meta">')
    if is_theme:
        # 主题卡片显示「相关动态 N 条」，比「N 家媒体报道」更贴切
        parts.append('<span class="chip">相关动态 %d 条</span>' % len(theme_members))
    elif count >= 2:
        parts.append('<span class="chip">%d 家媒体报道</span>' % count)
    parts.append("</div>")

    if is_theme:
        label_closed = "查看这 %d 条动态 ▸" % len(theme_members)
        parts.append(
            '<button class="toggle" type="button" data-target="%s" '
            'data-label-closed="%s">%s</button>'
            % (panel_id, esc(label_closed), esc(label_closed))
        )
        parts.append('<div class="cluster" id="%s" hidden>' % panel_id)
        parts.append(
            '<p class="cluster-note">以下是同一主题下的 %d 条动态，'
            "点开任意一条可看媒体原文。</p>" % len(theme_members)
        )
        for member in theme_members:
            parts.append(_render_theme_member(member))
        parts.append("</div>")
    elif len(cluster) >= 1:
        label_closed = "查看各家报道 ▸"
        parts.append(
            '<button class="toggle" type="button" data-target="%s" '
            'data-label-closed="%s">%s</button>'
            % (panel_id, label_closed, label_closed)
        )
        parts.append('<div class="cluster" id="%s" hidden>' % panel_id)
        if count >= 2 or len(cluster) >= 2:
            parts.append(
                '<p class="cluster-note">以下报道来自 %d 家媒体，观点可能不同，'
                "请自行判断。</p>" % max(count, len(cluster))
            )
        ordered = sorted(
            cluster, key=lambda m: 0 if m.get("is_primary") else 1
        )
        for media in ordered:
            parts.append(_render_media(
                media, bool(media.get("is_primary")), event_summary=zh
            ))
        parts.append("</div>")

    parts.append("</div></article>")
    return "".join(parts)


def _render_broadcast(day, is_today):
    """渲染顶部播报区：有 mp3 给播放器，没有就降级为浏览器朗读。"""
    broadcast = day.get("broadcast") or ""
    tts_ok = bool(day.get("tts_ok"))

    parts = ['<div class="broadcast">', "<h2>今日播报</h2>"]

    if is_today and tts_ok:
        parts.append(
            '<audio controls preload="none" src="audio/daily.mp3">'
            "你的浏览器不支持音频播放。</audio>"
        )
    elif is_today and not tts_ok:
        parts.append(
            '<button class="btn" type="button" onclick="dailyNewsSpeak()">'
            "🔊 朗读播报（约 2 分钟）</button>"
        )
    else:
        parts.append(
            '<p style="color:#6b7280;font-size:15px;margin:0">'
            "音频仅当日有效，历史归档只保留文字播报稿。</p>"
        )

    if broadcast:
        parts.append('<details class="script"><summary>展开播报稿全文</summary>')
        parts.append(
            '<div class="script-body" id="%s">%s</div>'
            % ("broadcast-text" if is_today else "broadcast-text-archive",
               esc(broadcast))
        )
        parts.append("</details>")

    parts.append("</div>")
    return "".join(parts)


def _render_section(key, label, events, degraded_note, available_count, is_today):
    """渲染一个版块。"""
    parts = ['<section class="block"><h2>%s</h2>' % esc(label)]

    if not events:
        note = degraded_note or "今天这个版块暂时没有抓到有效新闻，请稍后再看。"
        parts.append('<p class="empty-note">%s</p>' % esc(note))
    else:
        for index, event in enumerate(events, start=1):
            parts.append(_render_card(event, index))
        if degraded_note:
            parts.append('<p class="empty-note">%s</p>' % esc(degraded_note))

    parts.append("</section>")
    return "".join(parts)


def _render_recent(recent_days):
    """渲染「最近 N 天」摘要列表。"""
    if not recent_days:
        return ""
    parts = ['<section class="recent"><h2>最近 %d 天</h2>' % len(recent_days)]
    for day in recent_days:
        date = day.get("date") or ""
        sections = day.get("sections") or {}
        headlines = []
        for key in ("domestic", "international"):
            for event in (sections.get(key) or [])[:2]:
                if event.get("zh"):
                    headlines.append(event["zh"])
        text = " ｜ ".join(headlines[:3]) or "（当日无有效新闻）"
        parts.append(
            '<div class="recent-item"><div class="d">'
            '<a href="archive/%s.html">%s</a></div>'
            '<div class="t">%s</div></div>' % (esc(date), esc(date), esc(text))
        )
    parts.append("</section>")
    return "".join(parts)


def _render_raw_news(raw_news):
    """渲染底部可折叠的原始新闻列表（英文标题下配中文译文）。"""
    if not raw_news:
        return ""
    parts = [
        '<details class="raw"><summary>原始新闻列表（%d 条）</summary>'
        % len(raw_news)
    ]
    for item in raw_news[:60]:
        zh = esc(item.get("zh") or "")
        en = (item.get("en") or "").strip()
        source = esc(item.get("source") or "")
        url = item.get("url") or ""
        parts.append('<div class="raw-item">')
        if zh and is_valid_url(url):
            parts.append(
                '<div class="zh"><a href="%s" target="_blank" '
                'rel="noopener noreferrer">%s</a></div>' % (esc(url), zh)
            )
        elif zh:
            parts.append('<div class="zh">%s</div>' % zh)
        if en:
            parts.append('<div class="en" lang="en">%s</div>' % esc(en))
        parts.append('<div class="meta">%s</div>' % source)
        parts.append("</div>")
    parts.append("</details>")
    return "".join(parts)


def render_page(day_data, is_today, recent_days, site_url):
    """渲染一整页 HTML。首页与归档页共用，保证两边绝对一致。

    day_data    一天的完整数据（数据契约里的一个 day 对象）
    is_today    是否今日页。今日才有音频；归档页注明「音频仅当日有效」
    recent_days 首页用，最近若干天的摘要数据；归档页传 None
    site_url    站点根地址，用于 canonical 与分享信息
    """
    day = day_data or {}
    date_text = day.get("date") or ""
    sections = day.get("sections") or {}
    domestic = sections.get("domestic") or []
    international = sections.get("international") or []
    notes = day.get("section_notes") or {}

    home_prefix = "" if is_today else "../"

    head = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1,'
        'viewport-fit=cover">',
        "<title>%s%s</title>" % (
            "%s · " % esc(date_text) if date_text else "",
            "每日国际大事 · 中文播报",
        ),
        '<meta name="description" content="%s">' % esc(
            "每天更新的中文新闻播报站：国内外热点、多家媒体的不同观点，"
            "在中国大陆无需翻墙即可打开。"
        ),
        '<meta name="theme-color" content="#c62828">',
    ]
    if site_url:
        canonical = site_url.rstrip("/")
        if is_today:
            head.append('<link rel="canonical" href="%s/">' % esc(canonical))
        else:
            head.append(
                '<link rel="canonical" href="%s/archive/%s.html">'
                % (esc(canonical), esc(date_text))
            )
    head.append("<style>%s</style>" % BASE_CSS)
    head.append("</head>")
    head.append("<body>")

    body = ['<div class="wrap">']

    # 顶部
    body.append('<header class="top">')
    body.append('<h1><a href="%sindex.html" style="color:inherit">'
                "每日国际大事</a></h1>" % home_prefix)
    if date_text:
        body.append('<div class="date">%s</div>' % esc(date_text))
    body.append(
        '<div class="brand-note">国内热点与国际热点并排呈现，'
        "同一事件并列多家媒体的报道与观点，点开卡片可见各家原文。</div>"
    )
    body.append("</header>")

    # 播报
    if day.get("broadcast"):
        body.append(_render_broadcast(day, is_today))

    if not is_today:
        body.append(
            '<p class="empty-note" style="margin-bottom:20px">'
            "这是 %s 的归档页，音频仅当日有效。</p>" % esc(date_text)
        )

    # 两个版块
    body.append(_render_section(
        "domestic", "国内热点", domestic,
        notes.get("domestic"), notes.get("domestic_available", 0), is_today,
    ))
    body.append(_render_section(
        "international", "国际热点", international,
        notes.get("international"), notes.get("international_available", 0), is_today,
    ))

    # 最近 7 天
    if is_today:
        body.append(_render_recent(recent_days))

    # 原始新闻
    body.append(_render_raw_news(day.get("raw_news") or []))

    # 页脚
    body.append("<footer>")
    body.append(
        "本站由 GitHub Actions 每日北京时间 08:00 自动更新，"
        "内容来自公开 RSS，仅供个人阅读参考。"
    )
    if is_today:
        body.append('<br>归档保留 %d 天：' % KEEP_DAYS)
        if recent_days:
            for recent in recent_days[:7]:
                body.append(
                    ' <a href="archive/%s.html">%s</a>'
                    % (esc(recent.get("date")), esc(recent.get("date")))
                )
    body.append("</footer>")
    body.append("</div>")

    scripts = ["<script>%s</script>" % BASE_JS]
    return "\n".join(head + body + scripts) + "\n</body>\n</html>\n"


# ============================================================================
# 十一、清理（30 天归档）
# ============================================================================

def prune_days(days, today, keep_days=KEEP_DAYS):
    """只保留最近 keep_days 天的数据（按日期倒序返回）。"""
    kept = []
    for day in days:
        parsed = parse_date(day.get("date"))
        if parsed is None:
            continue
        age = (today - parsed).days
        if 0 <= age < keep_days or age == 0:
            kept.append(day)
        elif age < 0:
            # 未来日期（时钟漂移），保留但不参与排序异常
            kept.append(day)
    kept.sort(key=lambda d: str(d.get("date")), reverse=True)
    return kept


def prune_archive_files(archive_dir, keep_dates):
    """删除不再保留的归档 HTML。返回被删除的文件名列表。"""
    removed = []
    if not os.path.isdir(archive_dir):
        return removed
    keep = {str(d) for d in keep_dates}
    for name in sorted(os.listdir(archive_dir)):
        if not name.endswith(".html"):
            continue
        stem = name[:-len(".html")]
        if parse_date(stem) is None:
            continue
        if stem not in keep:
            path = os.path.join(archive_dir, name)
            try:
                os.remove(path)
                removed.append(name)
            except OSError:
                pass
    return removed


def prune_audio(audio_dir, keep_name="daily.mp3"):
    """音频只保留最新一天：删掉除 daily.mp3 之外的所有文件。"""
    removed = []
    if not os.path.isdir(audio_dir):
        return removed
    for name in sorted(os.listdir(audio_dir)):
        path = os.path.join(audio_dir, name)
        if not os.path.isfile(path):
            continue
        if name != keep_name:
            try:
                os.remove(path)
                removed.append(name)
            except OSError:
                pass
    return removed


# ============================================================================
# 十二、L0 准备 / L8 输出
# ============================================================================

class Paths:
    """输出路径集合。dry-run 时整组指向 _dryrun/，绝不碰真实站点文件。"""

    def __init__(self, base_dir, dry_run=False):
        self.dry_run = dry_run
        self.root = os.path.abspath(base_dir)
        self.out = os.path.join(self.root, "_dryrun") if dry_run else self.root
        self.index = os.path.join(self.out, "index.html")
        self.archive = os.path.join(self.out, "archive")
        self.data = os.path.join(self.out, "data")
        self.runs = os.path.join(self.data, "runs")
        self.entries = os.path.join(self.data, "entries.json")
        self.audio = os.path.join(self.out, "audio")
        self.mp3 = os.path.join(self.audio, "daily.mp3")
        self.tmp = os.path.join(self.out, ".tmp")

    def ensure(self):
        for directory in (self.out, self.archive, self.data, self.runs, self.audio):
            os.makedirs(directory, exist_ok=True)

    def describe(self):
        return "[演练模式] 输出目录 %s" % self.out if self.dry_run else "输出目录 %s" % self.out


def load_entries(paths):
    """读历史 entries.json。没有就当第一天（降级，不报错）。"""
    if not os.path.exists(paths.entries):
        return []
    try:
        with open(paths.entries, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        warn("历史数据读取失败（%s），按第一天处理" % exc)
        return []
    days = payload.get("days") if isinstance(payload, dict) else payload
    if not isinstance(days, list):
        warn("历史数据格式不对，按第一天处理")
        return []
    return days


def write_json(path, payload):
    """写 JSON（UTF-8，不转义中文，缩进 2）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def write_text(path, text):
    """写文本文件（原子替换）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def backup_audio(paths):
    """L0 第一件事：把旧 mp3 挪走。

    为什么不是直接删：如果今天发布闸门不通过，按规矩「不写任何文件」，
    页面要保留上一版——上一版还引用着 audio/daily.mp3。
    所以先挪到 .tmp/ 藏起来（页面在本次运行期间不会再引用昨天的音频），
    闸门通过后彻底删除，闸门不通过则原样还原。
    """
    if not os.path.exists(paths.mp3):
        return None
    os.makedirs(paths.tmp, exist_ok=True)
    backup = os.path.join(paths.tmp, "prev_daily.mp3")
    try:
        shutil.move(paths.mp3, backup)
        log("已移走昨天的音频，避免今天语音合成失败时配昨天的声音")
        return backup
    except OSError as exc:
        warn("旧音频挪走失败：%s" % exc)
        _remove_quietly(paths.mp3)
        return None


def restore_audio(paths, backup):
    """发布闸门不通过时，把旧音频还原回去。"""
    if not backup or not os.path.exists(backup):
        return
    try:
        os.makedirs(paths.audio, exist_ok=True)
        shutil.move(backup, paths.mp3)
        log("发布闸门未通过：已还原上一版音频")
    except OSError as exc:
        warn("旧音频还原失败：%s" % exc)


# ============================================================================
# 十三、构造一天的数据
# ============================================================================

def build_day(section_inputs, client, today, tts_broadcast_text=None):
    """跑 L3–L6，产出一整天的 day 数据结构（还不落地）。

    section_inputs: {"domestic": [原始条目], "international": [原始条目]}
    返回 (day_entry, meta)，meta 里带闸门日志等辅助信息。
    """
    sections = {}
    section_notes = {}
    gate_logs = {}
    all_clusters = {}

    labels = {"domestic": "国内", "international": "国际"}
    prefixes = {"domestic": "c", "international": "i"}

    for key in ("domestic", "international"):
        raw_items = section_inputs.get(key) or []
        # 先聚类并排序（家数优先、时间倒序），再按媒体配额挑出最终入选的事件，
        # 避免发稿量大的媒体（如一天发 24 条的 France 24）把席位占满。
        candidates = build_clusters(raw_items, prefixes[key])
        clusters, dropped = apply_publisher_quota(candidates)
        if dropped:
            log("  %s版块按媒体配额舍去 %d 条（保留 %d 条）" % (
                labels[key], len(dropped), len(clusters)))
        events_ordered, gate_log = build_section_events(
            clusters, labels[key], client
        )
        gate_logs[key] = gate_log

        events = []
        for cid, (summary, importance) in events_ordered:
            cluster = next(c for c in clusters if c["cid"] == cid)
            primary = cluster["items"][0]
            cluster_media = []
            for index, item in enumerate(cluster["items"]):
                is_primary = index == 0
                media = {
                    "source": item.get("source") or "",
                    "publisher": item_publisher(item),
                    "title_zh": item.get("title_zh") or item.get("title") or "",
                    "title_en": item.get("title_en") or "",
                    "url": item.get("url") or "",
                    "summary_zh": item.get("summary_zh") or "",
                    "image": item.get("image") or "",
                    "is_primary": is_primary,
                }
                # 英文原文摘要只在英文源上提供，供对照
                if item.get("lang") != "zh" and item.get("summary_en"):
                    media["summary_en"] = item["summary_en"]
                elif item.get("lang") != "zh" and item.get("summary"):
                    media["summary_en"] = item["summary"]
                cluster_media.append(media)

            events.append({
                "cid": cid,
                "zh": summary,
                "importance": importance,
                "source_count": vote_count(cluster["items"]),
                "image": primary.get("image") or "",
                "cluster": cluster_media,
            })

        # 同主题归并：把「习近平会见X国领导人」这类同一系列的多条事件
        # 归到一个主题组下，页面渲染成一张主卡片 + 可展开的条目列表。
        # 注意这不会丢事件：每个主题组里仍然保留全部原始事件。
        events = group_events_by_theme(events, labels[key], client)

        sections[key] = events
        section_notes[key] = ""
        section_notes["%s_available" % key] = len(raw_items)
        all_clusters[key] = clusters

    # 国内源全挂时，页面注明「从国际源筛选涉华报道」
    if section_inputs.get("_domestic_fallback"):
        section_notes["domestic"] = (
            "中文国内源今天全部不可用，本版块暂时退化为"
            "「从国际源里筛选涉及中国的报道」。"
        )
    if not (section_inputs.get("domestic") or []):
        section_notes["domestic"] = section_notes["domestic"] or (
            "今天没有抓到国内版块的有效新闻，可能中文源暂时不可用。"
        )
    if not (section_inputs.get("international") or []):
        section_notes["international"] = "今天没有抓到国际版块的有效新闻，请稍后再看。"

    # 播报稿
    if tts_broadcast_text is not None:
        broadcast = tts_broadcast_text
    else:
        broadcast = build_broadcast(
            [("国内", sections.get("domestic") or []),
             ("国际", sections.get("international") or [])],
            client, today,
        )

    day_entry = {
        "date": today.strftime("%Y-%m-%d"),
        "broadcast": broadcast,
        "tts_ok": False,  # 由调用方在合成语音后回填
        "sections": {
            "domestic": sections.get("domestic") or [],
            "international": sections.get("international") or [],
        },
        "section_notes": section_notes,
        "raw_news": build_raw_news(section_inputs),
    }

    meta = {"gate_logs": gate_logs, "clusters": all_clusters}
    return day_entry, meta


def build_raw_news(section_inputs):
    """构造底部「原始新闻列表」。"""
    rows = []
    seen = set()
    for key in ("domestic", "international"):
        for item in section_inputs.get(key) or []:
            url = item.get("url") or ""
            if url in seen:
                continue
            seen.add(url)
            rows.append({
                "en": item.get("title_en") or ("" if item.get("lang") == "zh" else item.get("title") or ""),
                "zh": item.get("title_zh") or item.get("title") or "",
                "url": url,
                "source": item.get("source") or "",
                "image": item.get("image") or "",
                "section": key,
            })
    return rows


# ============================================================================
# 十四、主流程
# ============================================================================

def run(dry_run=False, dry_run_tts_ok=False, root=None):
    """跑完整条流水线。返回退出码（0 表示正常，含「不发布」的良性退出）。"""
    started = time.time()
    run_id = now_bj().strftime("%Y%m%dT%H%M%S")
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = Paths(root, dry_run=dry_run)
    paths.ensure()

    today = today_bj()
    log("=" * 66)
    log("每日新闻播报站 · 开始运行  run_id=%s  %s" % (run_id, paths.describe()))
    log("=" * 66)

    # ---------------- L0 准备 ----------------
    log("L0 准备：读历史数据、清理今日临时产物")

    # 演练模式：先注入历史（昨天 + 31 天前），这样 30 天清理才真的被验证到
    if dry_run and not os.path.exists(paths.entries):
        seed_dry_run_history(paths, today)

    history = load_entries(paths)
    log("历史数据：%d 天（%s）" % (
        len(history),
        "最早 %s，最近 %s" % (history[-1].get("date"), history[0].get("date"))
        if history else "无，按第一天处理",
    ))
    audio_backup = backup_audio(paths)

    # ---------------- L1 采集 ----------------
    log("L1 采集：并发抓取国际源 %d 个、国内源 %d 个" % (
        len(INTL_FEEDS), len(DOMESTIC_FEEDS)))

    if dry_run:
        http_get = make_fake_http(today)
    else:
        http_get = http_get_bytes

    intl_raw, intl_status = collect_all(INTL_FEEDS, "international", http_get=http_get)
    # 国内版块 = 大陆官方源（中新网）+ 海外中文媒体。
    # 两者都归到 domestic 版块，但 publisher 不同，所以「家数」计票是正确的。
    # 没有海外中文源时国内版块就只有中新网一家（线上真实发生过）。
    dom_sources = list(DOMESTIC_FEEDS) + list(OVERSEAS_ZH_FEEDS)
    dom_raw, dom_status = collect_all(dom_sources, "domestic", http_get=http_get)

    # 热榜源（澎湃精选）单独串行抓取：RSSHub 公共实例有严格限流，
    # 和上面的并发抓取混在一起会 429。串行 + 间隔才稳定（实测 3/3 成功）。
    hot_raw, hot_status = [], []
    for source in HOT_FEEDS:
        items, error = fetch_source(source, "domestic", http_get=http_get)
        hot_status.append({
            "源": source["name"],
            "版块": "domestic",
            "条目数": len(items),
            "状态": "正常" if items else "失败",
            "说明": error,
        })
        if items:
            hot_raw.extend(items)
            log("热榜源 %s 抓到 %d 条（排序时加权）" % (source["name"], len(items)))
        else:
            warn("热榜源 %s 抓取失败：%s（不影响其它源）" % (source["name"], error))
        time.sleep(HOT_FEED_DELAY)
    dom_raw.extend(hot_raw)
    dom_status.extend(hot_status)

    log("采集结果：国际 %d 条、国内 %d 条（含 %d 个热榜源、%d 个海外中文媒体）" % (
        len(intl_raw), len(dom_raw), len(HOT_FEEDS), len(OVERSEAS_ZH_FEEDS)))

    # ---------------- L2 规范化 ----------------
    log("L2 规范化：近 %d 小时过滤、去重、软新闻过滤、图片归一" % WINDOW_HOURS)
    intl_items, intl_stats = normalize_items(intl_raw, now=now_bj())
    dom_items, dom_stats = normalize_items(dom_raw, now=now_bj())
    log("国际：%s" % intl_stats)
    log("国内：%s" % dom_stats)

    # 海外中文媒体是综合媒体：法广RFI / 纽约时报中文网 的 RSS 里既有涉华新闻
    # 也有纯国际新闻。必须按「是否涉华」分流，否则会出现
    # 「乌克兰袭击克里米亚大桥」出现在「国内热点」版块（线上真实发生过）。
    #
    # ⚠️ 这里必须用返回的两个列表**整体替换**，不能像早期实现那样写成
    #    `intl_items = intl_items + intl_extra` —— 因为 route_overseas_items
    #    返回的 intl 是「输入里所有国际条目」，不是「本次新增的国际条目」，
    #    拼接会让国际条目整体翻倍（线上日志实测出现 189 → 392 条）。
    # 按「源 scope + 内容」统一分流到国内/国际版块。
    #
    # 这是「国内版块混进国外新闻」的根本修复：
    #   早期实现只对「海外中文媒体白名单」做内容判定，而中新网不在名单里——
    #   但中新网的即时/要闻/社会频道恰恰混着大量国际新闻
    #   （实测 106 条里 38 条不涉华：「韩国高考防作弊」「日本隼鸟2号探测器」
    #    「欧盟经济增长好于预期」）。名单式方案漏一个源就泄漏。
    #   现在改为：每个源在配置里声明 scope，mixed 源逐条判定，不依赖任何白名单。
    before_dom = len(dom_items)
    dom_items, intl_items = assign_sections(dom_items + intl_items)
    moved_out = before_dom - len(dom_items)
    if moved_out:
        log("版块分流：%d 条非涉华新闻已从国内版块移到国际版块" % moved_out)

    # 自检：国内版块里绝不允许出现「mixed/intl 源且明确不涉华」的条目
    leaked = [
        it for it in dom_items
        if (it.get("scope") == "intl")
        or (it.get("scope") == "mixed"
            and not is_china_related(it, include_summary=False))
    ]
    if leaked:
        warn("分流自检发现国内版块仍有 %d 条国外新闻，已强制移除" % len(leaked))
        leaked_ids = {id(it) for it in leaked}
        dom_items = [it for it in dom_items if id(it) not in leaked_ids]
        for it in leaked:
            it["section"] = "international"
            intl_items.append(it)

    # 国内源全挂 → 退化为从国际源筛选涉华报道，并在页面注明
    domestic_fallback = False
    if not dom_items and intl_items:
        dom_items = filter_china_related(intl_items)
        domestic_fallback = True
        warn("国内中文源全部不可用，已退化为从国际源筛选涉华报道（%d 条）" % len(dom_items))

    if not intl_items and not dom_items:
        warn("国际与国内总共 0 条有效新闻（抓取可能全线失败）")
        warn("按空数据守卫：不写任何文件，保留上一版页面，正常退出")
        restore_audio(paths, audio_backup)
        _write_run_report(paths, run_id, {
            "run_id": run_id,
            "结果": "空数据守卫退出，未写任何文件",
            "国际源状态": intl_status,
            "国内源状态": dom_status,
            "国际规范化": intl_stats,
            "国内规范化": dom_stats,
        })
        return 0

    # ---------------- L3–L6 ----------------
    client = FakeAIClient(today) if dry_run else AIClient()
    if not dry_run and not client.configured:
        warn("缺少 CF_ACCOUNT_ID / CF_API_TOKEN，AI 无法调用")
        warn("不写任何文件，保留上一版页面，正常退出（请检查 GitHub Secrets）")
        restore_audio(paths, audio_backup)
        return 0

    log("L3 理解：分批翻译标题（每批 %d 条）" % TRANSLATE_BATCH)
    intl_items = intl_items  # 原地写入 title_zh
    trans_stats_intl = translate_titles(intl_items, client)
    trans_stats_dom = translate_titles(dom_items, client)
    log("标题翻译：国际 %s / 国内 %s" % (trans_stats_intl, trans_stats_dom))

    log("L3 理解：中文 bigram 聚类 + AI 生成今日大事")
    section_inputs = {
        "international": intl_items,
        "domestic": dom_items,
        "_domestic_fallback": domestic_fallback,
    }
    day_entry, meta = build_day(section_inputs, client, today)

    for key, label in (("domestic", "国内"), ("international", "国际")):
        events = day_entry["sections"][key]
        log("  %s版块：%d 条大事" % (label, len(events)))
        for event in events:
            log("    [%s] %s — %s（%d 家）" % (
                event["cid"], event["zh"], event["importance"] or "—",
                event["source_count"]))

    log("L4 校验闸门：国家实体对账结果")
    for key, logs in meta["gate_logs"].items():
        passed = sum(1 for row in logs if row["结果"] in ("通过", "兜底补齐"))
        log("  %s 版块：%d 条入闸，%d 条通过" % (key, len(logs), passed))
        for row in logs:
            if row["结果"] != "通过":
                log("    %s %s：%s" % (row["cid"], row["结果"],
                                      row["详情"].get("原因", "")))

    log("L5 深挖：抓正文并分段翻译（最多 %d 篇）" % DEEP_DIVE_COUNT)
    deep_count = 0
    for key in ("international", "domestic"):
        clusters = meta["clusters"][key]
        # 只对最终入选的簇做深挖
        chosen_cids = {event["cid"] for event in day_entry["sections"][key]}
        chosen = [c for c in clusters if c["cid"] in chosen_cids]
        deep_count += deep_dive(
            chosen, client, limit=DEEP_DIVE_COUNT // 2, http_get=http_get
        )
    log("全文翻译完成：%d 篇" % deep_count)

    # L5.5 补图：深挖只覆盖少数几篇，其余入选事件的卡片还是没图。
    # 中文源 RSS 普遍不带图，所以这里对「入选但无图」的事件逐个抓 og:image。
    backfilled = backfill_images(day_entry, meta, http_get=http_get)
    if backfilled:
        log("补图完成：%d 个事件从正文页取到了主图" % backfilled)

    # 深挖后把 summary_zh 回填到事件的 cluster 里
    _refresh_event_summaries(day_entry, meta)

    log("L6 播报：生成播报稿并合成语音")
    if not day_entry.get("broadcast"):
        warn("播报稿为空，语音降级")
    if dry_run:
        if dry_run_tts_ok:
            write_fake_mp3(paths.mp3)
            day_entry["tts_ok"] = is_valid_mp3(paths.mp3)
            log("演练：模拟语音合成成功（tts_ok=%s）" % day_entry["tts_ok"])
        else:
            day_entry["tts_ok"] = False
            log("演练：模拟语音合成失败，走浏览器朗读降级分支（tts_ok=False）")
    else:
        day_entry["tts_ok"] = generate_tts(day_entry.get("broadcast") or "", paths.mp3)

    # ---------------- L7 发布闸门 ----------------
    log("L7 发布闸门")
    sections_meta = {
        key: {"可用条目数": (section_inputs.get(key) or []).__len__()}
        for key in ("domestic", "international")
    }
    sections_meta["音频路径"] = paths.mp3
    passed, checks, problems = publish_gate(day_entry, sections_meta)
    for check in checks:
        log("  %s：%s / 要求 %s → %s" % (
            check["检查"], check["值"], check["要求"], check["结果"]))
    if not passed:
        warn("发布闸门未通过，按规矩不写任何文件，保留上一版页面：")
        for problem in problems:
            warn("  - %s" % problem)
        restore_audio(paths, audio_backup)
        _write_run_report(paths, run_id, {
            "run_id": run_id,
            "结果": "发布闸门未通过，未写任何文件",
            "闸门检查": checks,
            "问题": problems,
            "国际源状态": intl_status,
            "国内源状态": dom_status,
        })
        return 0

    # ---------------- L8 输出 ----------------
    log("L8 输出：写数据与页面")
    _publish(paths, day_entry, history, today, run_id, {
        "run_id": run_id,
        "国际源状态": intl_status,
        "国内源状态": dom_status,
        "国际规范化": intl_stats,
        "国内规范化": dom_stats,
        "标题翻译": {"国际": trans_stats_intl, "国内": trans_stats_dom},
        "校验闸门": meta["gate_logs"],
        "发布闸门": checks,
        "全文翻译篇数": deep_count,
        "AI消耗": client.usage_report(),
        "耗时秒": round(time.time() - started, 1),
        "结果": "已发布",
    })

    # 全部成功 → 彻底删掉旧音频备份
    if audio_backup and os.path.exists(audio_backup):
        _remove_quietly(audio_backup)

    log("完成，用时 %.1f 秒" % (time.time() - started))
    if not dry_run:
        log("接下来由 workflow 负责 git 提交与 Cloudflare Pages 部署")
    return 0


def backfill_images(day_entry, meta, http_get=None):
    """给「已入选但还没有主图」的事件补图。

    背景：国内版块线上 0/8 有图，国际版块 7/8 有图，观感割裂。
    中文源 RSS 完全不带图，而 deep_dive 只覆盖少数几篇。
    这里对剩下的事件逐个打开正文页取 og:image。

    成本控制：
      - 只处理入选的事件（最多每版块 8 条），不会把几百条都抓一遍
      - 并发 4 路，单条超时 15 秒
      - 任何失败静默跳过，绝不影响主流程（配图是锦上添花）
    """
    http_get = http_get or http_get_bytes

    targets = []
    for key in ("domestic", "international"):
        clusters = {c["cid"]: c for c in meta["clusters"][key]}
        for event in day_entry["sections"][key]:
            if event.get("image"):
                continue
            cluster = clusters.get(event["cid"])
            if not cluster or not cluster["items"]:
                continue
            primary = cluster["items"][0]
            url = primary.get("url") or ""
            if not is_valid_url(url):
                continue
            targets.append((event, cluster, primary, url))

    if not targets:
        return 0

    def work(target):
        event, cluster, primary, url = target
        try:
            _body, image = fetch_article(url, http_get=http_get, timeout=15)
        except Exception:  # noqa: BLE001 补图失败不影响任何东西
            return target, ""
        return target, image

    filled = 0
    # 并发 4 路：比串行快很多，又不会把外部站点打爆
    workers = min(4, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (event, cluster, primary, _url), image in pool.map(work, targets):
            if not image:
                continue
            event["image"] = image
            cluster["image"] = image
            if not primary.get("image"):
                primary["image"] = image
            filled += 1
    return filled


def _refresh_event_summaries(day_entry, meta):
    """深挖得到的正文译文要回填进事件的 cluster，页面才看得到。"""
    cluster_index = {}
    for key in ("domestic", "international"):
        for cluster in meta["clusters"][key]:
            for item in cluster["items"]:
                cluster_index[id(item)] = item

    for key in ("domestic", "international"):
        clusters = {c["cid"]: c for c in meta["clusters"][key]}
        for event in day_entry["sections"][key]:
            cluster = clusters.get(event["cid"])
            if not cluster:
                continue
            for media in event["cluster"]:
                url = media.get("url")
                for item in cluster["items"]:
                    if item.get("url") != url:
                        continue
                    if item.get("summary_zh"):
                        media["summary_zh"] = item["summary_zh"]
                    if item.get("summary_en"):
                        media["summary_en"] = item["summary_en"]
                    break


def _publish(paths, day_entry, history, today, run_id, report_extra):
    """真正写文件：整合历史 → 渲染首页与归档 → 清理 → 写运行报告。"""
    # 1. 今天的条目替换/插入历史
    days = [d for d in history if d.get("date") != day_entry["date"]]
    days.append(day_entry)
    days.sort(key=lambda d: str(d.get("date")), reverse=True)

    # 2. 清理 30 天前
    before = len(days)
    days = prune_days(days, today, KEEP_DAYS)
    pruned = before - len(days)
    if pruned > 0:
        log("清理 %d 天过期数据（保留最近 %d 天）" % (pruned, KEEP_DAYS))

    # 3. 渲染归档（所有保留的历史日）
    keep_dates = [d.get("date") for d in days]
    archive_count = 0
    for day in days:
        html = render_page(day, is_today=False, recent_days=None, site_url=SITE_URL)
        write_text(os.path.join(paths.archive, "%s.html" % day.get("date")), html)
        archive_count += 1

    # 4. 删掉过期归档文件
    removed = prune_archive_files(paths.archive, keep_dates)
    if removed:
        log("删除过期归档文件：%s" % ", ".join(removed))

    # 5. 渲染首页
    recent = [d for d in days if d.get("date") != day_entry["date"]][:RECENT_DAYS]
    index_html = render_page(day_entry, is_today=True, recent_days=recent, site_url=SITE_URL)
    write_text(paths.index, index_html)

    # 6. 写 entries.json
    write_json(paths.entries, {"days": days})

    # 7. 音频只留最新一天
    audio_removed = prune_audio(paths.audio)
    if audio_removed:
        log("清理多余音频：%s" % ", ".join(audio_removed))

    # 8. 运行报告
    report_extra["归档天数"] = archive_count
    report_extra["条目总天数"] = len(days)
    report_extra["清理过期天数"] = pruned
    _write_run_report(paths, run_id, report_extra)

    log("已写出：index.html、archive/*.html（%d 个）、data/entries.json、data/runs/%s.json" % (
        archive_count, run_id))
    log("首页大小：%.1f KB" % (os.path.getsize(paths.index) / 1024.0))
    return days


def _write_run_report(paths, run_id, payload):
    """写运行报告 data/runs/YYYY-MM-DD.json（源状态、AI 消耗、闸门结果）。"""
    date_key = today_bj().strftime("%Y-%m-%d")
    path = os.path.join(paths.runs, "%s.json" % date_key)
    report = {"日期": date_key, "run_id": run_id}
    report.update(payload)
    try:
        write_json(path, report)
        log("运行报告：%s" % path)
    except OSError as exc:
        warn("运行报告写入失败：%s" % exc)


# ============================================================================
# 十五、dry-run 用的假数据与假客户端（零网络、零 Cloudflare 调用）
# ============================================================================

def _rfc822(dt):
    """把 datetime 转成 RSS 用的 RFC 822 时间串。"""
    return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _build_fake_rss(entries):
    """把一组假条目拼成合法的 RSS 2.0 XML。"""
    items = []
    for entry in entries:
        image = entry.get("image")
        thumb = ""
        if image:
            thumb = (
                '<media:thumbnail xmlns:media="http://search.yahoo.com/mrss/" '
                'url="%s"/>' % image
            )
        items.append(
            "<item>"
            "<title>%s</title>"
            "<link>%s</link>"
            "<description>%s</description>"
            "<pubDate>%s</pubDate>"
            "%s"
            "</item>" % (
                _xml_escape(entry["title"]),
                _xml_escape(entry["link"]),
                _xml_escape(entry.get("description", "")),
                _rfc822(entry["published"]),
                thumb,
            )
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>演练源</title><link>https://example.org/</link>"
        "<description>dry-run 假数据</description>"
        + "".join(items)
        + "</channel></rss>"
    ).encode("utf-8")


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _dry_run_fixture(today):
    """构造演练用的假 RSS 数据（覆盖多源簇、GN、软新闻、无图、双版块）。"""
    now = now_bj()
    recent = now - timedelta(hours=2)
    older = now - timedelta(hours=8)

    # 国际：两两成簇，体现「同一事件多家媒体」
    intl = {
        "BBC": [
            # 与 Guardian 成簇（同一事件，措辞不同）
            ("Supertanker sinks after striking mine in Strait of Hormuz",
             "https://www.bbc.com/news/world-hormuz-tanker",
             "A supertanker carrying two million barrels of oil sank in the Strait "
             "of Hormuz after hitting a mine, shipping officials said.",
             "https://ichef.bbci.co.uk/ace/standard/240/cpsprodpb/tanker.jpg", recent),
            # 软新闻（应被过滤）
            ("Football star wins player of the year award",
             "https://www.bbc.com/sport/award",
             "The striker won the award for the third time.",
             "https://ichef.bbci.co.uk/ace/standard/240/cpsprodpb/sport.jpg", recent),
        ],
        "The Guardian": [
            ("Oil tanker sinks in Strait of Hormuz after mine strike",
             "https://www.theguardian.com/world/hormuz-tanker",
             "An oil tanker has sunk in the Strait of Hormuz after a mine strike, "
             "raising fears for global oil prices.",
             "https://i.guim.co.uk/img/media/tanker.jpg?width=500&quality=85", recent),
            # 词边界反例：标题里的 Depardieu 不应被 "eu" 误过滤
            ("Gerard Depardieu trial opens in Paris court",
             "https://www.theguardian.com/world/depardieu-trial",
             "The actor's trial opened in Paris on Monday.",
             "", older),
        ],
        "Al Jazeera": [
            ("South Africa shootings kill 17 in a single day",
             "https://www.aljazeera.com/news/south-africa-shootings",
             "At least 17 people were killed in two mass shootings in South Africa.",
             "https://www.aljazeera.com/wp-content/uploads/2026/11/sa.jpg?resize=770%2C513",
             recent),
        ],
        "Sky News": [
            ("Seventeen killed in South Africa mass shootings",
             "https://news.sky.com/story/south-africa-shootings",
             "Two mass shootings in South Africa left 17 people dead in one day.",
             "https://e3.365dm.com/26/11/768x432/skynews-sa.jpg", recent),
        ],
        "Google News": [
            # GN 单独成簇 → 只计 1 家
            ("UN warns of famine risk in Sudan",
             "https://news.google.com/rss/articles/sudan-famine",
             "The United Nations warned that millions face famine in Sudan.",
             "", older),
        ],
        "France 24": [
            # 无图条目
            ("Japan issues tsunami advisory after strong earthquake",
             "https://www.france24.com/en/japan-quake",
             "Japan issued a tsunami advisory after a strong earthquake struck "
             "off its coast.",
             "", older),
        ],
        "NPR": [],
    }

    # 国内：三条以上，全部中文，零翻译。
    # 键名必须与 DOMESTIC_FEEDS 里的 name 完全一致，否则演练时会报「该源没有假数据」。
    dom = {
        "中新网·即时": [
            ("中国三季度经济增速超出市场预期",
             "https://www.chinanews.com.cn/cj/2026/11-17/economy.shtml",
             "中国国家统计局公布数据显示，三季度经济增速超出市场预期。",
             "", recent),
            # 摘要首句重复（中新网真实特征），用来验证 collapse_repeated_prefix
            ("国务院常务会议部署扩大内需措施",
             "https://www.chinanews.com.cn/cj/2026/11-17/demand.shtml",
             "中新社北京11月17日电 国务院常务会议部署扩大内需措施。"
             "中新社北京11月17日电 国务院常务会议部署扩大内需措施，提出一系列政策。",
             "", recent),
        ],
        "中新网·要闻": [],
        "中新网·时政": [
            # 同主题案例：这几条的公共前缀都是「习近平会见」，
            # 用来验证 group_by_theme 会把它们归成一张主题卡片。
            ("习近平会见越共中央总书记、国家主席苏林",
             "https://www.chinanews.com.cn/gn/2026/11-17/meet-sulin.shtml",
             "习近平在深圳会见越共中央总书记、国家主席苏林。",
             "", recent),
            ("习近平会见加拿大总理卡尼",
             "https://www.chinanews.com.cn/gn/2026/11-17/meet-carney.shtml",
             "习近平在深圳会见加拿大总理卡尼。",
             "", recent),
            ("习近平会见墨西哥总统辛鲍姆",
             "https://www.chinanews.com.cn/gn/2026/11-17/meet-sheinbaum.shtml",
             "习近平在深圳会见墨西哥总统辛鲍姆。",
             "", older),
            ("全国多地迎来寒潮降温天气",
             "https://www.chinanews.com.cn/sh/2026/11-17/cold.shtml",
             "中央气象台发布寒潮预警，全国多地气温明显下降。",
             "", older),
        ],
        "中新网·社会": [],
        "中新网·国际": [],
        "联合早报": [
            ("中国与东盟签署新的经贸合作文件",
             "https://www.zaobao.com.sg/news/asean-trade",
             "双方在新加坡签署了新的经贸合作文件。",
             "https://cassette.sphdigital.com.sg/image/zaobao/asean-trade-photo.jpg", recent),
        ],
        # 海外中文媒体（演练也要覆盖，否则会报「该源没有假数据」）
        "纽约时报中文网": [
            ("中国与东盟签署新的经贸合作文件",
             "https://cn.nytimes.com/china/20261117/asean-trade/",
             "分析认为这标志着区域经贸合作进入新阶段。",
             "https://static01.nyt.com/images/2026/11/17/multimedia/asean-photo.jpg", recent),
        ],
        "法广RFI": [
            ("全国多地迎来寒潮降温天气",
             "https://www.rfi.fr/cn/中国/20261117-cold-wave",
             "气象部门提醒民众注意防寒保暖。",
             "https://s.rfi.fr/media/display/cold-wave-photo.jpg", older),
        ],
        # 热榜源：一条明显的热点，验证热度加权能把它排到前面
        "澎湃精选": [
            ("中国三季度经济增速超出市场预期，A股大涨",
             "https://www.thepaper.cn/newsDetail_forward_hot001",
             "国家统计局公布数据，市场反应积极。",
             "https://img.thepaper.cn/hot-economy.jpg", older),
        ],
    }

    feeds = {}
    for name, entries in list(intl.items()) + list(dom.items()):
        feeds[name] = _build_fake_rss([
            {
                "title": title,
                "link": link,
                "description": desc,
                "image": image,
                "published": published,
            }
            for title, link, desc, image, published in entries
        ])
    return feeds


def make_fake_http(today):
    """返回一个假的 http_get，把 URL 映射到演练用的假 RSS 字节。

    走的是和正式流程完全相同的 feedparser 解析路径，只是不发网络请求。
    """
    feeds = _dry_run_fixture(today)

    def fake_http_get(url, timeout=SOURCE_TIMEOUT):
        for source in INTL_FEEDS + DOMESTIC_FEEDS + OVERSEAS_ZH_FEEDS + HOT_FEEDS:
            if source["url"] == url:
                name = source["name"]
                payload = feeds.get(name)
                if payload is None:
                    raise urllib.error.URLError("演练：该源在假数据里没有配置")
                return payload
        # 正文页：给一段像样的英文正文，让全文翻译分支也能跑到
        return _fake_article_bytes(url)

    return fake_http_get


def _fake_article_bytes(url):
    body = (
        "Shipping officials confirmed on Tuesday that a supertanker carrying "
        "about two million barrels of crude oil sank in the Strait of Hormuz "
        "after striking a mine. The vessel went down in the early hours, and "
        "rescue teams were dispatched to the area. Oil prices rose sharply on "
        "the news as traders weighed the risk to one of the world's busiest "
        "shipping lanes. Analysts said the incident could push freight rates "
        "higher for weeks. The operator of the tanker declined to comment. "
        "Several countries have called for restraint and for an investigation "
        "into how the mine was placed in the waterway. "
    ) * 4
    # 刻意带上 og:image，让 dry-run 也能走到「从正文页补图」这条分支
    html = (
        "<html><head>"
        '<meta property="og:image" content="http://ichef.bbci.co.uk/ace/standard/240/og.jpg">'
        "</head><body><article><h1>Dry-run article</h1>"
        + "".join("<p>%s</p>" % body[i:i + 400] for i in range(0, len(body), 400))
        + "</article></body></html>"
    )
    return html.encode("utf-8")


class FakeAIClient:
    """演练用的假 AI 客户端：零网络、零 Cloudflare 调用。

    根据提示词内容判断当前是哪一个环节，返回结构合理的假结果，
    从而让 L3–L6 的每条分支都被真正执行一遍。
    """

    def __init__(self, today):
        self.today = today
        self.calls = 0
        self.prompt_chars = 0
        self.response_chars = 0

    # 与真实客户端同名的接口
    def complete(self, user_prompt, system_prompt=None, max_tokens=4096):
        self.calls += 1
        self.prompt_chars += len(user_prompt or "")
        result = self._dispatch(user_prompt or "")
        self.response_chars += len(result)
        return result

    def call(self, messages, max_tokens=4096):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        return self.complete(text)

    def usage_report(self):
        return {
            "模型": "演练假客户端（未消耗任何额度）",
            "调用次数": self.calls,
            "估算_neurons": 0,
            "占免费日额度百分比": 0.0,
        }

    # 内部：按提示词特征分派
    def _dispatch(self, prompt):
        if "逐条翻译成简体中文" in prompt:
            return self._translate_titles(prompt)
        if "同一主题的中文新闻标题" in prompt:
            return self._summarize_theme(prompt)
        if "今日" in prompt and "大事" in prompt and "总结" in prompt:
            return self._summarize(prompt)
        if "把下面这篇英文新闻翻译成简体中文" in prompt:
            return _fake_fulltext_translation(prompt)
        if "电台早间新闻主播" in prompt:
            return self._broadcast()
        if "同一事件的新闻报道" in prompt:
            return self._redo(prompt)
        return "演练：未知提示词"

    def _summarize_theme(self, prompt):
        """为主题组生成概括：用标题里的公共前缀拼一句合理的话。"""
        lines = [
            line[2:].strip()
            for line in prompt.splitlines()
            if line.startswith("- ")
        ]
        if not lines:
            return "演练：相关动态汇总"
        title = lines[0]
        if "会见" in title:
            return "习近平在APEC期间会见%d国领导人" % len(lines)
        return "%s等%d条动态" % (title[:12], len(lines))

    def _translate_titles(self, prompt):
        """把标题逐条翻成中文：用关键词映射，保证同一事件译出相同中文。"""
        lines = []
        for raw in prompt.splitlines():
            match = re.match(r"^(\d+)\.\s*(.+)$", raw.strip())
            if not match:
                continue
            index, title = match.group(1), match.group(2)
            lines.append("[%s] %s" % (index, _fake_translate(title)))
        return "\n".join(lines)

    def _summarize(self, prompt):
        """按 [cid] 输出浓缩概括，模拟真实模型的输出格式。

        注意：只能解析「现在，请处理以下 N 条新闻标题：」之后的部分。
        提示词模板里带有输出示例（[c13] 南非… / [c7] 油轮…），
        如果不截断，假 AI 会把示例也当成输入，导致 c7 被示例内容污染
        （实测症状：c7 的概括变成了示例里的「超级油轮触雷沉没」）。
        """
        marker = "现在，请处理以下"
        body = prompt.split(marker, 1)[1] if marker in prompt else prompt

        lines = []
        for raw in body.splitlines():
            match = re.match(r"^\[(c\d+|i\d+)\]\s*(【\d家媒体报道】)?\s*(.+)$",
                             raw.strip())
            if not match:
                continue
            cid = match.group(1)
            title = match.group(3)
            summary = _fake_short(title)
            importance = "影响值得关注"
            lines.append("[%s] %s——%s" % (cid, summary, importance))
        return "\n".join(lines)

    def _redo(self, prompt):
        cid_match = re.search(r"^编号：\s*([ci]\d+)$", prompt, re.MULTILINE)
        cid = cid_match.group(1) if cid_match else "c1"
        return "[%s] 事件概况以官方通报为准——暂无更多细节" % cid

    def _broadcast(self):
        return (
            "各位好，今天是%d年%d月%d日，欢迎收听今日国际大事。"
            "一艘超级油轮在霍尔木兹海峡触雷后沉没，国际油价随之上涨。"
            "南非同日发生两起大规模枪击事件，造成至少十七人死亡。"
            "日本近海发生强震，气象部门发布海啸提示。"
            "国内方面，三季度经济增速超出市场预期，国务院部署扩大内需措施。"
            "以上就是今天的要闻，明天同一时间再见。"
            % (self.today.year, self.today.month, self.today.day)
        )


# 演练用的标题→中文译文映射（同一事件必须译成同一句，聚类才能生效）
_FAKE_TRANSLATION_RULES = [
    (("supertanker", "tanker"), "超级油轮在霍尔木兹海峡触雷后沉没"),
    (("south africa",), "南非同日发生两起大规模枪击致至少17人死亡"),
    (("famine", "sudan"), "联合国警告苏丹面临饥荒风险"),
    (("earthquake", "tsunami"), "日本近海发生强震并发布海啸提示"),
    (("depardieu",), "德帕迪约案在巴黎开庭审理"),
    (("china", "economy"), "中国三季度经济增速超出市场预期"),
    (("domestic demand",), "国务院常务会议部署扩大内需措施"),
    (("cold wave",), "全国多地迎来寒潮降温天气"),
    (("asean", "trade"), "中国与东盟签署新的经贸合作文件"),
]


def _fake_translate(title):
    low = title.lower()
    for keywords, zh in _FAKE_TRANSLATION_RULES:
        if any(k in low for k in keywords):
            return zh
    return "演练译文：" + title[:24]


def _fake_short(title):
    """把标题压成一句概括（≤40 字）。"""
    for keywords, zh in _FAKE_TRANSLATION_RULES:
        if any(k in title for k in keywords):
            return zh
    return title[:30]


def _fake_fulltext_translation(prompt):
    return (
        "演练：这是一段中文译文正文。航运官员周二确认，一艘载有约两百万桶"
        "原油的超级油轮在霍尔木兹海峡触雷后沉没。油轮在凌晨下沉，救援队已"
        "赶往现场，油价随即大幅上涨。分析人士认为，这一事件可能在未来数周"
        "推高运费。"
    )


def write_fake_mp3(path):
    """写一个只有合法 MP3 文件头的小文件，用来演练「有音频」渲染分支。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"ID3\x03\x00\x00\x00\x00\x00\x00")
        handle.write(b"\x00" * 512)


def dry_run_verify(paths, today):
    """演练自检：确认过期数据与过期归档都被清掉了。"""
    problems = []

    if not os.path.exists(paths.entries):
        problems.append("没有生成 data/entries.json")
        return problems

    with open(paths.entries, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    days = payload.get("days") or []
    dates = [d.get("date") for d in days]

    stale = (today - timedelta(days=31)).strftime("%Y-%m-%d")
    if stale in dates:
        problems.append("31 天前的数据 %s 没有被清理" % stale)
    stale_file = os.path.join(paths.archive, "%s.html" % stale)
    if os.path.exists(stale_file):
        problems.append("31 天前的归档文件 %s 没有被删除" % stale_file)

    if today.strftime("%Y-%m-%d") not in dates:
        problems.append("今天的日期不在 entries.json 里")
    if len(dates) > KEEP_DAYS:
        problems.append("保留天数 %d 超过上限 %d" % (len(dates), KEEP_DAYS))

    for date in dates:
        if not os.path.exists(os.path.join(paths.archive, "%s.html" % date)):
            problems.append("归档文件缺失：%s.html" % date)

    if not os.path.exists(paths.index):
        problems.append("没有生成 index.html")

    return problems


def seed_dry_run_history(paths, today):
    """给演练造历史：昨天 + 31 天前，用来验证 30 天清理真的生效。"""
    def fake_day(date, title):
        return {
            "date": date.strftime("%Y-%m-%d"),
            "broadcast": "各位好，这是 %s 的历史播报稿，用于演练。" % date.strftime("%Y-%m-%d"),
            "tts_ok": False,
            "sections": {
                "domestic": [{
                    "cid": "c1",
                    "zh": title,
                    "importance": "演练用历史数据",
                    "source_count": 1,
                    "image": "",
                    "cluster": [{
                        "source": "演练源",
                        "title_zh": title,
                        "title_en": "",
                        "url": "https://www.bbc.com/news/dry-run-history",
                        "summary_zh": "演练用历史摘要。",
                        "image": "",
                        "is_primary": True,
                    }],
                }],
                "international": [],
            },
            "section_notes": {"domestic": "", "international": ""},
            "raw_news": [],
        }

    days = [
        fake_day(today - timedelta(days=1), "昨天的演练新闻"),
        fake_day(today - timedelta(days=31), "三十一天前的演练新闻"),
    ]

    paths.ensure()
    write_json(paths.entries, {"days": days})
    for day in days:
        write_text(
            os.path.join(paths.archive, "%s.html" % day["date"]),
            "<html><body>演练历史归档</body></html>",
        )
    log("演练：已注入历史数据（昨天、31 天前）与对应归档文件")


# ============================================================================
# 十六、命令行入口
# ============================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="每日新闻播报站生成脚本（全中文输出）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="演练模式：注入假 RSS 与假 AI 客户端，零网络、零 Cloudflare 调用，"
             "只写 _dryrun/ 目录",
    )
    parser.add_argument(
        "--dry-run-tts-ok", action="store_true",
        help="配合 --dry-run 使用：模拟语音合成成功，验证「有音频」的渲染分支",
    )
    args = parser.parse_args(argv)

    dry_run = bool(args.dry_run)
    if args.dry_run_tts_ok and not dry_run:
        parser.error("--dry-run-tts-ok 必须与 --dry-run 一起使用")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        if dry_run:
            paths = Paths(root, dry_run=True)
            # 每次演练都从干净目录开始，避免上一次的产物影响判断
            if os.path.isdir(paths.out):
                shutil.rmtree(paths.out, ignore_errors=True)
            paths.ensure()
            seed_dry_run_history(paths, today_bj())

        code = run(dry_run=dry_run, dry_run_tts_ok=args.dry_run_tts_ok, root=root)

        if dry_run:
            paths = Paths(root, dry_run=True)
            problems = dry_run_verify(paths, today_bj())
            print()
            log("=" * 66)
            if problems:
                warn("演练自检发现 %d 个问题：" % len(problems))
                for problem in problems:
                    warn("  - %s" % problem)
                return 1
            log("演练自检全部通过（30 天清理、归档、首页、数据契约均正常）")
            log("演练产物目录：%s" % paths.out)
            log("=" * 66)
        return code
    except KeyboardInterrupt:
        warn("被手动中断")
        return 130
    except Exception:  # noqa: BLE001 主流程兜底，打印完整堆栈便于排查
        print("=" * 66, file=sys.stderr)
        print("运行出现未预料的异常，完整堆栈如下：", file=sys.stderr)
        traceback.print_exc()
        print("=" * 66, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
