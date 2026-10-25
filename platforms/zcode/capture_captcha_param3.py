"""注入 fetch 拦截抓 captcha_verify_param（浏览器发消息→前端自动带参数发推理请求）"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    try:
        page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        page.evaluate("(t) => localStorage.setItem('token', t)", TOKEN)
        page.reload(wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(10000)
        # 注入 fetch 拦截（记录所有 POST body 到 window.__captured）
        page.evaluate(
            """() => {
                window.__captured = [];
                const origFetch = window.fetch;
                window.fetch = async (...args) => {
                    try {
                        const url = typeof args[0] === 'string' ? args[0] : (args[0].url || '');
                        const opts = args[1] || {};
                        if (opts.method === 'POST' || String(opts.method).toUpperCase() === 'POST') {
                            window.__captured.push({url: String(url).slice(0, 180), body: String(opts.body || '').slice(0, 1200)});
                        }
                    } catch (e) {}
                    return origFetch(...args);
                };
            }"""
        )
        print("已注入 fetch 拦截")
        # 关弹窗
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
            target.type("just say OK", delay=150)
            page.wait_for_timeout(800)
            page.keyboard.press("Enter")
            print("已发送——等 60s")
            time.sleep(60)
        else:
            nc = page.locator("text=New Chat")
            if nc.count() > 0:
                nc.first.click()
                page.wait_for_timeout(6000)
                print("已点 New Chat——等 40s")
                time.sleep(40)
        captured = page.evaluate("() => window.__captured || []")
        print("=== 捕获 POST ===")
        param = None
        for c in captured:
            print(json.dumps(c, ensure_ascii=False)[:600])
            if "captcha_verify_param" in c.get("body", ""):
                try:
                    param = json.loads(c["body"]).get("captcha_verify_param")
                except Exception:
                    pass
        if param:
            print("\n✅ captcha_verify_param:", str(param)[:200])
            with open(r"E:\AI\openAI\AIClient2API-main\configs\zcode\captcha_param.txt", "w", encoding="utf-8") as f:
                f.write(str(param))
            print("已保存")
        else:
            print("\n未抓到 captcha_verify_param")
        time.sleep(15)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(15)
    print(">>> 结束")
