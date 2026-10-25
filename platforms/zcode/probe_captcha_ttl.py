"""关键实验 v2：用 expect_request 同步等待对话请求，抓 captcha_verify_param + 完整请求
1) 浏览器发消息 → expect_request 同步拿到对话请求（含参数/headers/cookies）
2) 立即用该参数直连 chat.z.ai → 测"参数即时可用"
3) 等 60s 再用同一参数直连 → 测时效
4) 顺带确认浏览器会话是否真的静默过 captcha（还是需要弹滑块）
"""
import base64
import hashlib
import hmac
import json
import time
import uuid

import requests
from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

SIGN_KEY = "key-@@@@)))()((9))-xxxx&&&%%%%%"
FE_VERSION = "prod-fe-1.1.92"
BASE = "https://chat.z.ai"
CRED = r"E:\AI\openAI\AIClient2API-main\configs\zcode\1787986739492_zcode-auth\1787986739492_zcode-auth.json"
TOKEN = json.load(open(CRED, encoding="utf-8"))["token"]


def hmac_hex(key, data):
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def sign(body_str, prompt, ts):
    m = int(ts // 300000)
    v = hmac_hex(SIGN_KEY, str(m))
    p = base64.b64encode(prompt.encode()).decode()
    h = f"{body_str}|{p}|{ts}"
    return hmac_hex(v, h)


def direct_call(captcha_param, label, extra_headers=None, cookies=None):
    prompt = "say hello in one word"
    body = {
        "stream": False,
        "model": "glm-5.3",
        "messages": [{"role": "user", "content": prompt}],
        "signature_prompt": prompt,
        "params": {},
        "extra": {},
        "features": {"image_generation": False},
    }
    if captcha_param:
        body["captcha_verify_param"] = captcha_param
    ts = int(time.time() * 1000)
    body_str = json.dumps(body, sort_keys=True, separators=(",", ":"))
    sig = sign(body_str, prompt, ts)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept-Language": "en-US",
        "X-FE-Version": FE_VERSION,
        "X-Signature": sig,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Origin": "https://chat.z.ai",
        "x-region": "overseas",
        "x-device-id": "uid_test1234567890",
    }
    if extra_headers:
        headers.update(extra_headers)
    url = f"{BASE}/api/v2/chat/completions?timestamp={ts}&requestId={uuid.uuid4()}&version=0.0.1&platform=web&language=en-US&timezone=Asia%2FShanghai&signature_timestamp={ts}"
    r = requests.post(url, headers=headers, json=body, cookies=cookies, timeout=60)
    txt = r.text[:300].replace("\n", " ")
    print(f"[{label}] status={r.status_code} | {txt}")
    return r


captured = {}


def on_request(req):
    """立即记录但不阻塞"""
    try:
        if "chat/completions" in req.url and not captured.get("done"):
            captured["url"] = req.url
            captured["headers"] = req.all_headers()
            captured["post_data"] = req.post_data or ""
            captured["done"] = True
            print(">> on_request 已记录 chat/completions 请求")
    except Exception as e:
        print("on_request err:", str(e)[:120])


with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    context = browser.new_context()
    page = context.new_page()
    page.on("request", on_request)
    page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)
    page.evaluate("(t) => localStorage.setItem('token', t)", TOKEN)
    page.reload(wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(8000)
    for sel in ("button[aria-label='Close']", "text=Explore the GLM-5.3 Series"):
        el = page.locator(sel)
        if el.count() > 0:
            try:
                el.first.click(timeout=3000)
                page.wait_for_timeout(1200)
            except Exception:
                pass
    target = None
    for c in (
        page.locator('[contenteditable="true"][role="textbox"]:visible').last,
        page.locator('[contenteditable="true"]:visible').last,
        page.locator("textarea:visible").last,
    ):
        try:
            if c.count() > 0:
                box = c.first.bounding_box()
                if box and box["height"] > 20:
                    target = c.first
                    break
        except Exception:
            continue
    if target is None:
        print("!! 未找到输入框")
    else:
        # 用 expect_request 同步等待 chat/completions 请求
        with page.expect_request(
            lambda r: "chat/completions" in r.url and "api/v2" in r.url,
            timeout=45000,
        ) as req_info:
            target.click()
            target.type("hi", delay=150)
            page.wait_for_timeout(800)
            page.keyboard.press("Enter")
        req = req_info.value
        print(">> expect_request 拿到请求:", req.url[:120])
        headers = req.all_headers()
        post_data = req.post_data or ""
        try:
            pd = json.loads(post_data)
            cap = pd.get("captcha_verify_param", "")
            print(">> captcha_verify_param len:", len(cap))
            print(">> model:", pd.get("model"))
        except Exception as e:
            cap = ""
            print("post_data 解析失败:", str(e)[:100])
        captured["url"] = req.url
        captured["headers"] = headers
        captured["post_data"] = post_data
        captured["cookie"] = headers.get("cookie", "")
        # 打印关键 headers
        for k in ("x-device-id", "x-region", "x-signature", "user-agent", "accept-language", "origin", "cookie"):
            v = headers.get(k, "")
            print(f"   {k}: {v[:100]}")
        # cookies
        cookies = {c["name"]: c["value"] for c in context.cookies() if "z.ai" in c.get("domain", "")}
        print("   z.ai cookies:", {k: v[:30] for k, v in cookies.items()})

        if cap:
            # 1) 立即直连（带浏览器同款 x-device-id/cookie）
            time.sleep(1)
            direct_call(
                cap,
                "立即使用-带浏览器头",
                extra_headers={"x-device-id": headers.get("x-device-id", ""), "x-region": "overseas"},
                cookies=cookies,
            )
            # 2) 60s 后同一参数
            print("等待 60s 测时效...")
            time.sleep(60)
            direct_call(
                cap,
                "60s后复用",
                extra_headers={"x-device-id": headers.get("x-device-id", ""), "x-region": "overseas"},
                cookies=cookies,
            )
        else:
            print("!! 请求里没有 captcha_verify_param——说明浏览器会话静默过验证")
            # 也测一下：无参数直连（带浏览器头+cookie）
            direct_call("", "无参数-带浏览器头cookie", extra_headers={"x-device-id": headers.get("x-device-id", "")}, cookies=cookies)
    # 存盘
    with open(r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_req_capture_v2.json", "w", encoding="utf-8") as f:
        json.dump(captured, f, ensure_ascii=False, indent=1)
    print(">>> 已保存 _req_capture_v2.json")
    print(">>> 保持 10s")
    time.sleep(10)
print(">>> 结束")
