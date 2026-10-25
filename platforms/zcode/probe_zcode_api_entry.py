"""探测 chat.z.ai 侧边栏 ZCode API 官方入口（API 文档/端点/API key 页面）
流程：根域注入 token → 登录态 → 侧边栏点 ZCode API → 抓页面文本/链接/网络请求
"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

TOKEN_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"
with open(TOKEN_PATH, encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

hits = []


def on_response(resp):
    try:
        url = resp.request.url
        if "zcode" in url.lower() or "api.z.ai" in url or "openai" in url.lower():
            hits.append({"url": url[:200], "status": resp.status})
    except Exception:
        pass


with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    page.on("response", on_response)
    try:
        page.goto("https://chat.z.ai/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        page.evaluate("(t) => localStorage.setItem('token', t)", TOKEN)
        page.reload(wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(10000)
        # 关升级弹窗（×）
        for sel in ("text=Explore the GLM-5.3 Series", "button[aria-label='Close']", "[class*='close']:visible"):
            el = page.locator(sel)
            if el.count() > 0:
                try:
                    el.first.click(timeout=3000)
                    print("已点:", sel)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
        # 点侧边栏 ZCode API
        zcode_api = page.locator("text=ZCode API")
        if zcode_api.count() == 0:
            zcode_api = page.locator("text=ZCode")
        print("ZCode API 元素数:", zcode_api.count())
        if zcode_api.count() > 0:
            new_pages = []
            browser.on("page", lambda np: new_pages.append(np))
            try:
                zcode_api.first.click(timeout=8000)
            except Exception as e:
                print("点击异常:", str(e)[:80])
                zcode_api.first.press("Enter")
            page.wait_for_timeout(6000)
            print("点击后 URL:", page.url[:140])
            if new_pages:
                np = new_pages[-1]
                print("新标签页:", np.url[:160])
                np.wait_for_timeout(5000)
                body = np.evaluate('document.body ? document.body.innerText.slice(0, 2500) : ""')
                print("新标签页文本:", body.replace("\n", " ")[:1500])
                links = np.eval_on_selector_all(
                    "a", "els => els.map(e => (e.innerText.trim()||'') + '=>' + (e.href||'')).filter(t => t)"
                )
                print("链接:", [l[:150] for l in links[:20]])
            else:
                body = page.evaluate('document.body ? document.body.innerText.slice(0, 2500) : ""')
                print("当前页文本:", body.replace("\n", " ")[:1500])
        else:
            print("未找到 ZCode API 入口——侧边栏文本:")
            body = page.evaluate('document.body ? document.body.innerText.slice(0, 800) : ""')
            print(body.replace("\n", " ")[:700])
        print("=== zcode/api 相关响应 ===")
        for h in hits[:15]:
            print(h)
        print(">>> 保持浏览器 90s（可查看页面）...")
        time.sleep(90)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(30)
    print(">>> 结束")
