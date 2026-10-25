"""抓 chat.z.ai 前端真实对话请求（全量 /api 请求 + 请求体——token 注入登录态）
流程：根域注入 token → 进主界面 → 进入聊天（New Chat）→ 输入发送 → 打印所有 /api 请求（含 body）
"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

captured = []


def on_request(req):
    try:
        url = req.url
        if "/api" in url or "completions" in url or "chat" in url.lower():
            body = req.post_data or ""
            captured.append(
                {
                    "m": req.method,
                    "url": url[:220],
                    "body": body[:400],
                }
            )
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
        print("URL:", page.url[:120])
        body_text = page.evaluate('document.body ? document.body.innerText.slice(0, 250) : ""')
        print("页面:", body_text.replace("\n", " ")[:200])

        # 进入聊天：New Chat 按钮或直接点击主输入区
        new_chat = page.locator("text=New Chat")
        if new_chat.count() > 0:
            new_chat.first.click()
            print("已点 New Chat")
            page.wait_for_timeout(5000)
        else:
            # landing 页输入框（contenteditable / textarea）
            ce = page.locator('[contenteditable="true"]:visible').first
            ta = page.locator("textarea:visible").first
            target = ce if ce.count() > 0 else (ta if ta.count() > 0 else None)
            if target is not None:
                target.click()
                target.type("hi", delay=120)
                print("已输入 hi（输入区）")
                page.keyboard.press("Enter")
                page.wait_for_timeout(20000)

        # 兜底：仍在 landing 就点 "Get started"/"Explore" 等入口
        if "/c/" not in page.url and "hi" not in page.evaluate(
            'document.body ? document.body.innerText.slice(0, 800) : ""'
        ):
            for t in ("Get started", "Explore", "Start", "Open"):
                b = page.locator("button:visible", has_text=t)
                if b.count() > 0:
                    b.first.click()
                    print(f"已点 {t}")
                    page.wait_for_timeout(5000)
                    break
            ce = page.locator('[contenteditable="true"]:visible').first
            ta = page.locator("textarea:visible").first
            target = ce if ce.count() > 0 else (ta if ta.count() > 0 else None)
            if target is not None:
                target.click()
                target.type("hi", delay=120)
                page.keyboard.press("Enter")
                print("再次发送 hi")
                page.wait_for_timeout(20000)

        print("=== 捕获的 /api 请求 ===")
        for c in captured:
            print(json.dumps(c, ensure_ascii=False))
        print("最终 URL:", page.url[:140])
        print(">>> 保持浏览器 60s...")
        time.sleep(60)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
