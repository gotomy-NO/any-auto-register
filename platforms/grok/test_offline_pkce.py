# -*- coding: utf-8 -*-
"""离线测试：不联网、不启动浏览器。验证 PKCE/回调/payload/canonical 对齐。"""
import base64
import hashlib
import io
import json
import re
import sys
import types
from pathlib import Path

ROOT = Path(r"D:/AI/临时/Grok注册机/register-8000")
sys.path.insert(0, str(ROOT))

passed = []
failed = []

def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))

# ---------- ① 纯函数模块直接 import（无第三方依赖） ----------
import platforms.grok.xai_pkce as pkce

check("import xai_pkce", True)

# RFC 7636: verifier 长度 43~128，challenge = b64url(sha256(verifier))
s = pkce.create_pkce_session()
check("verifier length 43", len(s.code_verifier) == 43, str(len(s.code_verifier)))
check("verifier charset", re.fullmatch(r"[A-Za-z0-9_-]+", s.code_verifier) is not None)
expect_challenge = base64.urlsafe_b64encode(hashlib.sha256(s.code_verifier.encode("ascii")).digest()).decode().rstrip("=")
check("challenge S256 correct", s.code_challenge == expect_challenge)
check("state/nonce/session_id present", len(s.state) == 64 and len(s.nonce) == 32 and len(s.session_id) == 32)
# 唯一性
s2 = pkce.create_pkce_session()
check("sessions unique", s.code_verifier != s2.code_verifier and s.state != s2.state)

# authorization URL 参数
url = pkce.build_authorization_url(s)
check("authz url base", url.startswith("https://auth.x.ai/oauth2/authorize?"))
for kv in ("response_type=code", "code_challenge_method=S256", "client_id=b1a00492-073a-47ea-816f-4c329264a828",
           "redirect_uri=http%3A%2F%2F127.0.0.1%3A56121%2Fcallback", "scope=openid+profile+email+offline_access+grok-cli%3Aaccess+api%3Aaccess"):
    check(f"authz url has {kv.split('=')[0]}", kv in url)
check("authz url carries challenge", s.code_challenge in url and s.state in url)

# ---------- ② parse_callback ----------
code = pkce.parse_callback(f"http://127.0.0.1:56121/callback?code=abc123&state={s.state}", s.state)
check("parse_callback normal", code == "abc123")
try:
    pkce.parse_callback("http://127.0.0.1/cb?error=access_denied&error_description=nope", s.state)
    check("parse_callback error rejected", False)
except pkce.XaiPKCEError as e:
    check("parse_callback error rejected", "nope" in str(e))
try:
    pkce.parse_callback(f"http://127.0.0.1/cb?state={s.state}", s.state)
    check("parse_callback missing code rejected", False)
except pkce.XaiPKCEError:
    check("parse_callback missing code rejected", True)
try:
    pkce.parse_callback("http://127.0.0.1/cb?code=x&state=wrong", s.state)
    check("parse_callback bad state rejected", False)
except pkce.XaiPKCEError:
    check("parse_callback bad state rejected", True)
check("parse_callback bare code", pkce.parse_callback("  rawcode99  ", s.state) == "rawcode99")
try:
    pkce.parse_callback("", s.state)
    check("parse_callback empty rejected", False)
except pkce.XaiPKCEError:
    check("parse_callback empty rejected", True)

