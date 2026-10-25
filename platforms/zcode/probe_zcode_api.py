"""探测 ZCode API（登录后主界面 ZCode API 入口——API 文档/API key/端点）
流程：Camoufox 登录（协作验证）→ 主界面 → 点 "ZCode API" → 打印 API 页面内容
"""
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

EMAIL = "<REDACTED_EMAIL>"
[REDACTED-by-secret-scan]
OAUTH_URL = (
    "https://chat.z.ai/auth?response_type=code&client_id=client_P8X5CMWmlaRO9gyO-KSqtg"
    "&redirect_uri=https%3A%2F%2Fzcode.z.ai%2Flogin"
)

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    try:
        page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)
        email_btn = page.locator("button:visible", has_text="Continue with Email")
        if email_btn.count() > 0:
            email_btn.first.click()
            time.sleep(3)
        mail_input = page.locator(
            'input[placeholder*="Email"]:visible, input[type="email"]:visible'
        ).first
        pwd_input = page.locator(
            'input[placeholder*="Password"]:visible, input[type="password"]:visible'
        ).first
        if mail_input.count() > 0 and pwd_input.count() > 0:
            mail_input.fill(EMAIL, force=True)
            pwd_input.fill(PASSWORD, force=True)
            verify_el = page.locator("text=Click to start verification")
            if verify_el.count() > 0:
                verify_el.first.click(timeout=8000)
                print("已点验证——请用户手动滑动解锁")
                time.sleep(2.5)
            for i in range(50):
                time.sleep(3)
                try:
                    boxes = page.locator(
                        "[class*='aliyun-captcha']:visible, [class*='captcha']:visible, [class*='slider']:visible"
                    ).count()
                except Exception:
                    boxes = 0
                if boxes == 0:
                    print(f"验证通过（{i*3}s）——点 Sign in")
                    break
            signin_btn = page.locator("button:visible", has_text="Sign in")
            if signin_btn.count() > 0:
                signin_btn.first.click()
                time.sleep(8)
        print("登录后 URL:", page.url[:140])
        # 点 ZCode API 入口
        zcode_api = page.locator("text=ZCode API")
        if zcode_api.count() > 0:
            try:
                zcode_api.first.click(timeout=8000)
                print("已点击 ZCode API")
                time.sleep(5)
            except Exception as e:
                print("ZCode API 点击失败:", str(e)[:80])
        else:
            print("未找到 ZCode API 入口")
        print("点击后 URL:", page.url[:160])
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 1500) : ""')
        print("API 页面内容:", body.replace("\n", " ")[:1100])
        # 页面上找 API 端点/文档链接
        links = page.eval_on_selector_all(
            "a", "els => els.map(e => (e.innerText.trim() || '') + '=>' + (e.href || '')).filter(t => t)"
        )
        api_links = [l for l in links if any(k in l.lower() for k in ("api", "key", "doc", "openai"))]
        print("API 相关链接:", [l[:130] for l in api_links[:10]])
        print(">>> 浏览器保持打开中（查看 API 页面）...")
        time.sleep(60)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
