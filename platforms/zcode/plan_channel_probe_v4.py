# -*- coding: utf-8 -*-
"""Client Request Signing V4 — 纯 Python 移植 + 实链验证（codingPlanSignature 攻坚）。

协议源（TriDefender/zcode-api src/proxy/client-signing.ts，逐字节对照 ZCode 3.12.3
bundle `ClientRequestSigningV4Signer`）：
  握手 HMAC    "get_sign_key\\n{apiKeyId}\\n{ts}\\n{nonce}"
               HMAC-SHA256(key=HKDF(secret,"getSignKey_hmac"), msg)，base64
  业务 Ed25519 "{apiKeyId}\\n{ts}\\n{appVersion}\\n{sessionId}\\n{nonce}"  base64
  PoW seed     sha256("{apiKeyId}\\nzcode\\n{sessionId}\\n{ts}").hex[:32]
  PoW answer   sha256("{seed}\\n{nonce}{counter:08x}") 需 8 个前导 0 bit
  握手响应     {code:200, data:{privateCipher}} = AES-GCM(HKDF(secret,"ed25519_priv")) 解 PKCS8 Ed25519
  头集         X-Client-Ts / X-Client-Version / X-Client-Sig / X-Session-Id /
               X-Client-Nonce / X-App-Id / X-Client-Pow
  gate         GET /api/v1/agent/configs → data.codingPlanSignature.enable
  握手端点     POST /api/paas/c1f3a7e2/v2/client
  免签路径     /api/v1/zcode-plan/anthropic/v1/messages 等（客户端永不签名）

运行（本机约定）：
  cd register-8000
  env -u PYTHONPATH -u HTTP_PROXY -u HTTPS_PROXY \\
    SSL_CERT_FILE="$LOCALAPPDATA/anyauto-cacert.pem" \\
    CURL_CA_BUNDLE="$LOCALAPPDATA/anyauto-cacert.pem" \\
    .venv/Scripts/python.exe -m platforms.zcode.plan_channel_probe_v4
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import os
import secrets
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    load_der_private_key,
    load_pem_private_key,
)

ZCODE_ORIGIN = "https://zcode.z.ai"
GATE_PATH = "/api/v1/agent/configs"
HANDSHAKE_PATH = "/api/paas/c1f3a7e2/v2/client"
APP_ID = "zcode"
POW_BITS = 8
KDF_SALT = b"WD_CLIENT_SIGN_KDF_SALT"
KDF_INFO_HMAC = b"getSignKey_hmac"
KDF_INFO_ED25519 = b"ed25519_priv"


def mask(v: str) -> str:
    return f"{v[:8]}...({len(v)})" if v else "<empty>"


def hkdf(secret: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=KDF_SALT, info=info).derive(secret)


def handshake_sig(secret: str, message: str) -> str:
    mac = hmac_mod.new(hkdf(secret.encode(), KDF_INFO_HMAC), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def decrypt_private_key(api_key_id: str, secret: str, private_cipher: str) -> Ed25519PrivateKey:
    raw = base64.b64decode(private_cipher + "=" * (-len(private_cipher) % 4))
    if len(raw) <= 12 + 16:
        raise ValueError("privateCipher too short")
    iv, ct = raw[:12], raw[12:]
    aes = AESGCM(hkdf(secret.encode(), KDF_INFO_ED25519))
    plain = aes.decrypt(iv, ct, api_key_id.encode())
    text = plain.decode("utf-8").strip()
    # bundle: importKey("pkcs8", base64ToBytes(textDecode(plain))) —— 明文是 base64(PKCS8 DER)
    for candidate in (
        base64.b64decode(text + "=" * (-len(text) % 4)),
        plain,
    ):
        try:
            key = load_der_private_key(candidate, password=None)
        except Exception:  # noqa: BLE001
            try:
                key = load_pem_private_key(candidate, password=None)
            except Exception:  # noqa: BLE001
                continue
        if isinstance(key, Ed25519PrivateKey):
            return key
    raise ValueError(f"privateCipher 明文非 Ed25519 PKCS8（len={len(plain)} head={text[:24]!r}）")


def pow_answer(api_key_id: str, session_id: str, ts: str) -> str:
    seed = hashlib.sha256(f"{api_key_id}\nzcode\n{session_id}\n{ts}".encode()).hexdigest()[:32]
    nonce = secrets.token_hex(12)
    for counter in range(0x100000000):
        cand = f"{nonce}{counter:08x}"
        d = hashlib.sha256(f"{seed}\n{cand}".encode()).digest()
        if d[0] == 0:  # POW_BITS=8
            return cand
    raise RuntimeError("PoW unsolved")


def business_sig(priv: Ed25519PrivateKey, api_key_id: str, ts: str, app_version: str, session_id: str, nonce: str) -> str:
    msg = f"{api_key_id}\n{ts}\n{app_version}\n{session_id}\n{nonce}".encode()
    return base64.b64encode(priv.sign(msg)).decode()


def get_session():
    from curl_cffi import requests as cffi

    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("CURL_CA_BUNDLE")
    return cffi.Session(impersonate="chrome", verify=ca, timeout=25)


def load_combined_key() -> tuple[str, str, str]:
    """本机 ~/.zcode/v2/credentials.json 解出 coding-plan key（id.secret）。"""
    home = os.path.expanduser("~").rstrip("\\/")
    username = os.environ.get("USERNAME", "RDP")
    secret_str = f"zcode-credential-fallback:win32:{home}:{username}".encode()
    key = hashlib.sha256(secret_str).digest()
    cred = json.loads((Path(home) / ".zcode/v2/credentials.json").read_text(encoding="utf-8"))
    ck = next(k for k in cred if k.startswith("account-provider:coding-plan:") and k.endswith(":api-key"))
    combined = cred[ck]
    if combined.startswith("enc:v1:"):
        a, b, c = combined[7:].split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        n, tag, ct = base64.urlsafe_b64decode(pad(a)), base64.urlsafe_b64decode(pad(b)), base64.urlsafe_b64decode(pad(c))
        combined = AESGCM(key).decrypt(n, ct + tag, None).decode()
    dot = combined.find(".")
    return combined, combined[:dot], combined[dot + 1 :]


def identity_headers(app_version: str) -> dict:
    """g6n 平面（3.12.3）：无 X-Device-Mid，X-ZCode-Agent 内联。"""
    return {
        "HTTP-Referer": "https://zcode.z.ai",
        "User-Agent": f"ZCode/{app_version}",
        "X-ZCode-App-Version": app_version,
        "X-Title": "Z Code@electron",
        "X-Release-Channel": "production",
        "X-Client-Language": "zh-CN",
        "X-Client-Timezone": "Asia/Shanghai",
        "X-ZCode-Agent": "glm",
        "X-Platform": "win32-x64",
        "X-Os-Category": "windows",
    }


def main() -> int:
    combined, key_id, key_secret = load_combined_key()
    print(f"[cred] coding-plan key {mask(combined)} id_len={len(key_id)} secret_len={len(key_secret)}")
    if not key_id or not key_secret or key_id.startswith("eyJ"):
        print("[cred] 无 id.secret 分隔符 → V4 不适用该凭据")
        return 1

    AV = "3.12.3"
    session_id = secrets.token_hex(16)
    s = get_session()
    outcome: dict = {"session_id_len": len(session_id)}

    # ---- gate ----
    gr = s.get(ZCODE_ORIGIN + GATE_PATH, headers={**identity_headers(AV), "x-api-key": combined})
    print(f"[gate] {gr.status_code} {gr.text[:200]}")
    try:
        gate = gr.json()
        enable = ((gate.get("data") or {}).get("codingPlanSignature") or {}).get("enable")
    except Exception:  # noqa: BLE001
        enable = None
    print(f"[gate] codingPlanSignature.enable = {enable}")
    outcome["gate_enable"] = enable

    # ---- handshake（origin 候选：api.z.ai 直连为真 origin；zcode.z.ai 仅对照）----
    def try_handshake(origin: str):
        ts = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)
        sig = handshake_sig(key_secret, f"get_sign_key\n{key_id}\n{ts}\n{nonce}")
        hr = s.post(
            origin + HANDSHAKE_PATH,
            headers={"Authorization": combined, "Content-Type": "application/json"},
            json={"apiKey": combined, "nonce": nonce, "sig": sig, "ts": ts},
        )
        print(f"[handshake@{origin.split('//')[1]}] {hr.status_code} {hr.text[:200]}")
        try:
            body = hr.json()
        except Exception:  # noqa: BLE001
            body = {}
        return hr, body

    hr, body = try_handshake("https://api.z.ai")
    if body.get("code") != 200:
        hr2, body2 = try_handshake(ZCODE_ORIGIN)
        if body2.get("code") == 200:
            hr, body = hr2, body2
    cipher = (body.get("data") or {}).get("privateCipher")
    if body.get("code") != 200 or not cipher:
        print("[handshake] 未拿到 privateCipher → 链路断（code=%s）" % body.get("code"))
        outcome["handshake"] = {"status": hr.status_code, "code": body.get("code"), "msg": str(body.get("msg"))[:120]}
        _dump(outcome)
        return 2
    priv = decrypt_private_key(key_id, key_secret, cipher)
    print(f"[handshake] privateCipher 解密成功 → {type(priv).__name__} 就绪")
    outcome["handshake"] = {"status": hr.status_code, "code": body.get("code"), "priv_key": "Ed25519 ok"}

    # ---- signed POST（真 origin：api.z.ai/api/anthropic，x-api-key + SDK UA）----
    url = "https://api.z.ai/api/anthropic/v1/messages"
    req_body = {
        "model": "glm-5.3",
        "max_tokens": 64,
        "stream": False,
        "system": [{"type": "text", "text": "You are a helpful assistant."}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Reply exactly: OK"}]}],
    }

    def sdk_ua() -> str:
        return identity_headers(AV)["User-Agent"] + " ai-sdk/anthropic/3.0.81"

    def signed_headers(ts_v: str) -> dict:
        n = secrets.token_hex(16)
        return {
            **identity_headers(AV),
            "User-Agent": sdk_ua(),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "anthropic-version": "2023-06-01",
            "x-api-key": combined,
            "x-request-id": secrets.token_hex(16),
            "X-Client-Ts": ts_v,
            "X-Client-Version": AV,
            "X-Client-Sig": business_sig(priv, key_id, ts_v, AV, session_id, n),
            "X-Session-Id": session_id,
            "X-Client-Nonce": n,
            "X-App-Id": APP_ID,
            "X-Client-Pow": pow_answer(key_id, session_id, ts_v),
        }

    r = s.post(url, headers=signed_headers(str(int(time.time() * 1000))), json=req_body)
    print(f"[sign-messages] {r.status_code} {r.text[:400]}")
    outcome["signed_messages"] = {"status": r.status_code, "body": r.text[:400]}

    # ---- unsigned 对照（同 URL 同凭据同 UA）----
    r2 = s.post(
        url,
        headers={
            **identity_headers(AV),
            "User-Agent": sdk_ua(),
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": combined,
        },
        json=req_body,
    )
    print(f"[unsigned-ctrl] {r2.status_code} {r2.text[:200]}")
    outcome["unsigned_control"] = {"status": r2.status_code, "body": r2.text[:200]}

    # ---- quota（coding key 直试）----
    q = s.get("https://api.z.ai/api/monitor/usage/quota/limit", headers={"x-api-key": combined})
    print(f"[quota-key] {q.status_code} {q.text[:200]}")
    outcome["quota_with_key"] = {"status": q.status_code, "body": q.text[:200]}

    _dump(outcome)
    return 0


def _dump(outcome: dict) -> None:
    docs = Path(__file__).resolve().parents[2] / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    out = docs / ("zcode计划通道攻坚-v4-%d.json" % int(time.time()))
    out.write_text(json.dumps(outcome, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[dump] {out}")


if __name__ == "__main__":
    sys.exit(main())