# ---------- ③ token_to_auth_payload（假 JWT，不联网） ----------
def fake_jwt(payload: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"hdr.{seg}.sig"

tok = {
    "access_token": fake_jwt({"exp": 1893456000, "email": "A@Example.COM"}),
    "refresh_token": "rt_123",
    "id_token": fake_jwt({"sub": "u1"}),
    "token_type": "Bearer",
    "expires_in": 3600,
    "scope": "openid",
}
p = pkce.token_to_auth_payload(tok, email="a@example.com")
check("payload keeps AT/RT/id_token", p["access_token"] == tok["access_token"] and p["refresh_token"] == "rt_123" and p["id_token"].startswith("hdr."))
check("expires_at from JWT exp", p["expires_at"] == "2030-01-01T00:00:00Z")
check("email lowercased", p["email"] == "a@example.com")
check("oidc fields", p["oidc_issuer"] == "https://auth.x.ai" and p["oidc_client_id"] == pkce.CLIENT_ID)

# 无 exp 时回退 expires_in
tok2 = {"access_token": fake_jwt({}), "refresh_token": "rt", "expires_in": 7200}
import time as _t
from datetime import datetime, timezone
before = _t.time()
p2 = pkce.token_to_auth_payload(tok2, email="")
after = _t.time()
raw = p2["expires_at"]
dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
check("expires_in fallback ~now+7200", (before + 7200 - 5) <= dt <= (after + 7200 + 5), f"{raw} -> {dt}")
check("expires_at iso8601 Z format", raw.endswith("Z") and "T" in raw, raw)

# 缺 RT 抛错
try:
    pkce.token_to_auth_payload({"access_token": "x"}, email="")
    check("missing RT raises", False)
except pkce.XaiPKCEError:
    check("missing RT raises", True)

# 坏 JWT 不抛
check("bad jwt -> empty claims", pkce.jwt_claims("not.a.jwt!!") == {} and pkce.jwt_claims("") == {})

# ---------- ④ save/clear verifier（临时目录） ----------
import tempfile
with tempfile.TemporaryDirectory() as td:
    f = Path(td) / "pkce.json"
    pkce.save_pkce_session(s, f)
    saved = json.loads(f.read_text(encoding="utf-8"))
    check("save_pkce_session persists verifier", saved["code_verifier"] == s.code_verifier)
    pkce.clear_pkce_verifier(f)
    cleared = json.loads(f.read_text(encoding="utf-8"))
    check("clear_pkce_verifier wipes secrets", cleared["code_verifier"] == "" and cleared["code_challenge"] == "" and "completed_at" in cleared)

# ---------- ⑤ core.py 纯函数 + 不触发浏览器 ----------
import platforms.grok.core as gcore
check("import grok core", True)
pw1 = gcore._rand_password()
check("rand_password strength", pw1.endswith(",,,aA1") and len(pw1) == 18 and pw1[:12].isalnum(), pw1[:3] + "...")
m = gcore._mask("abcdefghijklmnopqrstuvwxyz")
check("_mask hides token", m == "abcd...<len=26>" and "efghij" not in m)

# ---------- ⑥ xai_browser import（camoufox 仅函数内引用，可无依赖 import） ----------
import platforms.grok.xai_browser as xbr
check("import xai_browser", True)
d = xbr.split_proxy("http://user:pass@1.2.3.4:8080")
check("split_proxy auth split", d.get("server") == "http://1.2.3.4:8080" and d.get("username") == "user" and d.get("password") == "pass" and "bypass" in d, str(d))
check("split_proxy bypass", d.get("bypass") == "127.0.0.1,localhost,::1", str(d.get("bypass")))
check("split_proxy empty", xbr.split_proxy("") == {})
check("browser session defaults", hasattr(xbr.XaiBrowserSession, "register_account") and hasattr(xbr.XaiBrowserSession, "authorize_pkce") and hasattr(xbr.XaiBrowserSession, "login_for_pkce"))

# ---------- ⑦ canonical_from_account 对齐（fake account 对象） ----------
from core.nr_client import canonical_from_account

class FakeAcct:
    platform = "grok"
    email = "t@x.com"
    password = "pw"

    def __init__(self):
        self._extra = {"tokens": {"access_token": "ATXYZ", "refresh_token": "RTXYZ", "id_token": "IDT"}}

    def get_extra(self):
        return self._extra

item = canonical_from_account("grok-cli", FakeAcct())
check("canonical reads extra.tokens", item.get("access_token") == "ATXYZ" and item.get("refresh_token") == "RTXYZ" and item.get("id_token") == "IDT")
check("canonical email/displayName", item.get("email") == "t@x.com" and "grok-cli" in str(item.get("displayName")))

# 生产链路：Account dataclass -> save_account -> AccountModel.get_extra() -> canonical
# Account dataclass 本身没有 get_extra()/get()，canonical 只吃 DB 模型或 dict——
# 模拟 AccountModel：用 Account 的 extra 构造带 get_extra 的行对象
from core.base_platform import Account, AccountStatus
acct = Account(platform="grok", email="e@x.com", password="p", status=AccountStatus.REGISTERED,
               extra={"tokens": {"access_token": "a", "refresh_token": "r", "id_token": "i", "expires_at": "2030-01-01T00:00:00Z"}})

class _Row:
    email = acct.email
    def get_extra(self):
        return acct.extra

item2 = canonical_from_account("grok-cli", _Row())
check("AccountModel-like row -> canonical", item2["access_token"] == "a" and item2["refresh_token"] == "r" and item2["id_token"] == "i")

# 嵌套字段优先、顶层兜底的读取顺序（与 nr_client 实现一致）
class _RowFlat:
    email = "flat@x.com"
    def get_extra(self):
        return {"access_token": "TOPAT", "refresh_token": "TOPRT"}

item3 = canonical_from_account("grok-cli", _RowFlat())
check("canonical top-level fallback", item3["access_token"] == "TOPAT" and item3["refresh_token"] == "TOPRT")

# ---------- ⑧ plugin import（registry 注册链） ----------
import platforms.grok.plugin as gplug
check("import grok plugin", True)
cls = None
from core.registry import get as reg_get
cls = reg_get("grok")
check("grok platform registered", cls is not None and cls.name == "grok")
check("plugin version bumped", cls.version == "2.0.0")

print()
print(f"== {len(passed)} passed, {len(failed)} failed ==")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
