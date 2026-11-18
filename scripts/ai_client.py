"""Cloudflare Workers AI 封装。

职责：
- 统一调用端点（免费层模型 @cf/google/gemma-4-26b-a4b-it）。
- 兼容两种返回结构（result.response 与 result.choices[0].message.content）。
- 剥掉输出外层的 markdown 代码块。
- 429 / 5xx 指数退避 3 次（1s→2s→4s）；3 次失败抛异常，由上层降级。
- 标题翻译分批（每批 8 条）、编号解析（含缺编号降级）、播报稿改写。

密钥只从环境变量读取（CF_ACCOUNT_ID / CF_API_TOKEN），绝不写进代码或日志。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable

MODEL = "@cf/google/gemma-4-26b-a4b-it"
AI_ENDPOINT_TEMPLATE = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
)

# 实测有效的系统提示词，不要改写
SYSTEM_PROMPT = (
    "你是严格的翻译与摘要引擎。只输出结果本身，禁止输出开场白、解释、说明或对原文的回显。"
    "禁止输出思考过程。"
)

# 标题翻译提示词模板（每批 8 条，措辞实测有效，不要改写）
TRANSLATE_PROMPT_TEMPLATE = """把下列英文新闻标题逐条翻译成简体中文。

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

现在开始翻译这 N 条：
{titles}"""

# 播报稿改写提示词模板（措辞实测有效，不要改写）
BROADCAST_PROMPT_TEMPLATE = """你是一名电台早间新闻主播。请把下面这些新闻要点，改写成一段适合朗读的中文播报稿。

要求：
- 开头用「各位好，今天是{date}，欢迎收听今日热点。」引入
- 把要点串联成连贯的段落，不要用 1.2.3. 编号
- 口语化、句子短、便于朗读，不要出现括号、破折号等符号
- 不要添加要点里没有的信息
- 总长度控制在 600 字以内
- 必须在字数上限内写完整，最后要有完整结尾
  （例如「以上就是今天的要闻，明天同一时间再见。」）
- 只输出播报稿正文，不要任何说明

