"""探测 accounts.x.ai/sign-up 注册页结构——ipflygates http 代理（页面大加载慢，超时加长到 90s）
目标：确认能否看到邮箱入口按钮/表单，评估 grok 注册可用性。
"""
import sys

from playwright.sync_api import sync_playwright

PROXY_SERVER = "http://aa_00019-zone-resi-region-DE-spec-4:Aa123456@ep.ipflygates.com:6616"
URL = "https://accounts.x.ai/sign-up"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, proxy={"server": PROXY_SERVER})
    pg = b.new_page()
    try:
        pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
        pg.wait_for_timeout(10000)
        print("URL:", pg.url[:130])
        print("TITLE:", pg.title()[:90])
        btns = pg.eval_on_selector_all(
            "button", "els => els.map(e => e.innerText.trim()).filter(t => t)"
        )
        print("按钮:", btns[:30])
        inputs = pg.eval_on_selector_all(
            "input",
            "els => els.map(e => (e.type || '') + '|' + (e.placeholder || '')).filter(t => t)",
        )
        print("输入框:", inputs[:20])
        body = pg.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("页面文本:", body.replace("\n", " ")[:500])
        # 判定
        low = body.lower()
        if "blocked" in low or "attention required" in low or "cloudflare" in low:
            print("==> 结论: 仍被 Cloudflare 拦截")
            sys.exit(1)
        if any(k in low for k in ("sign up", "email", "google", "continue")):
            print("==> 结论: 注册页正常加载，可见邮箱/继续等入口")
        else:
            print("==> 结论: 页面加载但未见明确注册入口，需人工确认")
    except Exception as e:
        print("访问异常:", str(e)[:250])
        sys.exit(1)
    b.close()
print(">>> 完成")
