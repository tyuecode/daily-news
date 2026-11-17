#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare Workers AI 调用封装。

本模块只负责一件事：把「一组 messages」变成「一段纯文本」，
其余（重试策略之外的降级、内容校验、提示词）全部交给上层。

以下四点均为实测踩坑结论，改动前请先读完：

1. 返回结构有两种，必须都兼容：
   - 纯文本模型：     {"result": {"response": "文本"}, "success": true}
   - OpenAI 兼容模型：{"result": {"choices": [{"message": {"content": "文本"}}]}}
   历史上只认第一种，把成功的调用误判成失败，导致整站降级成英文标题。

2. 模型偶尔会把结果整段包进 markdown 代码块（```），必须剥掉再使用。

3. 429 / 5xx / 网络错误要指数退避重试（1s → 2s → 4s），累计 3 次仍失败就抛
   AIError，由上层降级（例如标题回退英文原文），绝不因为一次 AI 失败把整站搞空。

4. 账号与令牌只从环境变量读取（CF_ACCOUNT_ID / CF_API_TOKEN），
   任何情况下都不允许写进代码、注释或日志。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

# 唯一文本模型（选型结论见技术调研文档，禁止随意替换）
DEFAULT_MODEL = "@cf/google/gemma-4-26b-a4b-it"

# 端点模板
ENDPOINT = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
)

# 系统提示词：措辞实测有效，不要改写
SYSTEM_PROMPT = (
    "你是严格的翻译与摘要引擎。只输出结果本身，"
    "禁止输出开场白、解释、说明或对原文的回显。禁止输出思考过程。"
)

# 重试参数：429 / 5xx 指数退避，1s → 2s → 4s
MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.0
DEFAULT_TIMEOUT = 90

# gemma-4-26b-a4b-it 的计费单价（neurons / 百万 token），用于估算消耗
PRICE_INPUT_PER_M = 9091
PRICE_OUTPUT_PER_M = 27273

# 免费层每日额度（neurons），用于在运行报告里给出占比
DAILY_QUOTA = 10000


class AIError(RuntimeError):
    """AI 调用失败（重试耗尽、返回结构无法识别、未配置凭据等）。"""


class _Retryable(AIError):
    """可重试的错误：429、5xx、网络中断。"""


def strip_code_fence(text):
    """剥掉模型输出外层的 markdown 代码块。

    >>> strip_code_fence('```json\\n{"a": 1}\\n```')
    '{"a": 1}'
    >>> strip_code_fence('```\\n甲\\n乙\\n```')
    '甲\\n乙'
    >>> strip_code_fence('正常的输出')
    '正常的输出'
    """
    if not text:
        return ""
    t = str(text).strip()
    if t.startswith("```"):
        # 去掉第一行（可能是 ``` 或 ```json / ```text）
        first_nl = t.find("\n")
        if first_nl == -1:
            # 整段只有一行 ```xxx``` 的畸形输出
            return t.strip("`").strip()
        t = t[first_nl + 1:]
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def extract_text(payload):
    """从 Workers AI 的响应 JSON 里取出文本，兼容两种返回结构。

    两种结构都必须支持，否则会把成功调用误判为失败。
    """
    if not isinstance(payload, dict):
        raise AIError("AI 返回不是 JSON 对象，无法解析")

    if payload.get("success") is False:
        errors = payload.get("errors") or []
        raise AIError("AI 返回 success=false：%s" % json.dumps(errors, ensure_ascii=False)[:300])

    result = payload.get("result")
    if result is None:
        raise AIError("AI 返回缺少 result 字段")

    # 结构一：纯文本模型
    if isinstance(result, dict):
        resp = result.get("response")
        if isinstance(resp, str) and resp.strip():
            return resp

        # 结构二：OpenAI 兼容模型
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] or {}
            message = first.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                # 少数模型会把 content 拆成 [{type/text}] 数组
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if isinstance(content, str) and content.strip():
                return content
            # 兜底：部分模型用 text 字段
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                return text

        # 结构三：result 直接是字符串（极少数情况）
    if isinstance(result, str) and result.strip():
        return result

    raise AIError("AI 返回结构无法识别：%s" % json.dumps(payload, ensure_ascii=False)[:300])


