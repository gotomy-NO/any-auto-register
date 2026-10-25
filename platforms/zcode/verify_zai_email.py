"""点击 Z.ai Verify Email 链接——完成邮箱验证（Camoufox）
流程：登录 2e-mail → 收件箱打开 Z.ai 邮件 → 提取/点击 Verify Email 链接 → 完成验证
用法: python verify_zai_email.py <邮箱名> <密码>
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
        page.goto("https://2e-mail.ccwu.cc", wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        # 登录（中英文兼容）
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
        # 收件箱——找 Z.ai 邮件并打开
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 800) : ""')
        print("收件箱:", body.replace("\n", " ")[:400])
        zai_item = page.locator("text=Verify Your Email, text=Z.ai")
        if zai_item.count() > 0:
            try:
                zai_item.first.click(timeout=6000)
                print("已打开 Z.ai 邮件")
            except Exception:
                pass
            time.sleep(3)
        # 找 Verify Email 链接——主页面 + iframe（邮件内容可能用 iframe 渲染）
        verify_href = None
        for f in [page] + list(page.frames):
            try:
                links = f.eval_on_selector_all(
                    "a",
                    "els => els.map(e => (e.innerText.trim() || '') + '=>' + (e.href || '')).filter(t => t.split('=>')[0].length > 0)",
                )
            except Exception:
                links = []
            print("链接来源:", f.url[:60], "| 链接数:", len(links))
            for l in links:
                if "verify" in l.lower():
                    href = l.split("=>")[-1] if "=>" in l else None
                    if href and href.startswith("http"):
                        verify_href = href
                        print("找到 Verify 链接:", href[:150])
                        break
            if verify_href:
                break
        # 兜底：文本点击 Verify Email（可能是 div/span 按钮）
        if not verify_href:
            verify_el = page.locator("text=Verify Email")
            if verify_el.count() > 0:
                try:
                    verify_el.first.click(timeout=8000)
                    print("已点击 Verify Email 文本（新标签页/跳转）")
                except Exception as e:
                    print("Verify Email 文本点击失败:", str(e)[:80])
        if verify_href and verify_href.startswith("http"):
            print("跳转验证链接:", verify_href[:150])
            page.goto(verify_href, wait_until="domcontentloaded", timeout=30000)
            print("已跳转验证链接——邮箱验证完成！")
            time.sleep(6)
        print("验证后 URL:", page.url[:140])
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("验证后页面:", body2.replace("\n", " ")[:500])
        print(">>> 浏览器保持打开中（查看验证结果）...")
        time.sleep(60)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
