# -*- coding: utf-8 -*-
"""编排级离线验证：stub 浏览器+兑换，验证 core.GrokRegister.register 全链路。
运行：.venv/Scripts/python.exe platforms/grok/test_offline_orchestration.py
不联网、不启动浏览器、不真实验证码服务。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platforms.grok import core as gcore
from platforms.grok import xai_browser as xbr
from platforms.grok import xai_pkce as pkce


class FakeSession:
    def authorize_pkce(self, auth_url, expected_state):
        return f"http://127.0.0.1:56121/callback?code=AUTHCODE123&state={expected_state}"

    def prepare_pkce_retry(self):
        pass

    def close(self):
        pass

    def hold_failure(self, **kw):
        pass


def fake_register_xai_account(**kwargs):
    gc = kwargs["get_verification_code"]
    assert gc(False) == "ABCDEF" and gc(True) == "ABCDEF" and gc() == "ABCDEF"
    assert kwargs["proxy"] == "http://1.2.3.4:8080"
    assert kwargs["headless"] is True
    assert kwargs["password"].endswith(",,,aA1")
    return {"ok": True, "email": kwargs["email"], "cookies": {}, "steps": []}, FakeSession()


calls = {}


def fake_exchange(code, session, **kw):
    calls.update(code=code, verifier=session.code_verifier, proxy=kw.get("token_proxy"))
    return {
        "access_token": "ATPRE" + "x" * 50,
        "refresh_token": "RTPRE" + "y" * 40,
        "id_token": "IDT" + "z" * 30,
        "expires_in": 3600,
    }


def main():
    xbr.register_xai_account = fake_register_xai_account
    pkce.exchange_code = fake_exchange

    logs = []
    reg = gcore.GrokRegister(proxy="http://1.2.3.4:8080", log_fn=logs.append, headless=True)
    result = reg.register(email="probe@example.com", password=None, otp_callback=lambda: "ABCDEF")
    tok = result["tokens"]

    assert tok["access_token"].startswith("ATPRE") and len(tok["access_token"]) == 55
    assert tok["refresh_token"].startswith("RTPRE") and len(tok["refresh_token"]) == 45
    assert tok["id_token"].startswith("IDT")
    assert tok["expires_at"].endswith("Z") and tok["oidc_client_id"] == pkce.CLIENT_ID
    assert calls["code"] == "AUTHCODE123" and len(calls["verifier"]) == 43
    assert calls["proxy"] == "http://1.2.3.4:8080"

    joined = "\n".join(str(x) for x in logs)
    for marker in ("AUTHCODE123", "x" * 10, "y" * 10, "z" * 10, "ATPREx", "RTPREy", "IDTz"):
        assert marker not in joined, f"token leaked into log: {marker}"
    mask = [x for x in joined.splitlines() if "AT=" in x]
    assert mask and "AT=ATPR...<len=55>" in mask[0], mask

    from core.nr_client import canonical_from_account

    class Row:
        email = result["email"]

        def get_extra(self):
            return {"tokens": tok}

    item = canonical_from_account("grok-cli", Row())
    assert item["access_token"] == tok["access_token"]
    assert item["refresh_token"] == tok["refresh_token"]
    assert item["id_token"] == tok["id_token"]
    assert item["email"] == "probe@example.com"

    # verifier 用后即抹（成功与失败路径）
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "s.json"

        def ok_exchange(code, session, **kw):
            return {"access_token": "A" * 10, "refresh_token": "R" * 10}

        pkce.exchange_code = ok_exchange
        pkce.authorize_and_exchange(
            lambda u, s: f"http://127.0.0.1:56121/callback?code=C&state={s}", session_path=f)
        assert json.loads(f.read_text(encoding="utf-8"))["code_verifier"] == ""

        def boom(code, session, **kw):
            raise RuntimeError("network down")

        pkce.exchange_code = boom
        try:
            pkce.authorize_and_exchange(
                lambda u, s: f"http://127.0.0.1:56121/callback?code=C&state={s}", session_path=f)
        except RuntimeError:
            pass
        assert json.loads(f.read_text(encoding="utf-8"))["code_verifier"] == ""

    print("== orchestration + masking + canonical + verifier-wipe PASS ==")
    print("masked log:", mask[0].strip())
    print("bulk-import keys:", sorted(item.keys()))


if __name__ == "__main__":
    main()
