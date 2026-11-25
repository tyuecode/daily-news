#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_audio.py — 用 edge-tts 把中文语音播报稿转成 MP3。

设计要点
--------
* 增量生成：每篇稿子旁边存一份 <date>.json 元数据（含稿件 sha256）。
  稿件没变且 MP3 存在且元数据一致 → 跳过，不重复消耗 TTS 配额。
  这样配合 GitHub Actions 的 restore-keys 缓存，每天只合成新增的一期。
* 失败重试：edge-tts 依赖微软公开接口，偶发网络抖动 / 429 很正常，
  单篇最多重试 --retries 次，指数退避。
* 时长精确：用 WordBoundary 事件的 offset+duration 累计出真实时长，
  写进元数据，站点直接读，渲染时不需要 ffmpeg/ffprobe。
* 不静默失败：任何一期最终失败都会让脚本以非 0 退出，避免发布一个
  缺音频的站点。

用法
----
    python scripts/make_audio.py                        # 生成全部缺失/过期的音频
    python scripts/make_audio.py --force                # 强制全部重新生成
    python scripts/make_audio.py --only 2026-11-25      # 只处理指定日期
    python scripts/make_audio.py --voice zh-CN-YunxiNeural --rate +5%
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import edge_tts
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "错误：未安装 edge-tts。请先运行  pip install edge-tts\n"
    )
    sys.exit(2)

DEFAULT_VOICE = "zh-CN-YunyangNeural"  # 央视新闻主播音色，最适合播报
DEFAULT_RATE = "+0%"
DEFAULT_VOLUME = "+0%"


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_text(raw: str) -> str:
    """清理 TTS 文本：去 BOM、统一换行、压缩多余空白。"""
    text = raw.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 去掉行首行尾空白，保留空行（空行给 TTS 自然停顿）
    lines = [ln.strip() for ln in text.split("\n")]
    # 连续空行压成一个
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def collect_jobs(src_dir: Path, only: Optional[str]) -> list[tuple[str, Path]]:
    """
    扫描 audio-script/YYYY/MM/YYYY-MM-DD.txt，返回 [(date, path)]，按日期升序。
    """
    jobs: list[tuple[str, Path]] = []
    for path in sorted(src_dir.rglob("*.txt")):
        name = path.stem
        # 严格匹配 YYYY-MM-DD
        try:
            datetime.strptime(name, "%Y-%m-%d")
        except ValueError:
            log(f"  跳过非日期文件：{path}")
            continue
        if only and name != only:
            continue
        jobs.append((name, path))
    jobs.sort(key=lambda x: x[0])
    return jobs


def read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# --------------------------------------------------------------------------
# 单篇合成
# --------------------------------------------------------------------------
async def synth_one(
    text: str,
    out_mp3: Path,
    voice: str,
    rate: str,
    volume: str,
    retries: int,
) -> dict:
    """
    合成一段文本，返回 {'duration_ms': int, 'bytes': int}。
    失败抛异常，由调用方决定是否让整个脚本失败。
    """
    last_err: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            communicate = edge_tts.Communicate(
                text,
                voice,
                rate=rate,
                volume=volume,
                boundary="WordBoundary",
            )

            chunks: list[bytes] = []
            end_offsets: list[int] = []

            async for chunk in communicate.stream():
                ctype = chunk.get("type")
                if ctype == "audio":
                    chunks.append(chunk["data"])
                elif ctype == "WordBoundary":
                    # offset / duration 单位是 100 纳秒
                    off = int(chunk.get("offset", 0))
                    dur = int(chunk.get("duration", 0))
                    end_offsets.append(off + dur)

            audio = b"".join(chunks)
            if len(audio) < 1024:
                raise RuntimeError(
                    f"生成的音频过小（{len(audio)} 字节），判定为失败"
                )

            out_mp3.parent.mkdir(parents=True, exist_ok=True)
            tmp = out_mp3.with_suffix(".mp3.tmp")
            tmp.write_bytes(audio)
            os.replace(tmp, out_mp3)

            # 100ns -> ms
            duration_ms = int(max(end_offsets) / 10_000) if end_offsets else 0
            return {"duration_ms": duration_ms, "bytes": len(audio)}

        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                backoff = min(2 ** attempt, 20)
                log(f"    第 {attempt} 次失败：{exc!r}；{backoff}s 后重试…")
                await asyncio.sleep(backoff)

    raise RuntimeError(f"重试 {retries} 次后仍失败：{last_err!r}")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
