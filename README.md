# 每日国际大事 · 中文播报站

> 每天更新一个**在中国大陆无需翻墙就能打开**的中文渠道，看清国内外热点，也看到国际上不同的观点。

站点是纯静态页面，托管在 **Cloudflare Pages + 自有自定义域名**（不用 GitHub Pages——`*.github.io` 在国内访问很不稳定）。
每天北京时间 8:00 由 GitHub Actions 自动跑完整条流水线，也可以随时手动触发。

---

## 它每天做什么

```
UTC 0:00（北京 8:00）自动触发
  │
  ├─ L0  读历史数据、清掉今日临时产物
  ├─ L1  并发抓取 7 个国际英文源 + 若干中文国内源（单源超时 20 秒）
  ├─ L2  近 36 小时过滤 / URL 去重 / 软新闻剔除 / 图片分辨率归一
  ├─ L3  英文标题分批翻译成中文（每批 8 条）→ bigram Jaccard 聚类 → AI 生成今日大事
  ├─ L4  校验闸门：国家实体对账（防编造）、链接归属（防错位），不合格打回重做 ≤2 次
  ├─ L5  选 6 篇抓正文，按句子边界分段（约 900 字符）全文翻译
  ├─ L6  AI 改写 600 字播报稿 → edge-tts 合成 mp3
  ├─ L7  发布闸门：条数 / 链接 / 中文占比 / 音频文件头，任一不通过就不写任何文件
  └─ L8  写数据、渲染首页与归档、清理 30 天前、提交、部署 Cloudflare Pages
```

**抓不到新闻就不写文件。** 若有效新闻为 0 条，或发布闸门不通过，脚本直接正常退出，
站点保留上一版页面，绝不会被覆盖成空白页。

---

## 页面怎么设计的（易用性优先）

用户的核心诉求是「30 秒内知道今天发生了什么」，「看不同观点」是深入需求。所以：

- **默认视图极其简洁**：每条大事只有「一句话概括 + 一句重要性 + N 家媒体报道」。
- **不同观点默认折叠**：点「查看各家报道 ▸」才展开同一事件的多家媒体报道，
  并提示「以下报道来自 N 家媒体，观点可能不同，请自行判断」。
- **两个版块**：「国内热点」「国际热点」各自成一条时间线，按来源家数优先、时间倒序排列。
- **顶部可听**：有 mp3 给播放器；mp3 合成失败则降级成「朗读播报」按钮（浏览器 SpeechSynthesis）。
- **手机优先**：卡片大、字大、按钮大（最小点击区 44–48px），触摸友好。

---

## 文件结构

```
.github/workflows/daily.yml   定时 + 手动 + 仓库名守卫 + smoke + 生成 + 提交 + 部署（单文件闭环）
scripts/generate.py           核心脚本：L0–L8，支持 --dry-run
scripts/ai_client.py          Workers AI 调用封装：两种返回结构兼容、指数退避、代码块清洗
scripts/live_check.py         真实抓取联调检查（真实 RSS + 假 AI，零额度，不写站点文件）
smoke_test.py                 运行时冒烟测试（workflow 里先跑，失败即中止）
index.html                    首页（今日完整详情 + 最近 7 天摘要）
archive/YYYY-MM-DD.html       每日归档，保留 30 天
data/entries.json             结构化历史数据（唯一真相源），保留 30 天
data/runs/YYYY-MM-DD.json     运行报告（源状态、AI 消耗估算、闸门结果）
audio/daily.mp3               播报音频，每天覆盖，只留最新一天
requirements.txt              依赖（范围约束，不用精确版本号）
```

---

## 技术选型

| 环节 | 方案 |
| --- | --- |
| 文本生成与翻译 | Cloudflare Workers AI `@cf/google/gemma-4-26b-a4b-it`（免费层 10000 Neurons/天，本方案日耗约 12%） |
| 语音合成 | **edge-tts**，音色 `zh-CN-XiaoxiaoNeural`（纯免费、无需密钥） |
| RSS 解析 | feedparser |
| 正文提取 | beautifulsoup4 |
| 运行环境 | GitHub Actions，Python 3.11 |
| 托管 | **Cloudflare Pages + 自有域名** |

