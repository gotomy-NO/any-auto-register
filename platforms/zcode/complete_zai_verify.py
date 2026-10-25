"""登录 2e-mail 邮箱 → 取件 → 点击 Verify Email 链接完成 zcode 邮箱验证（自动）
2e-mail.ccwu.cc 是用户自建的邮箱服务器（可 API 取件——此处先用网页方式）
用法: python complete_zai_verify.py <邮箱名> <密码>
"""
import sys
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

MAIL_NAME = sys.argv[1] if len(sys.argv) > 1 else "ztestgsi4d95"
MAIL_PASS = sys.argv[2] if len(sys.argv) > 2 else "<REDACTED_PASSWORD>"

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    try:
        # 1. 登录 2e-mail（中英文兼容）
        page.goto("https://2e-mail.ccwu.cc", wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        lmail = page.locator(
            'input[placeholder*="邮箱"]:visible, input[placeholder*="Email"]:visible, input[type="email"]:visible, input[type="text"]:visible'
        ).first
        lpwd = page.locator(
            'input[placeholder*="密码"]:visible, input[placeholder*="Password"]:visible, input[type="password"]:visible'
        ).first
        if lmail.count() > 0 and lpwd.count() > 0:
            lmail.fill(MAIL_NAME, force=True)
            lpwd.fill(MAIL_PASS, force=True)
            login_btn = page.locator("button:visible", has_text="Sign in")
            if login_btn.count() == 0:
                login_btn = page.locator("button:visible", has_text="登录")
            if login_btn.count() > 0:
                login_btn.first.click()
                print("已点登录")
                time.sleep(5)
        print("登录后 URL:", page.url[:130])
        # 2. 收件箱——找 Z.ai 邮件标题并点击打开详情
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 800) : ""')
        print("收件箱:", body.replace("\n", " ")[:400])
        # 点击邮件标题（Z.ai Verify Your Email）
        mail_item = page.locator("text=Verify Your Email").first
        if mail_item.count() == 0:
            mail_item = page.locator("text=Z.ai").first
        if mail_item.count() > 0:
            try:
                mail_item.click(timeout=8000)
                print("已点击邮件标题打开详情")
            except Exception as e:
                print("邮件标题点击失败:", str(e)[:80])
            time.sleep(4)
        print("详情后 URL:", page.url[:140])
        # 3. 提取 Verify Email 链接（主页面 + iframe）
        verify_href = None
        for f in [page] + list(page.frames):
            try:
                links = f.eval_on_selector_all(
                    "a",
                    "els => els.map(e => (e.innerText.trim() || '') + '=>' + (e.href || '')).filter(t => t)",
                )
            except Exception:
                links = []
            print("frame:", f.url[:70], "| a 链接数:", len(links))
            for l in links:
                if "verify" in l.lower():
                    href = l.split("=>")[-1]
                    if href and href.startswith("http"):
                        verify_href = href
                        print("找到 Verify 链接:", href[:160])
                        break
            if verify_href:
                break
        # 4. 点击/跳转验证链接
        if verify_href and verify_href.startswith("http"):
            page.goto(verify_href, wait_until="domcontentloaded", timeout=30000)
            print("已跳转 Verify Email 链接——完成邮箱验证！")
            time.sleep(6)
        else:
            # 兜底：页面里点击 Verify Email 文本/按钮
            verify_el = page.locator("text=Verify Email")
            if verify_el.count() > 0:
                try:
                    verify_el.first.click(timeout=8000)
                    print("已点击 Verify Email（文本）")
                    time.sleep(5)
                except Exception as e:
                    print("Verify Email 点击失败:", str(e)[:80])
        print("验证后 URL:", page.url[:150])
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("验证后页面:", body2.replace("\n", " ")[:500])
        print(">>> 浏览器保持打开中（查看验证结果）...")
        time.sleep(90)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
