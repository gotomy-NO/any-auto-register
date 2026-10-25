"""注册 2e-mail.ccwu.cc 临时邮箱（Cloud Mail）——用户指导：zcode 注册临时测试用
用法: python register_cloudmail.py [网址]  （网址不写死）
"""
import random
import string
import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "https://2e-mail.ccwu.cc"
PROFILE_DIR = r"E:\AI\zcode_browser_profile"


def rand_str(n, chars=string.ascii_lowercase + string.digits):
    return "".join(random.choice(chars) for _ in range(n))


mail_name = "ztest" + rand_str(7)
password = "Aa!" + rand_str(9, string.ascii_letters + string.digits + "!@#")
print(">>> 计划注册:", mail_name, "@2e-mail.ccwu.cc | 密码:", password)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        proxy={"server": "http://127.0.0.1:7897"},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        # 点击"创建账号"链接/文本（切换显示注册表单——非 button 元素）
        create_link = page.locator("text=创建账号")
        if create_link.count() > 0:
            try:
                create_link.first.click(timeout=5000)
                print("已点击'创建账号'切换注册表单")
            except Exception as e:
                print("创建账号点击失败:", str(e)[:80])
            page.wait_for_timeout(1500)

        # 切换后按 placeholder 定位注册表单（邮箱/密码/确认密码——用 :visible 排除隐藏的登录表单）
        mail_input = page.locator('input[placeholder="邮箱"]:visible')
        pwd_input = page.locator('input[placeholder="密码"]:visible')
        confirm_input = page.locator('input[placeholder="确认密码"]:visible')
        print("可见邮箱:", mail_input.count(), "| 可见        print("可见邮箱:", mail_input.count(), "| 可见[REDACTED-by-secret-scan]| 可见确认:", confirm_input.count())
        if mail_input.count() > 0 and pwd_input.count() > 0 and confirm_input.count() > 0:
            for inp, val in (
                (mail_input.first, mail_name),  # 只填邮箱名（系统自动补 @2e-mail.ccwu.cc 后缀）
                (pwd_input.first, password),
                (confirm_input.first, password),
            ):
                try:
                    inp.click(timeout=5000)
                except Exception:
                    pass
                try:
                    inp.fill(val, force=True)
                except Exception as e:
                    print(f"fill 失败:", str(e)[:80])
                    inp.evaluate(
                        "(el, v) => { const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set; "
                        "setter.call(el, v); el.dispatchEvent(new Event('input', {bubbles: true})); }",
                        val,
                    )
            print("已按 placeholder 填注册表单（邮箱/密码/确认密码）")
            reg_btn = page.locator("button:visible", has_text="创建账号")
            if reg_btn.count() == 0:
                reg_btn = page.locator("button:visible", has_text="注册")
            if reg_btn.count() > 0:
                reg_btn.first.click()
                print("已点击创建账号/注册按钮")
            else:
                print("未找到创建账号按钮，页面按钮:", page.eval_on_selector_all(
                    "button:visible", "els => els.map(e => e.innerText.trim()).filter(t => t)"))
            page.wait_for_timeout(5000)
            print("注册后 URL:", page.url[:140])
            body = page.evaluate('document.body ? document.body.innerText.slice(0, 800) : ""')
            print("提交后完整页面文本:", body.replace("\n", " ")[:600])

            # 登录验证：切到登录模式，用注册的邮箱/密码登录（成功=注册成功）
            try:
                to_login = page.locator("text=去登录")
                if to_login.count() > 0:
                    to_login.first.click(timeout=5000)
                    page.wait_for_timeout(1500)
                    print("已切换到登录模式")
                lmail = page.locator('input[placeholder="邮箱"]:visible')
                lpwd = page.locator('input[placeholder="密码"]:visible')
                if lmail.count() > 0 and lpwd.count() > 0:
                    lmail.first.fill(mail_name, force=True)  # 只填邮箱名（不带后缀）
                    lpwd.first.fill(password, force=True)
                    login_btn = page.locator("button:visible", has_text="登录")
                    if login_btn.count() > 0:
                        login_btn.first.click()
                        print("已点击登录（验证注册）")
                        page.wait_for_timeout(5000)
                        print("登录后 URL:", page.url[:140])
                        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 500) : ""')
                        print("登录后页面文本:", body2.replace("\n", " ")[:400])
                    else:
                        print("未找到登录按钮")
                else:
                    print("未找到登录表单输入框")
            except Exception as e:
                print("登录验证异常:", str(e)[:150])
        else:
            print("输入框不足 6 个（注册表单未渲染）")
        time.sleep(60)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(30)
    print(">>> 结束")
