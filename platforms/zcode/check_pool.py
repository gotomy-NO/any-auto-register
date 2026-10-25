"""检查 Outlook 邮箱池 zcode 调度状态"""
import requests

r = requests.get(
    "http://127.0.0.1:8000/api/mail-imports/snapshot", params={"type": "microsoft"}, timeout=15
)
items = r.json().get("items", [])
print("总数:", len(items))
enabled = [i for i in items if i.get("enabled")]
print("enabled:", len(enabled))

zcode_ok = 0
for i in items:
    tr = i.get("platform_trace") or {}
    z = tr.get("zcode")
    other = {}
    for k, v in tr.items():
        if isinstance(v, dict):
            other[k] = {kk: vv for kk, vv in v.items() if kk in ("excluded", "success", "fails", "in_use")}
    mark = "OK" if (i.get("enabled") and not z) else "--"
    if i.get("enabled") and not z:
        zcode_ok += 1
    print(f"  [{mark}] {i['email'][:34]:36s} enabled={str(i.get('enabled')):5s} zcode={z} other={other}")
print("zcode 可用邮箱数（enabled 且未被 zcode 占用/剔除）:", zcode_ok)
