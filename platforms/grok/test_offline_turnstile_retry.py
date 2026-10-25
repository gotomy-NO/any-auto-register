# -*- coding: utf-8 -*-
"""离线验证：geoip 一致化启动参数 + Turnstile 拒绝后的「换会话+换邮箱+退避」重试。

运行：.venv/Scripts/python.exe platforms/grok/test_offline_turnstile_retry.py
不联网、不启动浏览器。真实验收另由服务重启后的真实批量任务完成。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platforms.grok import plugin as gp
from platforms.grok import xai_browser as xbr
from platforms.grok.plugin import GrokPlatform, _turnstile_backoff, _sleep_interruptible
from platforms.grok.xai_browser import (
    XaiBrowserError,
    XaiTurnstileRejected,
)


def test_launch_kwargs():
    """_launch_geoip_kwargs：GeoIP 已装 → geoip+disable_coop+humanize 全开。"""
    kwargs = xbr._launch_geoip_kwargs({"server": "http://127.0.0.1:7897"})
    assert kwargs.get("geoip") is True, kwargs
    assert kwargs.get("disable_coop") is True, kwargs
    assert kwargs.get("humanize") is True, kwargs
    print("launch kwargs (geoip installed):", kwargs)

    # 降级链：geoip_allowed 抛错（未装 extra）→ 仍保留 disable_coop/humanize
    real = xbr_input = None
    import camoufox.geolocation as geo

    class _Boom:
        def __call__(self):
            raise RuntimeError("simulated geoip missing")

    saved = geo.geoip_allowed
    try:
        geo.geoip_allowed = _Boom()
        kwargs2 = xbr._launch_geoip_kwargs(None)
        assert "geoip" not in kwargs2, kwargs2
        assert kwargs2.get("disable_coop") is True and kwargs2.get("humanize") is True
        print("launch kwargs (geoip missing fallback):", kwargs2)
    finally:
        geo.geoip_allowed = saved


def test_backoff_table():
    assert (_turnstile_backoff(0), _turnstile_backoff(1), _turnstile_backoff(2)) == (
        20.0,
        45.0,
        90.0,
    )
    assert _turnstile_backoff(99) == 90.0
    print("backoff table OK: 20/45/90 (cap 90)")


class _FakeMailbox:
    """最小邮箱桩：每次 get_email 发新号；requeue 只记账。"""

    def __init__(self):
        self.n = 0
        self.requeued = []

    def get_email(self, platform=""):
        self.n += 1
        return type("MA", (), {"email": f"tmp{self.n}@ee-mail.cc.cd"})()

    def get_current_ids(self, acct):
        return set()

    def wait_for_code(self, acct, **kw):
        return "ABC-DEF"

    def requeue_account(self, acct, platform="", failed=False, **kw):
        self.requeued.append((acct.email, failed))


def _build_platform(monkey_reg, attempts=3):
    p = GrokPlatform.__new__(GrokPlatform)  # 跳过 __init__ 的注册表副作用
    from core.base_platform import RegisterConfig

    p.config = RegisterConfig(executor_type="headless", proxy="http://1.2.3.4:8080")
    p.mailbox = _FakeMailbox()
    p._log_fn = lambda msg: None
    # 注入桩 register
    reg = type("R", (), {"register": staticmethod(monkey_reg)})()
    real_init = GrokPlatform.register

    import platforms.grok.core as gcore

    saved = gcore.GrokRegister
    gcore.GrokRegister = lambda **kw: reg
    p._saved_grokregister = (gcore, saved)
    p._mailbox_attempts = attempts
    return p


def _with_patched_register(fn, attempts=3, backoff_table=(0.05, 0.05, 0.05)):
    """把 GrokRegister.register 换成桩，跑 GrokPlatform.register。

    backoff_table 桩化退避表（默认 20/45/90 是生产节奏，测试不能真等 155s）。
    """
    import platforms.grok.core as gcore
    import platforms.grok.xai_browser as x

    saved = gcore.GrokRegister
    saved_table = x.TURNSTILE_RETRY_BACKOFF_SEC
    x.TURNSTILE_RETRY_BACKOFF_SEC = backoff_table
    calls = {"n": 0, "emails": []}

    class FakeReg:
        def register(self, email, password=None, otp_callback=None):
            calls["n"] += 1
            calls["emails"].append(email)
            return fn(calls["n"], email)

    gcore.GrokRegister = lambda **kw: FakeReg()
    try:
        p = GrokPlatform(
            config=__import__("core.base_platform", fromlist=["RegisterConfig"]).RegisterConfig(
                executor_type="headless", proxy="http://1.2.3.4:8080"
            ),
            mailbox=_FakeMailbox(),
        )
        p._log_fn = lambda msg: None
        result = p.register(email=None, password=None)
        return result, calls
    finally:
        gcore.GrokRegister = saved
        x.TURNSTILE_RETRY_BACKOFF_SEC = saved_table


def test_retry_exchange_email():
    """第 1 次 Turnstile 拒 → 换邮箱重试；第 2 次成功。"""
    from core.base_platform import RegisterConfig

    def flow(n, email):
        if n == 1:
            raise XaiTurnstileRejected("xAI 人机验证未自动通过（headless）；重试需新会话+退避")
        return {
            "email": email,
            "password": "x,,,aA1",
            "given_name": "A",
            "family_name": "B",
            "tokens": {"access_token": "A" * 10, "refresh_token": "R" * 10},
        }

    result, calls = _with_patched_register(flow, attempts=3)
    assert calls["n"] == 2, calls
    assert calls["emails"][0] != calls["emails"][1], "must switch email"
    assert result.extra["tokens"]["access_token"] == "A" * 10
    print("retry-after-reject OK: 2 sessions, emails:", calls["emails"])


def test_retry_exhaustion():
    """连续 Turnstile 拒 → 耗尽 attempts 后原样上抛，最后一次异常不被吞。"""
    from core.base_platform import RegisterConfig

    def flow(n, email):
        raise XaiTurnstileRejected("xAI 人机验证未自动通过（headless）；重试需新会话+退避")

    try:
        _with_patched_register(flow, attempts=3)
        raise AssertionError("must raise after exhaustion")
    except XaiTurnstileRejected as e:
        print("exhaustion OK: raised through:", str(e)[:40])


def test_non_turnstile_no_retry():
    """其他 XaiBrowserError（如表单异常）不触发换邮箱重试，直接上抛。"""
    from core.base_platform import RegisterConfig

    def flow(n, email):
        raise XaiBrowserError("xAI 页面没有可用的继续按钮")

    try:
        _with_patched_register(flow, attempts=3)
        raise AssertionError("must raise immediately")
    except XaiBrowserError as e:
        assert "继续按钮" in str(e)
        print("non-turnstile error passthrough OK (no email retry)")


def test_backoff_actually_waited():
    """退避真实发生：退避表桩成 0.3s，验证重试间隔 >=0.3s（接线生效）。"""
    from core.base_platform import RegisterConfig

    def flow(n, email):
        if n < 2:
            raise XaiTurnstileRejected("rejected")
        return {
            "email": email,
            "password": "x,,,aA1",
            "given_name": "A",
            "family_name": "B",
            "tokens": {"access_token": "A" * 10, "refresh_token": "R" * 10},
        }

    t0 = time.time()
    result, calls = _with_patched_register(
        flow, attempts=3, backoff_table=(0.3, 0.3, 0.3)
    )
    elapsed = time.time() - t0
    assert calls["n"] == 2 and elapsed >= 0.3, (calls, elapsed)
    print(f"backoff waited OK: elapsed={elapsed:.2f}s >= 0.3s")


def main():
    test_launch_kwargs()
    test_backoff_table()
    test_retry_exchange_email()
    test_retry_exhaustion()
    test_non_turnstile_no_retry()
    test_backoff_actually_waited()
    print("== ALL turnstile-retry offline tests PASS ==")


if __name__ == "__main__":
    main()
