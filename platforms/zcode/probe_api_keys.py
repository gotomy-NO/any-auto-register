"""探测 chat.z.ai 账号设置/API Keys 入口（z.ai 官方 OpenAI 兼容 API）
流程：注入 token → 点 profile(ZtestAbc8) → 列出菜单项 → 找 API Keys/Settings → 进入抓取
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
        page.reload(wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(10000)
        # 关弹窗
        close = page.locator("button[aria-label='Close']")
        if close.count() > 0:
            try:
                close.first.click(timeout=3000)
                page.wait_for_timeout(1000)
            except Exception:
                pass
        # 点 profile（用户名 ZtestAbc8 或头像）
        profile = page.locator("text=ZtestAbc8")
        print("profile 元素数:", profile.count())
        if profile.count() > 0:
            try:
                profile.first.click(timeout=8000)
            except Exception as e:
                print("profile 点击异常:", str(e)[:80])
            page.wait_for_timeout(3000)
            # 列出弹出的菜单项
            menu = page.evaluate(
                """() => {
                    const out = [];
                    for (const e of document.querySelectorAll('div,li,a,button')) {
                        const t = (e.innerText || '').trim();
                        if (t && t.length < 40 && (
                            /api key|api key|settings|setting|account|logout|log out|profile|dashboard|subscription|billing|developer/i.test(t))) {
                            const r = e.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0 && r.top < window.innerHeight) {
                                out.push(t);
                            }
                        }
                    }
                    return [...new Set(out)].slice(0, 30);
                }"""
            )
            print("菜单项:", menu)
        # 常见 API key 页面直接尝试
        for u in ("https://chat.z.ai/settings", "https://chat.z.ai/settings/api-keys", "https://z.ai/dashboard"):
            page.goto(u, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            body = page.evaluate('document.body ? document.body.innerText.slice(0, 500) : ""')
            print(f"\n{u}:")
            print("  ", body.replace("\n", " ")[:300])
        print(">>> 保持浏览器 90s...")
        time.sleep(90)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(30)
    print(">>> 结束")
