"""ZCode 注册（Camoufox 防检测浏览器）——调出登录页，保持浏览器给用户手动操作
自动：Camoufox 启动 → 打开 OAuth 登录页 → 点 Continue with Email（邮箱入口）
人工：用户在浏览器手动：填表单（值已打印）/人机验证/创建账号
用法: python camoufox_zcode_manual.py [网址]  （网址不写死）
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
    exclude_addons=[DefaultAddons.UBO],  # 排除默认 ublock addon（路径无效——跳过检查）
) as browser:
    page = browser.new_page()
    try:
        # 打开 zcode.z.ai（正常入口）
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        print("URL:", page.url[:110])
        # 右上角登录（如 zcode 首页有登录按钮）
        login_btn = page.locator("button:visible", has_text="登录")
        if login_btn.count() > 0:
            login_btn.first.click()
            print("已点右上角登录")
            time.sleep(4)
        # 若已跳转到 OAuth 登录页——点 Continue with Email（英文邮箱入口）
        if "chat.z.ai/auth" in page.url:
            email_btn = page.locator("button:visible", has_text="Continue with Email")
            if email_btn.count() > 0:
                email_btn.first.click()
                print("已点 Continue with Email")
                time.sleep(3)
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 500) : ""')
        print("当前页面:", body.replace("\n", " ")[:400])
        print(">>> 浏览器保持打开中——请用户手动操作（表单值见上：名称/邮箱/密码）")
        time.sleep(240)  # 用户手动操作窗口
        print(">>> 保持结束，最终状态:")
        print("最终 URL:", page.url[:140])
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("最终页面文本:", body2.replace("\n", " ")[:500])
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(30)
    print(">>> 结束")
