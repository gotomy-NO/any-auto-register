"""ZCode 注册（Camoufox）——进入注册表单并填好名称/邮箱/密码——用户图形化验证+创建账号
自动：Camoufox → OAuth 登录页 → Continue with Email → 探测/进入注册表单 → 填名称/邮箱/密码
人工：用户在浏览器：图形化验证（拖动滑块）+ 点击创建账号
用法: python camoufox_fill_form.py [网址]  （网址不写死）
"""
import sys
import time
from urllib.parse import quote

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

URL = sys.argv[1] if len(sys.argv) > 1 else "https://zcode.z.ai"
NAME = "ZtestAbc8"
EMAIL = "<REDACTED_EMAIL>"
PASSWORD = "REDACTED_PLACEHOLDER"
print(">>> 表单值 | 名称:", NAME, "| 邮箱:", EMAIL, "| 密码:", PASSWORD)

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    try:
        # 打开 zcode.z.ai（正常入口——用户之前手动从这进入能过验证）
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        print("URL:", page.url[:110])
        # 右上角登录
        login_btn = page.locator("button:visible", has_text="登录")
        if login_btn.count() > 0:
            login_btn.first.click()
            print("已点右上角登录")
            time.sleep(4)
        # OAuth 登录页——点 Continue with Email
        if "chat.z.ai/auth" in page.url:
            email_btn = page.locator("button:visible", has_text="Continue with Email")
            if email_btn.count() > 0:
                email_btn.first.click()
                print("已点 Continue with Email")
                time.sleep(4)
        # 探测当前页面结构（输入框/按钮——确认注册流程）
        inputs = page.eval_on_selector_all(
            "input:visible",
            "els => els.map(e => (e.type || '') + '|' + (e.placeholder || '')).filter(t => t)",
        )
        btns = page.eval_on_selector_all(
            "button:visible", "els => els.map(e => e.innerText.trim()).filter(t => t)"
        )
        print("输入框:", inputs)
        print("按钮:", btns[:15])
        # 填表逻辑：
        # a) 若出现邮箱输入框（英文 Email）——先填邮箱（分步流程）
        mail_only = page.locator(
            'input[placeholder*="Email"]:visible, input[placeholder*="邮箱"]:visible, input[type="email"]:visible'
        )
        name_input = page.locator(
            'input[placeholder*="名称"]:visible, input[placeholder*="Name"]:visible'
        )
        if mail_only.count() > 0 and name_input.count() == 0:
            print("分步流程：先填邮箱")
            mail_only.first.fill(EMAIL)
            # 找下一步按钮（Continue/下一步）
            next_btn = page.locator(
                "button:visible", has_text="Continue"
            )
            if next_btn.count() == 0:
                next_btn = page.locator("button:visible", has_text="下一步")
            if next_btn.count() > 0:
                next_btn.first.click()
                print("已点 Continue（下一步）")
                time.sleep(4)
        # 填完整表单（名称/邮箱/密码）
        name_input = page.locator(
            'input[placeholder*="名称"]:visible, input[placeholder*="Name"]:visible'
        )
        mail_input = page.locator(
            'input[placeholder*="电子邮箱"]:visible, input[placeholder*="邮箱"]:visible, input[placeholder*="Email"]:visible'
        )
        pwd_input = page.locator(
            'input[placeholder*="密码"]:visible, input[placeholder*="Password"]:visible'
        )
        print("名称框:", name_input.count(), "| 邮箱框:", mail_input.count(), "| 密码框:", pwd_input.count())
        if name_input.count() > 0 and mail_input.count() > 0 and pwd_input.count() > 0:
            name_input.first.fill(NAME)
            time.sleep(0.5)
            mail_input.first.fill(EMAIL)
            time.sleep(0.5)
            pwd_input.first.fill(PASSWORD)
            print("已填名称/邮箱/密码——请用户图形化验证+点击创建账号")
        else:
            print("注册表单未完全出现——当前输入框:", page.eval_on_selector_all(
                "input:visible", "els => els.map(e => (e.type || '') + '|' + (e.placeholder || '')).filter(t => t)"))
        print(">>> 浏览器保持打开中——请用户：①图形化验证（拖动滑块）②点击创建账号")
        time.sleep(240)  # 用户操作窗口
        print(">>> 保持结束，最终状态:")
        print("最终 URL:", page.url[:140])
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("最终页面文本:", body2.replace("\n", " ")[:500])
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(30)
    print(">>> 结束")