**国际新闻源（7 个，均为实测可用）**：BBC World、The Guardian、Al Jazeera、France 24、
NPR World、Sky News、Google News (WORLD)。
其中半岛电视台刻意保留——它提供与非西方媒体不同的视角，正是「看不同观点」的关键来源。

**国内中文源（实测结论）**：用 **中新网官方 RSS** 的 5 个频道（即时/要闻/时政/社会/国际），
每源 30 条、境外访问畅通、时间字段完整。联合早报走第三方公益镜像作配图补充，失败会被静默跳过。

⚠️ 以下源**实测不可用，已从配置中剔除**，不要再用：
- **联合早报官方 RSS** 已下线（返回 SPA 网页 / 404）
- **人民网、新华网、新浪 RSS 全部停更**（最新条目停在 2016–2025 年）。
  它们形式上是合法 RSS、`bozo=False`、条目上百条，是最危险的「僵尸源」——
  脚本内置了**停更源守卫**，最新条目超过 7 天的源会整源丢弃并记入运行报告
- **央视 RSS** 全部 404；**rsshub.app** 公共实例已被 Cloudflare 拦截（403）
- **BBC 中文** `simp` 会被 302 到 `trad`（拿到繁体）且频繁超过 20s 超时，故不采用

**成本：全程零成本**，不需要绑定信用卡，也不需要实名认证。

### 为什么用 edge-tts 而不是 Workers AI 的 TTS

Workers AI 的 `@cf/myshell-ai/melotts` 已于 **2026-11-20 弃用**，同厂的 `aura-1` 又只支持英语，
无法满足中文播报。改用 **edge-tts**（微软开源项目）：完全免费、不需要 API key，
中文音色 `zh-CN-XiaoxiaoNeural` 自然度足够，几秒就能合成 600 字播报稿，
也不占用 Workers AI 的免费额度。

---

## 你需要手动完成的设置

### 1. 添加三个 Repository secrets

打开仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加：

| 名称 | 内容 |
| --- | --- |
| `CF_ACCOUNT_ID` | 你的 Cloudflare 账号 ID |
| `CF_API_TOKEN` | Cloudflare API Token，需同时具备 **Workers AI:Edit** 和 **Pages:Edit** 权限 |
| `CF_PAGES_PROJECT` | Cloudflare Pages 项目名（例如 `daily-news`） |

这三个值**只从 GitHub Secrets 读取，不写进代码、不进仓库、不进日志**。

（可选）在 **Settings → Secrets and variables → Actions → Variables** 里加一个
`SITE_URL`，例如 `https://news.你的域名.com`，用于页面里的 canonical 链接。

### 2. 首次创建 Cloudflare Pages 项目并绑定自定义域名

在 Cloudflare 后台 **Workers & Pages → Create → Pages** 创建一个项目，
项目名与 `CF_PAGES_PROJECT` 保持一致；然后在项目设置里 **Custom domains** 绑定你的域名，
按提示添加 CNAME 记录（域名已托管在你的 Cloudflare 账号时通常一键完成）。

首次执行 workflow 时 wrangler 也会尝试自动创建项目，但绑定域名仍需在后台点一次。

### 3. 手动跑一次验证

到仓库 **Actions → 每日新闻自动更新 → Run workflow** 跑一次，
确认 Secrets 生效、页面能正常生成、自定义域名能打开。

### 4. 之后就是自动的

以后每天北京时间 8:00 自动更新。GitHub 定时任务在高峰时段可能延迟 5–45 分钟，属正常现象。

### 5. 中文国内源若不可用，改配置即可

国内源写在 `scripts/generate.py` 的 `DOMESTIC_FEEDS` 列表里，改这个列表就能增删，
不用动任何逻辑。若全部中文源都抓不到，国内版块会自动退化为「从国际源里筛选涉华报道」，
并在页面上明确注明，不会留空。

---

## 本地 dry-run（不花钱、不联网、不消耗额度）

