"""验证 A2 重启后 zcode-web 池条目与模型列表（只读）"""
import requests

BASE = "http://127.0.0.1:9000"
H = {"Authorization": "Bearer <REDACTED_API_KEY>", "Content-Type": "application/json"}

r = requests.get(f"{BASE}/v1/models", headers=H, timeout=20)
print("models status:", r.status_code)
data = r.json().get("data", [])
print("模型总数:", len(data))
zc = [m["id"] for m in data if "zcode" in m["id"].lower()]
print("zcode 模型:", zc)

# 尝试 provider-pools 相关只读接口确认池条目
for path in ("/api/provider-pools", "/api/providers/slim"):
    try:
        rr = requests.get(BASE + path, headers=H, timeout=10)
        txt = rr.text[:500]
        print(f"{path}: {rr.status_code} zcode_count={txt.count('zcode')} | {txt[:200]}")
    except Exception as e:
        print(f"{path}: ERR {str(e)[:80]}")