新闻要点：
{points}"""

TRANSLATE_BATCH_SIZE = 8   # 一次给 20 条模型只翻前 5 条，必须每批 8 条
MAX_RETRIES = 3            # 3 次失败后抛异常
RETRY_DELAYS = (1, 2, 4)   # 指数退避 1s → 2s → 4s
END_SENTENCE = "以上就是今天的要闻，明天同一时间再见。"  # 结束语兜底


# ── 返回解析与清洗 ──────────────────────────────────────────────────

def strip_code_block(text: str) -> str:
    """剥掉外层 markdown 代码块（```json ... ``` 或 ``` ... ```）。"""
    if not isinstance(text, str):
        return ""
    text = text.strip()
    match = re.match(r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?```$", text, re.DOTALL)
    return match.group(1).strip() if match else text


def parse_ai_response(payload: dict) -> str:
    """兼容两种返回结构，取出文本并清洗。

    格式一：{"result": {"response": "文本"}, "success": True}
    格式二：{"result": {"choices": [{"message": {"content": "文本"}}]}}

    只认一种会把成功误判成失败；两种都尝试。
    """
    if not isinstance(payload, dict):
        raise ValueError("AI 返回不是字典结构")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("AI 返回缺少 result 字段")

    text = result.get("response")
    if isinstance(text, str) and text.strip():
        return strip_code_block(text)

    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return strip_code_block(content)

    raise ValueError("AI 返回中既无 result.response 也无 result.choices[0].message.content")


# ── 核心调用 ────────────────────────────────────────────────────────

def call_ai(messages: list[dict], max_tokens: int = 4096,
            opener: Callable | None = None, sleeper: Callable | None = None,
            account_id: str | None = None, api_token: str | None = None,
            logger: Callable | None = None) -> str:
    """调用 Workers AI；429/5xx 退避 3 次，最终失败抛 RuntimeError。

    opener / sleeper 可注入以便冒烟测试零网络验证退避逻辑。
    account_id / api_token 默认从环境变量读取；测试时可显式注入假值。
    """
    account_id = account_id or os.environ.get("CF_ACCOUNT_ID", "")
    api_token = api_token or os.environ.get("CF_API_TOKEN", "")
    if not account_id or not api_token:
        raise RuntimeError("缺少 CF_ACCOUNT_ID 或 CF_API_TOKEN 环境变量")

    url = AI_ENDPOINT_TEMPLATE.format(account_id=account_id, model=MODEL)
    body = json.dumps({
        "messages": messages,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    opener = opener or urllib.request.urlopen
    sleeper = sleeper or time.sleep

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
            )
            with opener(req, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return parse_ai_response(payload)
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 仅对 429 / 5xx 退避；4xx 其他错误立即抛出
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt < MAX_RETRIES - 1:
                    if logger:
                        logger(f"AI 调用返回 {exc.code}，{RETRY_DELAYS[attempt]}s 后重试（第 {attempt + 1} 次）")
                    sleeper(RETRY_DELAYS[attempt])
                    continue
            raise RuntimeError(f"AI 调用失败：HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                if logger:
                    logger(f"AI 调用异常（{type(exc).__name__}），{RETRY_DELAYS[attempt]}s 后重试（第 {attempt + 1} 次）")
                sleeper(RETRY_DELAYS[attempt])
                continue
        except Exception as exc:
            last_error = exc
            break

    raise RuntimeError(f"AI 调用连续 {MAX_RETRIES} 次失败：{last_error}")


def call_ai_text(text: str, **kwargs) -> str:
    """用标准 system + user 结构调用一次 AI，返回纯文本。"""
    return call_ai(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        **kwargs,
    )


# ── 标题翻译 ────────────────────────────────────────────────────────

def parse_translation_output(output: str, expected: int) -> list[str]:
    """解析 [编号] 译文 格式；缺编号时按顺序降级补位。

    规则：
    - 每行匹配 ^\\[(\\d+)\\]\\s*(.+)$，编号落在 1..expected 时填入对应位置。
    - 「不要输出示例」——但模型偶尔回显示例两行；因编号 1、2 会与真实条目冲突，
      这里按“首次出现的编号优先”处理，后续重复编号忽略。
    - 无编号行：按顺序填入下一个空位。
    - 最终空位用英文原文占位由调用方处理（这里返回空串）。
    """
    results = [""] * expected
    plain_lines: list[str] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^\[(\d+)\]\s*(.+)$", line)
        if match:
            number = int(match.group(1))
            # 首次出现的编号优先；示例回显的重复编号忽略
            if 1 <= number <= expected and not results[number - 1]:
                results[number - 1] = match.group(2).strip()
        elif re.match(r"^\[?\d+\.?\]?$", line):
            # 光秃秃的编号（如 "[1.]"）没有译文，跳过，留给回退逻辑
            continue
        else:
            plain_lines.append(line)

    # 有编号的先落位，剩余空位再按无编号行的顺序补
    for line in plain_lines:
        for i in range(expected):
            if not results[i]:
                results[i] = line
                break
    return results


def translate_titles(titles_en: list[str], ai_text: Callable | None = None,
                     logger: Callable | None = None) -> list[str]:
    """分批（每批 8 条）翻译英文标题；失败回退英文原文，不空掉。

    ai_text：注入的 AI 调用函数，签名 (prompt: str) -> str；默认用 call_ai_text。
    """
    ai_text = ai_text or call_ai_text
    results: list[str] = []
    for start in range(0, len(titles_en), TRANSLATE_BATCH_SIZE):
        batch = titles_en[start:start + TRANSLATE_BATCH_SIZE]
        numbered = "\n".join(f"{i + 1}. {title}" for i, title in enumerate(batch))
        prompt = TRANSLATE_PROMPT_TEMPLATE.format(titles=numbered)
        try:
            output = ai_text(prompt)
            parsed = parse_translation_output(output, len(batch))
        except Exception as exc:
            if logger:
                logger(f"标题翻译失败，回退英文原文：{exc}")
            parsed = [""] * len(batch)
        # 空位回退英文原文，保证不空掉
        for i, item in enumerate(parsed):
            results.append(item or batch[i])
    return results


# ── 播报稿 ──────────────────────────────────────────────────────────

def truncate_at_sentence(text: str, limit: int = 600) -> str:
    """在句子边界截断（不能切断词）；limit 内找最近的句号/问号/感叹号。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    # 在 limit 内找最后一个句末标点
    boundaries = [window.rfind(mark) for mark in ("。", "！", "？", ".", "!", "?")]
    pos = max(boundaries)
    if pos >= 0:
        return window[:pos + 1]
    # 找不到句子边界时，退到空白符边界（不切断词）
    space_pos = max(window.rfind(" "), window.rfind("\n"))
    return window[:space_pos] if space_pos > 0 else window


def ensure_ending(text: str, limit: int = 600) -> str:
    """保证播报稿有完整结尾；必要时截断到句子边界后补结束语。"""
    text = (text or "").strip()
    if not text:
        return END_SENTENCE
    if text.endswith(END_SENTENCE):
        return text
    if "再见" in text[-20:] or "就到这里" in text[-20:]:
        return text
    truncated = truncate_at_sentence(text, limit)
    # 预留结束语的空间：若补上后会超 limit，再截短一点
    room = limit - len(END_SENTENCE) - 1
    if len(truncated) > room:
        truncated = truncate_at_sentence(truncated, room)
    return truncated + END_SENTENCE


def generate_broadcast(points: list[str], date_str: str,
                       ai_text: Callable | None = None,
                       logger: Callable | None = None) -> str:
    """用排名靠前的若干条要点改写播报稿；失败降级为要点拼接 + 完整结尾。"""
    ai_text = ai_text or call_ai_text
    numbered = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(points))
    prompt = BROADCAST_PROMPT_TEMPLATE.format(date=date_str, points=numbered)
    try:
        output = ai_text(prompt)
        return ensure_ending(output)
    except Exception as exc:
        if logger:
            logger(f"播报稿生成失败，降级为要点拼接：{exc}")
        fallback = f"各位好，今天是{date_str}，欢迎收听今日热点。" + "。".join(points)
        return ensure_ending(fallback)