async def main_async(args: argparse.Namespace) -> int:
    src_dir = Path(args.audio_src)
    out_dir = Path(args.out_dir)

    if not src_dir.exists():
        log(f"错误：找不到稿件目录 {src_dir}")
        return 2

    jobs = collect_jobs(src_dir, args.only)
    if not jobs:
        log(f"没有找到待处理的语音稿（{src_dir}）")
        return 0

    log(f"语音稿目录：{src_dir}")
    log(f"输出目录  ：{out_dir}")
    log(f"音色      ：{args.voice}  语速：{args.rate}  音量：{args.volume}")
    log(f"待检查 {len(jobs)} 篇\n")

    generated: list[str] = []
    skipped = 0
    failed: list[tuple[str, str]] = []

    for idx, (date, path) in enumerate(jobs, 1):
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = clean_text(raw)
        if not text:
            log(f"[{idx}/{len(jobs)}] {date}：稿件为空，跳过")
            failed.append((date, "稿件内容为空"))
            continue

        digest = sha256_text(text)
        out_mp3 = out_dir / f"{date}.mp3"
        meta_path = out_dir / f"{date}.json"
        meta = read_meta(meta_path)

        unchanged = (
            out_mp3.exists()
            and meta.get("source_sha256") == digest
            and meta.get("voice") == args.voice
            and meta.get("rate") == args.rate
            and meta.get("volume") == args.volume
            and int(meta.get("bytes", 0)) > 1024
        )

        if unchanged and not args.force:
            log(f"[{idx}/{len(jobs)}] {date}：无变化，跳过")
            skipped += 1
            continue

        chars = len(text)
        log(f"[{idx}/{len(jobs)}] {date}：合成中…（{chars} 字）")
        started = time.time()

        try:
            result = await synth_one(
                text, out_mp3, args.voice, args.rate, args.volume, args.retries
            )
        except Exception as exc:  # noqa: BLE001
            log(f"    失败：{exc}")
            failed.append((date, str(exc)))
            continue

        elapsed = time.time() - started
        dur_ms = result["duration_ms"]
        mins = dur_ms / 60000.0 if dur_ms else 0.0

        meta_path.write_text(
            json.dumps(
                {
                    "date": date,
                    "file": f"{date}.mp3",
                    "voice": args.voice,
                    "rate": args.rate,
                    "volume": args.volume,
                    "source_sha256": digest,
                    "duration_ms": dur_ms,
                    "bytes": result["bytes"],
                    "chars": chars,
                    "generated_at": datetime.now(timezone.utc)
                    .astimezone()
                    .isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        generated.append(date)
        log(
            f"    完成：{result['bytes'] / 1024:.1f} KB，时长 "
            f"{mins:.1f} 分钟，耗时 {elapsed:.1f}s"
        )

    # ---------------- 汇总 ----------------
    log("\n" + "=" * 58)
    log(f"新生成 {len(generated)} 篇：{', '.join(generated) if generated else '无'}")
    log(f"跳过   {skipped} 篇（未变化）")
    if failed:
        log(f"失败   {len(failed)} 篇：")
        for date, reason in failed:
            log(f"  - {date}: {reason}")
        log("=" * 58)
        return 1

    log("=" * 58)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="用 edge-tts 把语音播报稿转成 MP3（增量生成）"
    )
    p.add_argument(
        "--audio-src", default="audio-script", help="语音稿根目录（默认 audio-script）"
    )
    p.add_argument(
        "--out-dir", default="dist/audio", help="MP3 输出目录（默认 dist/audio）"
    )
    p.add_argument("--voice", default=DEFAULT_VOICE, help=f"TTS 音色（默认 {DEFAULT_VOICE}）")
    p.add_argument("--rate", default=DEFAULT_RATE, help="语速，如 +0%% / +8%%（默认 +0%%）")
    p.add_argument("--volume", default=DEFAULT_VOLUME, help="音量，如 +0%%（默认 +0%%）")
    p.add_argument("--retries", type=int, default=4, help="单篇失败重试次数（默认 4）")
    p.add_argument("--force", action="store_true", help="忽略缓存，强制重新生成")
    p.add_argument("--only", default=None, help="只处理指定日期，如 2026-11-25")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
