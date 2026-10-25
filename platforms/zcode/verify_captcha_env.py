"""打码基础环境验证：配置读写持久化 + 手动/打码双模式行为 + 平台扩展错误处理"""
import json
import time

import requests

BASE = "http://127.0.0.1:8000"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail[:120]}")


# 1) 配置持久化：写 5 个新 key → 读回
r = requests.put(f"{BASE}/api/config", json={"data": {
    "zcode_captcha_mode": "api",
    "zcode_captcha_provider": "captcharun",
    "zcode_captcha_type": "Aliyun",
    "captcharun_key": "test-token-xxx",
    "zcode_proxy": "http://127.0.0.1:7897",
}}, timeout=10)
check("PUT /api/config 写入 5 个打码 key", r.status_code == 200 and r.json().get("ok"), r.text[:150])

r = requests.get(f"{BASE}/api/captcha/zcode/config", timeout=10)
cfg = r.json()
check("GET 打码配置 mode=api", cfg.get("mode") == "api", json.dumps(cfg, ensure_ascii=False))
check("GET 打码配置 provider=captcharun", cfg.get("provider") == "captcharun")
check("GET 打码配置 has_key=True（test-token-xxx）", cfg.get("has_key") is True)
check("GET 打码配置 proxy 正确", cfg.get("proxy") == "http://127.0.0.1:7897")

# 2) 配置持久化：改回 manual 并清空 key → 读回
r = requests.put(f"{BASE}/api/config", json={"data": {
    "zcode_captcha_mode": "manual",
    "captcharun_key": "",
}}, timeout=10)
check("PUT 切回 manual + 清 key", r.status_code == 200, r.text[:100])
r = requests.get(f"{BASE}/api/captcha/zcode/config", timeout=10)
cfg = r.json()
check("读回 mode=manual", cfg.get("mode") == "manual", json.dumps(cfg, ensure_ascii=False))
check("读回 has_key=False（已清空）", cfg.get("has_key") is False)

# 3) 打码模式（api）+ 无 key → 明确报错
r = requests.post(f"{BASE}/api/captcha/zcode/solve", json={
    "token": "dummy-token", "mode": "api", "provider": "captcharun",
}, timeout=30)
detail = r.json().get("detail", "") if r.status_code == 500 else ""
check("api 模式无 key → 500 明确报错", r.status_code == 500 and "缺少 API key" in detail, f"HTTP {r.status_code} {detail[:100]}")

# 4) 未知打码平台 → 明确报错
r = requests.post(f"{BASE}/api/captcha/zcode/solve", json={
    "token": "dummy", "mode": "api", "provider": "noplatform",
}, timeout=30)
detail = r.json().get("detail", "") if r.status_code == 500 else ""
check("未知平台 → 500 明确报错", r.status_code == 500 and "未知打码平台" in detail, f"HTTP {r.status_code} {detail[:100]}")

# 5) 缺 token → Pydantic 框架层 422（token 必填字段，框架校验拦截）
r = requests.post(f"{BASE}/api/captcha/zcode/solve", json={"mode": "manual"}, timeout=10)
check("缺 token → 422 框架层必填校验", r.status_code == 422, r.text[:100])

# 6) 手动模式（默认）配置态可被前端读取（前端开关数据源）
check("配置端点字段齐全（前端开关用）", all(k in cfg for k in ("mode", "provider", "captcha_type", "has_key", "proxy")))

# 7) 手动模式真实求解（弹窗，等用户滑动）——单独跑，避免卡住上面的批量验证
print()
print(">>> 下一步单独跑手动模式真实求解（弹窗需人工滑动）")

ok = sum(1 for _, c, _ in results if c)
print(f"\n==== 基础环境验证: {ok}/{len(results)} 通过 ====")
if ok < len(results):
    for n, c, d in results:
        if not c:
            print("  FAIL:", n, d[:100])
    raise SystemExit(1)
