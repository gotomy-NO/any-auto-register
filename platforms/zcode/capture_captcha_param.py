"""从 chat.z.ai 已登录会话发真实消息，拦截对话请求抓 captcha_verify_param
（前端 enable_captcha 时先调 vN() 取验证参数——抓一次存下来给 A2 直连用）
"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\1787986739492_zcode-auth\1787986739492_zcode-auth.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

captures = []


def on_request(req):
    try:
        if "chat/completions" in req.url or "captcha" in req.url.lower():
            captures.append(
                {"url": req.url[:200], "body": (req.post_data or "")[:800]}
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
        # 关弹窗（× / Explore）
        for sel in ("button[aria-label='Close']", "text=Explore the GLM-5.3 Series"):
            el = page.locator(sel)
            if el.count() > 0:
                try:
                    el.first.click(timeout=3000)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
        # 发消息
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
        if target is not None:
            target.click()
            target.type("hi", delay=150)
            page.wait_for_timeout(800)
            page.keyboard.press("Enter")
            print("已发送 hi——等 60s 抓对话请求（含 captcha_verify_param）")
            time.sleep(60)
        else:
            print("未找到输入框——尝试 New Chat")
            nc = page.locator("text=New Chat")
            if nc.count() > 0:
                nc.first.click()
                page.wait_for_timeout(6000)
        print("=== 捕获的对话/captcha 请求 ===")
        for c in captures:
            print(json.dumps(c, ensure_ascii=False))
        # 提取 captcha_verify_param
        param = None
        for c in captures:
            try:
                body = json.loads(c["body"])
                if body.get("captcha_verify_param"):
                    param = body["captcha_verify_param"]
                    break
            except Exception:
                continue
        if param:
            print("\n✅ captcha_verify_param:", str(param)[:200])
            with open(r"E:\AI\openAI\AIClient2API-main\configs\zcode\captcha_param.txt", "w", encoding="utf-8") as f:
                f.write(param)
            print("已保存到 configs/zcode/captcha_param.txt")
        else:
            print("\n未抓到 captcha_verify_param——打印页面状态:")
            body = page.evaluate('document.body ? document.body.innerText.slice(0, 500) : ""')
            print(body.replace("\n", " ")[:400])
        print(">>> 保持浏览器 30s...")
        time.sleep(30)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
