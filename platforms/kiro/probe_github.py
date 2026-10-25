"""探测 Kiro 登录页 GitHub SOCIAL 入口结构（点击 GitHubSign in 看跳转，不填账号）"""
from playwright.sync_api import sync_playwright

KIRO_SIGNIN_URL = "https://app.kiro.dev/signin"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, proxy={"server": "http://127.0.0.1:7897"})
    pg = b.new_page()
    try:
        pg.goto(KIRO_SIGNIN_URL, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(3000)
        print("URL:", pg.url[:100])
        btns = pg.eval_on_selector_all(
            "button", "els => els.map(e => e.innerText.trim()).filter(t => t)"
        )
        print("按钮:", btns[:20])
        # 点击 GitHub 按钮（如存在）
        gh = pg.locator("button", has_text="GitHub")
        if gh.count() > 0:
            gh.first.click()
            pg.wait_for_timeout(4000)
            print("点击 GitHub 后 URL:", pg.url[:150])
            print("点击后 TITLE:", pg.title()[:80])
        else:
            print("未找到 GitHub 按钮")
    except Exception as e:
        print("异常:", str(e)[:200])
    b.close()
