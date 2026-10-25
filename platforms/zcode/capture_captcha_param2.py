"""从 chat.z.ai 会话发消息，捕获 ALL 请求（含 body），找 captcha_verify_param"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

all_reqs = []


def on_request(req):
    try:
        url = req.url
        if "chat.z.ai" in url or "chatglm" in url:
            all_reqs.append(
                {"m": req.method, "url": url[:180], "body": (req.post_data or "")[:900]}
            )
    except Exception as e:
        all_reqs.append({"err": str(e)[:80]})


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
            target.type("say OK", delay=150)
            page.wait_for_timeout(800)
            page.keyboard.press("Enter")
            print("已发送——等 50s 抓请求")
            time.sleep(50)
        print("=== 全部请求 ===")
        for r in all_reqs:
            print(json.dumps(r, ensure_ascii=False))
        # 找 captcha_verify_param
        found = None
        for r in all_reqs:
            b = r.get("body", "")
            if "captcha_verify_param" in b:
                try:
                    found = json.loads(b).get("captcha_verify_param")
                except Exception:
                    found = "RAW:" + b[:300]
                break
        if found:
            print("\n✅ captcha_verify_param:", str(found)[:250])
            with open(r"E:\AI\openAI\AIClient2API-main\configs\zcode\captcha_param.txt", "w", encoding="utf-8") as f:
                f.write(str(found))
            print("已保存")
        else:
            print("\n未找到 captcha_verify_param")
        time.sleep(15)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(15)
    print(">>> 结束")
