"""抓取 chat.z.ai 真实对话请求（精准版：注入 token → 聊天 UI 发送 → 只抓 chat.z.ai 的 POST/PUT）
同时 dump /api/config 与 localStorage 全部 key
"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

hits = []
STATIC = (".js", ".css", ".png", ".svg", ".gif", ".ttf", ".woff", ".jpg", ".webp")
BLOCK = ("alb.reddit", "sdata.chatglm", "analytics", "t.co", "bing.com", "facebook.net", "google.", "z-cdn", "artifacts-cdn", "z-cdn-media")


def on_request(req):
    try:
        url = req.url
        if any(b in url for b in BLOCK):
            return
        if url.split("?")[0].endswith(STATIC):
            return
        if "chatglm" in url or "z.ai" in url or "bigmodel" in url:
            hits.append({
                "m": req.method,
                "url": url[:250],
                "auth": (req.headers.get("authorization") or "")[:40],
                "body": (req.post_data or "")[:400],
            })
    except Exception:
        pass


with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    page.on("request", on_request)
    try:
        page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        page.evaluate("(t) => localStorage.setItem('token', t)", TOKEN)
        page.reload(wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(10000)
        # dump localStorage keys + /api/config
        keys = page.evaluate("() => Object.keys(localStorage)")
        print("localStorage keys:", keys)
        cfg = page.evaluate("() => fetch('/api/config').then(r=>r.text()).catch(e=>String(e))")
        print("/api/config:", str(cfg)[:600])
        print("URL:", page.url[:120])

        # 进聊天：优先 New Chat 文本，然后找主输入框（contenteditable）
        nc = page.locator("text=New Chat")
        if nc.count() > 0:
            nc.first.click()
            page.wait_for_timeout(5000)
        # 候选输入框（按优先级）
        candidates = [
            page.locator('[contenteditable="true"][role="textbox"]:visible').last,
            page.locator('[contenteditable="true"]:visible').last,
            page.locator("textarea:visible").last,
        ]
        target = None
        for c in candidates:
            try:
                if c.count() > 0:
                    box = c.first.bounding_box()
                    if box and box["height"] > 20:
                        target = c.first
                        break
            except Exception:
                continue
        if target is not None:
            target.click()
            target.type("hi", delay=150)
            page.wait_for_timeout(800)
            page.keyboard.press("Enter")
            print("已发送 hi——等待 45s 抓对话请求")
            time.sleep(45)
        else:
            print("未找到输入框——页面文本:", page.evaluate(
                'document.body ? document.body.innerText.slice(0, 300) : ""'
            ).replace("\n", " ")[:250])
            time.sleep(20)
        print("=== chat.z.ai POST/PUT 请求 ===")
        for h in hits:
            print(json.dumps(h, ensure_ascii=False))
        print("最终 URL:", page.url[:140])
        print(">>> 保持 30s...")
        time.sleep(30)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
