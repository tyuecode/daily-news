# 每日国际与中国热点新闻早报

每天早上用约八分钟，掌握国际与中国最重要的新闻，并理解全球变化对中国经济、科技、金融与普通居民生活的实际影响。

- 🌐 **在线阅读：[daily-news.cc.cd](https://daily-news.cc.cd/)**
- 📚 **全部归档：[daily-news.cc.cd/archive.html](https://daily-news.cc.cd/archive.html)**
- 🎧 每期配中文语音播报（edge-tts 合成）

日期统一采用北京时间（Asia/Shanghai）。

---

## 最新一期

- [2026-11-27 最新简报](latest.md)
- [2026-11-27 完整情报 JSON](news/2026/11/2026-11-27.json)
- [2026-11-27 新闻文字简报](news/2026/11/2026-11-27.md)
- [2026-11-27 中文语音播报稿](audio-script/2026/11/2026-11-27.txt)

## 历史归档

### 2026年

#### 11月
| 日期 | JSON情报 | 新闻简报 | 语音稿 |
|---|---|---|---|
| 2026-11-27 | [JSON](news/2026/11/2026-11-27.json) | [Markdown](news/2026/11/2026-11-27.md) | [TXT](audio-script/2026/11/2026-11-27.txt) |
| 2026-11-26 | [JSON](news/2026/11/2026-11-26.json) | [Markdown](news/2026/11/2026-11-26.md) | [TXT](audio-script/2026/11/2026-11-26.txt) |
| 2026-11-25 | [JSON](news/2026/11/2026-11-25.json) | [Markdown](news/2026/11/2026-11-25.md) | [TXT](audio-script/2026/11/2026-11-25.txt) |

---

## 目录结构

```
daily-news/
├── news/YYYY/MM/YYYY-MM-DD.json      # 完整新闻情报（结构化，页面数据源）
├── news/YYYY/MM/YYYY-MM-DD.md        # 文字简报（GitHub 阅读版）
├── audio-script/YYYY/MM/YYYY-MM-DD.txt  # 中文语音播报稿（TTS 原文）
├── site/                             # 静态资源（样式、robots、响应头、图标）
├── scripts/
│   ├── build_site.py                 # JSON → 静态站点（仅标准库）
│   └── make_audio.py                 # 播报稿 → MP3（edge-tts，增量）
├── .github/workflows/publish.yml     # 构建 + 合成语音 + 发布 Pages
├── latest.md                         # 最新一期（GitHub 版）
└── README.md
```

**语音 MP3 不入库。** 每次流水线运行时生成，只发布到站点，仓库保持纯文本。历史音频由 Actions 缓存承接。

---

## 自动化流程

推送到 `main`（改动了 `news/**`、`audio-script/**`、`site/**`、`scripts/**` 或工作流本身）即触发流水线：

```
检出 → 安装 edge-tts → 校验 JSON
     → 恢复语音缓存 → 增量合成 MP3
     → 构建 dist/ → 发布到 Cloudflare Pages
```

对应步骤说明：

| 步骤 | 作用 |
|---|---|
| 校验新闻数据 | 结构、日期、置顶引用、来源链接、空分类；**有错误即中止**，不发布半成品 |
| 恢复语音缓存 | 按「月份 + 稿件哈希」取回历史 MP3 |
| 生成语音播报 | 仅合成新增/变更的期次，未变化则跳过 |
| 构建静态站点 | 生成首页、归档、每期详情页、`sitemap.xml`、`archive.json` |
| 验收构建产物 | 直接检查输出 HTML：新闻是否全部渲染、置顶是否出现、对华影响区块数量、站内链接是否有效 |
| 发布到 Cloudflare Pages | `wrangler pages deploy`，分支 `main` |
| 验证线上可访问 | 发布后回访站点，连续探测 HTTP 200 才算成功 |

### 两道发布闸门

历史事故的教训是「JSON 合法、构建成功、页面却整块空白」。因此设两道闸门：

1. **数据闸门**（`build_site.py --check-only`）：检查输入 JSON。
   置顶引用了不存在的 ID、缺 summary、缺来源、链接不是 http(s)、日期不一致 → **直接失败**。
2. **产物闸门**（`verify_site.py`）：检查输出 HTML。
   逐条核对新闻标题是否有出现、置顶是否渲染、对华影响区块数量是否与数据一致、
   是否残留 `{esc(...)}` 之类未渲染字面量、站内链接是否指向真实文件。

任何一道不通过都不会发布。

### 字段兼容

新闻 JSON 由模型生成，字段结构会漂移（已实测到三代差异：`degree`/`level`、
嵌套 `channels`/扁平渠道、置顶对象/字符串 ID、`why_watch`/`reason` 等）。
`scripts/normalize.py` 是唯一的适配层，渲染层只消费它的输出，
不再直接读原始字段。

### 音频版本化

页面引用的是带内容哈希的文件名（`/audio/YYYY-MM-DD.<hash>.mp3`）。
稿件一改，哈希变、URL 变，用户不会继续听到旧音频。
若稿件已更新但语音尚未重新合成，页面会显式提示「对应上一版稿件」。

### 触发方式

- **自动**：向 `main` 推送上述路径的改动
- **手动**：Actions → 「构建并发布每日新闻站点」→ Run workflow
  - 勾选 `force_audio` 可强制重新合成全部语音（换了音色/语速时用）

构建与部署是两个 job：构建可被新提交取消，**部署不取消**，避免上线半截产物。

### 配置的 Secrets

| Secret | 说明 |
|---|---|
| `CF_API_TOKEN` | Cloudflare API Token，需 `Cloudflare Pages:Edit` 权限 |
| `CF_ACCOUNT_ID` | Cloudflare 账号 ID |
| `CF_PAGES_PROJECT` | Pages 项目名 |

---

## 本地预览

```bash
# 1. 构建站点（不需要任何第三方依赖）
python scripts/build_site.py

# 2. 合成语音（需要 edge-tts）
pip install edge-tts
python scripts/make_audio.py

# 3. 本地起服务预览
python -m http.server 8000 --directory dist
# 打开 http://localhost:8000
```

常用参数：

```bash
python scripts/build_site.py --base-url https://daily-news.cc.cd
python scripts/make_audio.py --only 2026-11-26   # 只处理某一期
python scripts/make_audio.py --force             # 全部重新合成
python scripts/make_audio.py --voice zh-CN-YunxiNeural --rate +5%
```

可用中文音色：`zh-CN-YunyangNeural`（默认，新闻播报）、`zh-CN-YunxiNeural`、
`zh-CN-YunjianNeural`、`zh-CN-XiaoxiaoNeural`、`zh-CN-XiaoyiNeural`。

---

## 内容标准

- 标准采集窗口：前一日 08:00 至当日 08:00（北京时间）。
- 发生时间、消息公布时间、媒体报道时间分别标注。
- 窗口结束后的重要更新单独标记为「窗口后补充」，不与窗口内新闻混淆。
- 新闻以官方公告和可靠媒体报道为主；未经证实的说法明确标注。
- 市场价格标明报道时点或收盘口径，不作为实时行情。
- 每条新闻附原始来源链接；分析判断与客观事实分开陈述。
- 本项目为每日一次的信息整理，**不代表全天候实时监测**，也不构成投资建议。

## 免责声明

本仓库仅发布公开新闻信息、原创中文摘要与分析，以及指向公开来源的链接。不包含任何个人隐私、API Key、密码或访问令牌。
