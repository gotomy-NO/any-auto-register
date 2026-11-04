# -*- coding: utf-8 -*-
"""Camoufox 启动熔断器（host_browser_broken）— 纯标准库，跨进程共用。

背景（2026-10-07）：主机级 camoufox 启动故障，`Camoufox.start: Timeout 180000ms
exceeded` + RenderCompositorSWGL 故障，批产每轮白烧 3 分钟×3 回退（当日 backend.log
命中 172 次启动超时）。本模块把「连续 N 次启动超时类失败」判定为宿主浏览器不可用，
写熔断状态；批循环(:8000)/守护(scripts)/任务执行器读它来停止白烧。

接口约定（读者三处一致）：
  * :8000 进程（platforms/grok/xai_browser.py）——写：启动成功 record_launch_success()，
    全部回退失败且属超时类 record_launch_failure(exc_text)。
  * scripts/grok_batch_loop.py —— 读 is_open()，OPEN 则长退避不挂批。
  * scripts/grok_batch_guard.py —— 读 is_open()，OPEN 则 should_run=False（不重挂循环）
    + set_halt(host_browser_broken)；并每 30 分钟短超时探针，成功则 clear() 自动恢复。
  * api/tasks.py —— 读 is_open()，grok 批次内 OPEN 则跳过剩余账号（止住批内白烧）。

状态文件：%LOCALAPPDATA%/hermes/cache/scratch/camoufox_breaker.json（与 grok_batch_loop/
guard 的 state 同目录）。可用 env CAMOUFOX_BREAKER_PATH 覆盖（测试用）。
默认「不存在 == CLOSED」，即文件被清理也不会误伤批产（fail-open toward 生产）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

# 连续多少次「启动超时类」失败即熔断
FAIL_THRESHOLD = 3
# 识别启动超时/宿主浏览器故障的关键字（大小写敏感，匹配真实异常文本）
TIMEOUT_KEYWORDS = ("Camoufox.start", "Timeout", "RenderCompositorSWGL", "Juggler")
# 计数窗口：距上次失败超过该分钟数视为「非连续」，下次失败从 1 重新计
CONSECUTIVE_WINDOW_MIN = 120
# 熔断原因码（固定词，便于守护/告警匹配）
REASON = "host_browser_broken"


def _breaker_path() -> str:
    env = os.environ.get("CAMOUFOX_BREAKER_PATH")
    if env:
        return env
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", "cache", "scratch")
    return os.path.join(base, "camoufox_breaker.json")


def _now() -> datetime:
    return datetime.now()


def _load() -> dict:
    try:
        with open(_breaker_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save(d: dict) -> None:
    p = _breaker_path()
    try:
        d["updated_at"] = _now().isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass


def classify_launch_error(error_text) -> bool:
    """异常文本是否属「启动超时/宿主浏览器故障」类。"""
    t = str(error_text or "")
    return any(k in t for k in TIMEOUT_KEYWORDS)


def _count_is_fresh(d: dict) -> bool:
    last = d.get("last_failure_at")
    if not last:
        return False
    try:
        return (_now() - datetime.fromisoformat(last)) <= timedelta(minutes=CONSECUTIVE_WINDOW_MIN)
    except Exception:  # noqa: BLE001
        return False


def snapshot() -> dict:
    d = _load()
    d.setdefault("state", "CLOSED")
    d.setdefault("consecutive_failures", 0)
    d.setdefault("reason", "")
    return d


def is_open() -> bool:
    """熔断是否 OPEN。文件不存在/损坏 → False（fail-open 保生产）。"""
    return _load().get("state") == "OPEN"


def record_launch_success() -> dict:
    """任一次 camoufox 启动成功 → 复位计数并 CLOSED（宿主已恢复）。"""
    d = _load()
    d["state"] = "CLOSED"
    d["consecutive_failures"] = 0
    d["reason"] = ""
    d["last_success_at"] = _now().isoformat(timespec="seconds")
    _save(d)
    return d


def record_launch_failure(error_text) -> dict:
    """启动超时类失败 +1；达阈值 → OPEN(host_browser_broken)。非该类失败不动本熔断。"""
    text = str(error_text or "")[:600]
    if not classify_launch_error(text):
        return snapshot()
    d = _load()
    n = int(d.get("consecutive_failures") or 0)
    if not _count_is_fresh(d):
        n = 0
    n += 1
    d["consecutive_failures"] = n
    d["last_failure_at"] = _now().isoformat(timespec="seconds")
    d["last_error"] = text
    d.setdefault("state", "CLOSED")
    if n >= FAIL_THRESHOLD and d.get("state") != "OPEN":
        d["state"] = "OPEN"
        d["reason"] = REASON
        d["opened_at"] = _now().isoformat(timespec="seconds")
    _save(d)
    return d


def clear(reason: str = "manual") -> dict:
    """人工/探针解除熔断。"""
    d = _load()
    d["state"] = "CLOSED"
    d["consecutive_failures"] = 0
    d["reason"] = ""
    d["cleared_by"] = reason
    d["cleared_at"] = _now().isoformat(timespec="seconds")
    _save(d)
    return d


def probe_due(interval_min: float) -> bool:
    """熔断 OPEN 且距上次探针 ≥ interval_min 分钟 → 该探针了。"""
    d = _load()
    if d.get("state") != "OPEN":
        return False
    last = d.get("last_probe_at")
    if not last:
        return True
    try:
        return (_now() - datetime.fromisoformat(last)) >= timedelta(minutes=interval_min)
    except Exception:  # noqa: BLE001
        return True


def note_probe(ok: bool) -> dict:
    """记录一次探针结果；成功即自动解除熔断。"""
    d = _load()
    d["last_probe_at"] = _now().isoformat(timespec="seconds")
    d["last_probe_ok"] = bool(ok)
    if ok:
        d["state"] = "CLOSED"
        d["consecutive_failures"] = 0
        d["reason"] = ""
        d["cleared_by"] = "auto_probe"
        d["cleared_at"] = _now().isoformat(timespec="seconds")
    _save(d)
    return d


if __name__ == "__main__":  # 手动查看状态
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        print(json.dumps(clear("manual_cli"), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(snapshot(), ensure_ascii=False, indent=1))
