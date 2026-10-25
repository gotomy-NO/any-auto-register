"""探测 chat.z.ai settings 子页（Dashboard/Account）找 API key 入口
流程：注入 token → /settings → 逐个点子菜单（Dashboard/Account/General）→ 抓文本+链接
"""
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
        page.goto("https://chat.z.ai/settings", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
        # 逐个点子菜单
        for tab in ("Dashboard", "Account", "General"):
            el = page.locator(f"text={tab}")
            if el.count() > 0:
                try:
                    el.first.click(timeout=5000)
                    page.wait_for_timeout(4000)
                    url = page.url
                    body = page.evaluate('document.body ? document.body.innerText.slice(0, 1200) : ""')
                    links = page.eval_on_selector_all(
                        "a", "els => els.map(e => (e.innerText.trim()||'') + '=>' + (e.href||'')).filter(t => t)"
                    )
                    print(f"\n===== {tab} | {url} =====")
                    print(body.replace("\n", " ")[:800])
                    print("链接:", [l[:130] for l in links[:12]])
                except Exception as e:
                    print(f"{tab} 点击失败:", str(e)[:80])
        print(">>> 保持浏览器 90s（手动查看 settings 页）...")
        time.sleep(90)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(30)
    print(">>> 结束")