class AIClient:
    """Cloudflare Workers AI 的最小客户端。

    用法：
        client = AIClient()
        text = client.complete("把下面的标题翻译成中文：\\n1. ...")
    """

    def __init__(
        self,
        account_id=None,
        api_token=None,
        model=None,
        timeout=DEFAULT_TIMEOUT,
        sleep=time.sleep,
    ):
        # 凭据优先取参数，其次取环境变量；只用 strip，不打印
        self.account_id = (account_id or os.environ.get("CF_ACCOUNT_ID") or "").strip()
        self.api_token = (api_token or os.environ.get("CF_API_TOKEN") or "").strip()
        self.model = (model or os.environ.get("CF_MODEL") or "").strip() or DEFAULT_MODEL
        self.timeout = timeout
        self._sleep = sleep

        # 统计（用于运行报告里的消耗估算）
        self.calls = 0
        self.ok_calls = 0
        self.failed_calls = 0
        self.prompt_chars = 0
        self.response_chars = 0

    # ------------------------------------------------------------------ 基础

    @property
    def configured(self):
        """凭据是否齐全。"""
        return bool(self.account_id and self.api_token)

    def _url(self):
        return ENDPOINT.format(account_id=self.account_id, model=self.model)

    def _post(self, body_bytes):
        """真正发起 HTTP 请求，把错误归类成可重试 / 不可重试。"""
        req = urllib.request.Request(
            self._url(),
            data=body_bytes,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.api_token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # 429 与 5xx 可重试，其余（401/403/400）立即失败
            if exc.code == 429 or 500 <= exc.code < 600:
                raise _Retryable("AI 接口返回 HTTP %s" % exc.code)
            raise AIError("AI 接口返回 HTTP %s（凭据或参数有问题，不重试）" % exc.code)
        except urllib.error.URLError as exc:
            raise _Retryable("AI 接口网络错误：%s" % exc.reason)
        except TimeoutError:
            raise _Retryable("AI 接口连接超时")
        except OSError as exc:
            raise _Retryable("AI 接口连接失败：%s" % exc)

        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise _Retryable("AI 接口返回的不是合法 JSON")

    # ------------------------------------------------------------------ 调用

    def complete(self, user_prompt, system_prompt=SYSTEM_PROMPT, max_tokens=4096):
        """把一段用户提示词变成模型输出文本。

        max_tokens 不能太小：1024 会把译文截断（实测）。
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self.call(messages, max_tokens=max_tokens)

    def call(self, messages, max_tokens=4096):
        """按 messages 调用，带指数退避重试。"""
        if not self.configured:
            raise AIError("缺少 CF_ACCOUNT_ID / CF_API_TOKEN，无法调用 AI")

        body = json.dumps(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                # 只有部分模型支持该参数，不支持的会忽略，属于安全的多余参数
                "chat_template_kwargs": {"enable_thinking": False},
            },
            ensure_ascii=False,
        ).encode("utf-8")

        prompt_chars = sum(
            len(m.get("content") or "") for m in messages if isinstance(m, dict)
        )
        self.calls += 1
        self.prompt_chars += prompt_chars

        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                payload = self._post(body)
                text = strip_code_fence(extract_text(payload))
                self.ok_calls += 1
                self.response_chars += len(text)
                if not text:
                    raise AIError("AI 返回了空文本")
                return text
            except _Retryable as exc:
                last_error = exc
                if attempt == MAX_ATTEMPTS - 1:
                    break
                # 指数退避：1s → 2s
                self._sleep(BACKOFF_BASE * (2 ** attempt))
            except AIError as exc:
                # 不可重试的错误直接上抛，由上层降级
                self.failed_calls += 1
                raise exc

        self.failed_calls += 1
        raise AIError("AI 调用重试 %d 次仍失败：%s" % (MAX_ATTEMPTS, last_error))

    # ------------------------------------------------------------------ 统计

    def usage_report(self):
        """估算 tokens 与 neurons 消耗，用于写入 data/runs/*.json。"""
        # 粗略折算：中文约 1 token / 1.6 字符，英文约 1 token / 4 字符，
        # 这里统一按 1 token ≈ 3 字符估算，只用于给用户一个数量级参考。
        in_tokens = self.prompt_chars / 3.0
        out_tokens = self.response_chars / 3.0
        neurons = (
            in_tokens * PRICE_INPUT_PER_M + out_tokens * PRICE_OUTPUT_PER_M
        ) / 1_000_000.0
        return {
            "模型": self.model,
            "调用次数": self.calls,
            "成功次数": self.ok_calls,
            "失败次数": self.failed_calls,
            "输入字符数": self.prompt_chars,
            "输出字符数": self.response_chars,
            "估算_neurons": round(neurons, 1),
            "占免费日额度百分比": round(neurons / DAILY_QUOTA * 100, 2),
        }


def clean_model_output_lines(text):
    """把模型输出按行清洗：去掉首尾空白、去掉行首的 - 或 * 列表符号。"""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[-*•]\s+", "", line)
        lines.append(line)
    return lines