`--dry-run` **零网络、零 Cloudflare 调用、零额度消耗**。
输出全部写到独立的 `_dryrun/` 目录，**不会覆盖真实的** `index.html`、`data/`、`audio/`。

```bash
python -m pip install -r requirements.txt
python smoke_test.py                      # 运行时冒烟测试，先跑这个
python scripts/generate.py --dry-run      # 端到端演练（默认走无音频降级分支）
python scripts/generate.py --dry-run --dry-run-tts-ok   # 额外验证「有音频」渲染分支
python scripts/live_check.py              # 真实抓 RSS 联调（假 AI，零额度，不写站点文件）
```

dry-run 覆盖的场景：多源簇、Google News 条目、软新闻、无图条目、
TTS 失败降级、国内/国际双版块，以及「31 天前的数据与归档被自动清理」。

---

## 架构要点（为什么这么做）

- **生成与部署在同一个 workflow 文件里。** 因为 `GITHUB_TOKEN` 推送的提交不会触发另一个工作流，
  拆成两个文件会导致「提交了但页面不更新」。
- **仓库名守卫。** workflow 第一步校验 `GITHUB_REPOSITORY` 必须等于 `tyuecode/daily-news`，
  防止误操作其他仓库（尤其是用户的主页仓库）。
- **运行时冒烟测试而非只做语法检查。** `py_compile` 只查语法不查 `NameError`，
  历史上就出现过语法通过、线上却因函数未定义导致整站空白的事故。
- **用稳定 `cid` 而不是数组下标。** 摘要与链接靠 `cid` 绑定，
  某条事件被打回重做时不会影响其它条目，也不会张冠李戴。
- **翻译防编造。** 摘要生成后抽取国家实体，与所链接原文标题比对；
  有 2 个以上国家在原文里找不到，就判定为编造，打回重做或丢弃该条。
- **每轮开始先把旧 `daily.mp3` 挪走。** 避免某天 TTS 失败时，页面播着昨天的音频配今天的稿。
  此时页面自动降级为浏览器 `SpeechSynthesis` 朗读。
- **音频文件必须按文件头校验。** 用文件大小猜会把几 MB 的 JSON 当音频写进文件，
  页面放出来是乱码。合法头为 `ID3` 或 `0xFF` 帧同步。
- **所有外部文本写入 HTML 前都做 `html.escape()`。** RSS 里的恶意标题不能破坏整页结构。

---

## 关键参数（都在 `scripts/generate.py` 顶部）

| 参数 | 值 | 说明 |
| --- | --- | --- |
| `WINDOW_HOURS` | 36 | 新闻时效窗口 |
| `KEEP_DAYS` | 30 | 归档保留天数（音频只留最新一天） |
| `TRANSLATE_BATCH` | 8 | 每批翻译条数（一次给 20 条模型只会翻前 5 条） |
| `CLUSTER_THRESHOLD` | 0.35 | 中文 bigram Jaccard 聚类阈值 |
| `SOURCE_TIMEOUT` | 20 | 单源抓取超时（秒） |
| `TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | 播报音色 |

---

## 常见问题

**Q：页面打开了但没有音频？**
先看播报区是不是「朗读播报」按钮——那是 TTS 失败后的正常降级，点一下用浏览器朗读即可。
原因可在 `data/runs/<日期>.json` 里查。

**Q：某天页面没更新？**
看 Actions 的运行日志和 `data/runs/<日期>.json`。若是发布闸门不通过（条数不足、中文占比过低），
说明当天抓取质量确实不行，站点保留上一版是刻意设计，不是故障。

**Q：想加新闻源？**
英文源加到 `INTL_FEEDS`，中文源加到 `DOMESTIC_FEEDS`，都是配置项，改完不用动逻辑。
加源后建议本地跑一次 `python scripts/generate.py --dry-run` 确认没有意外。

**Q：Workers AI 额度会不会不够？**
完整一轮约消耗 1,170 neurons，占免费层每日 10,000 的约 12%，且运行报告里会记录实际估算值。
