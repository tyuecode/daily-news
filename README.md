# daily-news

每日国际大事 · 自动抓取 → 中文翻译 → AI 摘要 → 语音播报 → 静态网站

## 当前状态

**仓库已清空，等待重写。**

此前的实现因代码反复打补丁、结构混乱而被重置（git 历史也一并清除）。
新实现将从零开始，结构更清晰。

## 规划中的结构

```
.github/workflows/daily.yml   定时 + 手动触发 + 生成 + 部署
scripts/generate.py           核心脚本
index.html                    首页
archive/                      每日归档（保留 30 天）
data/entries.json             结构化历史数据
audio/daily.mp3               播报音频（只保留最新一天）
requirements.txt              仅标准库
README.md                     中文说明
```

## 技术选型（已验证）

- **AI**：Cloudflare Workers AI 免费层，`@cf/google/gemma-4-26b-a4b-it`
- **语音**：`@cf/myshell-ai/melotts`（中文，按音频分钟计费）
- **新闻源**：BBC、The Guardian、Al Jazeera、France 24、NPR、Sky News、Google News
- **托管**：GitHub Pages（public 仓库免费）
- **成本**：全程零成本，每日约消耗 12% 的免费 Neurons 额度

## 访问地址

开启 GitHub Pages 后：https://tyuecode.github.io/daily-news/

## 重写说明

详细的技术调研结论（模型对比、新闻源可用性、踩坑清单、验证过的提示词）
已整理在仓库外的《daily-news-技术调研结论.md》中，重写时可直接参考。
