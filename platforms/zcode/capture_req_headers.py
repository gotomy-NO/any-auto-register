"""抓取浏览器对话请求完整细节——on_request 立即写入文件（不依赖保存时机）
输出: _req_capture_full.json（URL全文 + post_data全文 + headers）+ _req_capture_full.txt（可读）
"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

TOKEN = json.load(open(r"E:\AI\openAI\AIClient2API-main\configs\zcode\1787986739492_zcode-auth\1787986739492_zcode-auth.json", encoding="utf-8"))["token"]

OUT_JSON = r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_req_capture_full.json"
captures = []


def on_request(req):
    try:
        if "chat/completions" in req.url:
            item = {
                "url": req.url,
                "method": req.method,
                "post_data": req.post_data or "",
                "headers": req.all_headers(),
            }
            captures.append(item)
            # 立即落盘
            with open(OUT_JSON, "w", encoding="utf-8") as f:
                json.dump(captures, f, ensure_ascii=False, indent=1)
            print(">> 捕获#", len(captures), "URL len=", len(req.url))
            print(">> FULL URL:", req.url)
            print(">> POST len=", len(req.post_data or ""))
    except Exception as e:
        print("on_request err:", str(e)[:150])


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
    if target is not None:
        target.click()
        target.type("hi", delay=150)
        page.wait_for_timeout(800)
        page.keyboard.press("Enter")
        print("已发送 hi——等 45s 抓对话请求")
        time.sleep(45)
    print("最终捕获数:", len(captures))
    if captures:
        c = captures[-1]
        with open(r"E:\AI\openAI\注册机1\any-auto-register-main\platforms\zcode\_req_capture_full.txt", "w", encoding="utf-8") as f:
            f.write("URL:\n" + c["url"] + "\n\nPOST_DATA:\n" + c["post_data"] + "\n\nHEADERS:\n" + json.dumps(c["headers"], indent=1))
        print("可读文件已保存")
    print(">>> 保持 8s")
    time.sleep(8)
print(">>> 结束")
