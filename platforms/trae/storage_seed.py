# -*- coding: utf-8 -*-
"""storage_seed.py — 从本机 Trae IDE 客户端本地存储中**只读**提取「种」。

背景（docs/trae全网方案调研-20261006.md §1 路径A / §5）：
- Trae 客户端把登录态写在 `globalStorage/storage.json`（VS Code 系，部分版本用
  `state.vscdb` SQLite）。
- 认证值 key = `iCubeAuthInfo://icube.cloudide`：
    * 国际版(SG)：**明文 JSON**（零解密）。
    * 国内版(CN)：`tc` 加密串（base64），算法见 trae-local-api 的 CRACK_TUTORIAL §三。
- **真设备号**在**键名**里：`iCubeAuthInfo://icube-dc:<16位>`（连解密都不用）。
  自编 16 位设备号会被 4017 max_device 拒；真号只能从装过客户端的机器上取。

⚠️ 本模块**只读**：绝不写回/登出用户的客户端登录态；凭据只以掩码返回/日志。
⚠️ `tc` 解密：算法按公开文档实现；未在真实 CN storage.json 上验证（本机无客户端），
   故采用「多候选（SALT 组合 × 拼接/异或）+ JSON 校验」自适应，命中即返回。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
from typing import Optional

# 硬编码盐值（公开逆向，CRACK_TUTORIAL.md §3.4.1）—— 48=0x30 修正见 _SALT_A 末段
_SALT_A = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
                 124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
                 84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78, 8,
                 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
_SALT_B = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95, 96, 81,
                 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239, 160, 224, 59,
                 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126, 186,
                 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])
_SALT_C = bytes([191, 192, 216, 250, 122, 246, 220, 97, 31, 254, 98, 27, 8, 72, 71, 176, 135,
                 99, 96, 18, 127, 101, 203, 104, 211, 102, 191, 125, 37, 72, 150, 156, 51, 229,
                 121, 35, 17, 153, 141, 177, 110, 131, 150, 128, 172, 255, 254, 6, 18, 140, 55,
                 62, 236, 249, 135, 64, 135, 12, 117, 4, 89, 149, 168, 209])
_SALT_D = bytes([246, 204, 26, 232, 232, 70, 129, 109, 223, 146, 169, 242, 23, 241, 105, 145,
                 50, 196, 165, 42, 254, 120, 3, 54, 244, 207, 209, 85, 53, 6, 138, 106, 175,
                 148, 31, 204, 186, 186, 165, 182, 87, 142, 49, 10, 39, 110, 26, 154, 86, 56,
                 173, 125, 18, 64, 198, 225, 99, 99, 83, 82, 191, 134, 76, 170])

# 加密类型 Header
_HDR_AES = bytes([0x74, 0x63, 0x05, 0x10, 0x00, 0x00])          # "tc" 标准
_HDR_AES_PRIVATE = bytes([0x12, 0x39, 0x20, 0x20, 0x02, 0x03])  # 私有

# 认证值 key / 设备号 key 名
AUTH_KEY = "iCubeAuthInfo://icube.cloudide"
_DEVICE_KEY_RE = re.compile(r"iCubeAuthInfo://icube-dc:([0-9a-fA-F]{16})")
_MACHINE_KEY_RE = re.compile(r"iCubeAuthInfo://icube-mc:([0-9a-fA-F]{16,128})")


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _derive_key_iv(random_bytes: bytes, salt: bytes, *, mode: str = "concat"):
    """CRACK_TUTORIAL §3.4.3：finalHash = SHA512( SHA512(random) (‖/⊕) salt )；key=[0:16] iv=[16:32]。"""
    h = hashlib.sha512(random_bytes).digest()
    combined = (h + salt) if mode == "concat" else _xor(h, salt)
    final = hashlib.sha512(combined).digest()
    return final[0:16], final[16:32]


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = dec.update(data) + dec.finalize()
    if out and out[-1] <= 16:  # PKCS7 去填充（宽松）
        out = out[:-out[-1]]
    return out


def decrypt_tc(blob: bytes) -> dict:
    """解密 `tc` 串 → dict。多候选自适应：命中能 json.loads 的候选即返回。

    返回 {"ok":bool, "data":dict|None, "enc":str, "variant":str}
    """
    if len(blob) < 38:
        return {"ok": False, "data": None, "enc": "too_short", "variant": ""}
    header = blob[0:6]
    enc = "AES" if header == _HDR_AES else ("AES_PRIVATE" if header == _HDR_AES_PRIVATE else "UNKNOWN")
    salt_base = _xor(_SALT_C, _SALT_D) if enc == "AES_PRIVATE" else _xor(_SALT_A, _SALT_B)
    random_bytes, enc_data = blob[6:38], blob[38:]
    for mode in ("concat", "xor"):
        try:
            key, iv = _derive_key_iv(random_bytes, salt_base, mode=mode)
            pt = _aes_cbc_decrypt(key, iv, enc_data)
            for cand in (pt, pt.rstrip(b"\x00")):
                txt = cand.decode("utf-8", "strict")
                obj = json.loads(txt)
                return {"ok": True, "data": obj, "enc": enc, "variant": f"{enc}/{mode}"}
        except Exception:  # noqa: BLE001
            continue
    return {"ok": False, "data": None, "enc": enc, "variant": ""}


def _parse_auth_value(raw) -> dict:
    """把 `iCubeAuthInfo://icube.cloudide` 的原始值解析成 dict（明文 JSON 或 tc 加密）。"""
    if isinstance(raw, dict):
        return {"ok": True, "data": raw, "enc": "dict"}
    val = str(raw or "")
    # 可能是被再包一层的 JSON 字符串
    try:
        obj = json.loads(val)
        if isinstance(obj, dict):
            return {"ok": True, "data": obj, "enc": "plaintext-json"}
    except Exception:  # noqa: BLE001
        pass
    try:
        blob = base64.b64decode(val, validate=False)
    except Exception:  # noqa: BLE001
        return {"ok": False, "data": None, "enc": "not-b64"}
    if blob[:2] == b"tc" or blob[:6] in (_HDR_AES, _HDR_AES_PRIVATE):
        return decrypt_tc(blob)
    return {"ok": False, "data": None, "enc": "unknown"}


def extract_seed(data: dict) -> dict:
    """从 storage 键值表里提取 设备号 + token 种。**只读、只读、只读**。"""
    out: dict = {"device_id": "", "machine_id": "", "token": "", "refresh_token": "",
                 "user_id": "", "email": "", "auth_ok": False, "auth_enc": "", "keys_matched": []}
    for k in data.keys():
        m = _DEVICE_KEY_RE.search(str(k))
        if m:
            out["device_id"] = m.group(1)
            out["keys_matched"].append(str(k))
        m2 = _MACHINE_KEY_RE.search(str(k))
        if m2:
            out["machine_id"] = m2.group(1)
    res = _parse_auth_value(data.get(AUTH_KEY))
    out["auth_enc"] = res.get("enc", "")
    if res.get("ok"):
        d = res["data"] or {}
        out["auth_ok"] = True
        # 兼容多种字段名
        def _pick(*names):
            for n in names:
                for kk in d.keys():
                    if kk.lower() == n.lower():
                        v = d[kk]
                        return v if isinstance(v, str) else ("" if v is None else str(v))
            return ""
        out["token"] = _pick("token", "ideToken", "accessToken", "jwt")
        out["refresh_token"] = _pick("refreshToken", "refresh_token")
        out["user_id"] = _pick("userId", "user_id", "uid")
        out["email"] = _pick("email", "account", "mail")
    return out


# --------------------------------------------------------------------------- #
# 定位 / 读取
# --------------------------------------------------------------------------- #
def storage_candidates() -> list[str]:
    """本机可能的 storage.json 路径（含用户覆盖 env）。只读。"""
    out: list[str] = []
    override = os.environ.get("TRAE_STORAGE_JSON")
    if override:
        out.append(override)
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    products = ["Trae", "Trae CN", "TRAE SOLO", "TRAE SOLO CN"]
    for p in products:
        out.append(os.path.join(appdata, p, "User", "globalStorage", "storage.json"))
        out.append(os.path.join(appdata, p, "User", "globalStorage", "state.vscdb"))
    return out


def read_storage_file(path: str) -> dict:
    """读 storage.json 或 state.vscdb(ItemTable) → 键值 dict。"""
    if path.lower().endswith(".vscdb"):
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT key, value FROM ItemTable").fetchall()
        finally:
            db.close()
        data: dict = {}
        for k, v in rows:
            data[str(k)] = v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else v
        return data
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_local_seed(path: Optional[str] = None) -> dict:
    """扫描候选路径，返回第一个能读到 `iCubeAuthInfo://icube.cloudide` 的种。"""
    paths = [path] if path else storage_candidates()
    tried = []
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        tried.append(p)
        try:
            data = read_storage_file(p)
        except Exception as e:  # noqa: BLE001
            tried[-1] = f"{p} (读取失败:{type(e).__name__})"
            continue
        if AUTH_KEY in data or any(_DEVICE_KEY_RE.search(str(k)) for k in data.keys()):
            seed = extract_seed(data)
            seed["source_path"] = p
            seed["tried"] = tried
            return seed
    return {"device_id": "", "machine_id": "", "token": "", "refresh_token": "", "user_id": "",
            "email": "", "auth_ok": False, "auth_enc": "", "keys_matched": [],
            "source_path": "", "tried": tried, "error": "未找到任何 Trae 客户端本地存储"}


def mask_seed(seed: dict) -> dict:
    """日志友好视图：token/refresh 掩码。"""
    def m(v: str) -> str:
        return f"{v[:12]}...<len={len(v)}>" if v else ""
    return {k: (m(v) if k in ("token", "refresh_token") else v) for k, v in seed.items()}


if __name__ == "__main__":
    s = read_local_seed(os.environ.get("TRAE_STORAGE_JSON") or None)
    print(json.dumps(mask_seed(s), ensure_ascii=False, indent=2))
