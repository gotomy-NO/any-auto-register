"""ZCode 注册页调出（独立浏览器——用户人工通过人机验证 + 点创建账号）
自动：打开 zcode.z.ai → 右上角登录 → 新标签页 → 点注册 → 填名称/邮箱/密码 → 点"点击开始验证"（触发人机验证弹窗）
人工：用户在可见浏览器窗口拖动滑块完成验证 + 点击"创建账号"
用法: python open_zcode_manual.py [网址]  （网址不写死）
"""
import random
import string
import sys
import time

from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync
except Exception:
    stealth_sync = None

URL = sys.argv[1] if len(sys.argv) > 1 else "https://zcode.z.ai"
PROFILE_DIR = r"E:\AI\zcode_firefox_profile"  # Firefox 独立 profile（与 Chromium 的 zcode_browser_profile 分开）
EMAIL = "<REDACTED_EMAIL>"  # 临时邮箱（已注册可用）
NAME = "ZtestAbc8"  # 固定名称（用户手工操作需要知道表单值）
[REDACTED-by-secret-scan]  # 固定密码（用户手工操作需要知道表单值）
print(">>> 名称:", NAME, "| 邮箱:", EMAIL, "| 密码:", PASSWORD)


def apply_stealth(pg):
    """应用 stealth 防检测（隐藏 webdriver/自动化特征——阿里云人机验证）"""
    if stealth_sync:
        try:
            stealth_sync(pg)
            print("已应用 stealth 防检测")
        except Exception as e:
            print("stealth 应用失败:", str(e)[:80])


def human_pause(lo=0.6, hi=1.8):
    """人类节奏随机停顿（降低自动化行为检测）"""
    time.sleep(random.uniform(lo, hi))

with sync_playwright() as p:
    ctx = p.firefox.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        proxy={"server": "http://127.0.0.1:7897"},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    apply_stealth(page)
    new_pages = []
    ctx.on("page", lambda np: new_pages.append(np))
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        human_pause()
        login_btn = page.locator("button:visible", has_text="登录")
        if login_btn.count() > 0:
            login_btn.first.click()
            print("已点右上角登录")
            human_pause(1.5, 3.0)
        if new_pages:
            page = new_pages[-1]
            apply_stealth(page)  # 新标签页也应用 stealth
            print("已切到登录页新标签页")
        human_pause()
        reg_btn = page.locator("button:visible", has_text="注册")
        if reg_btn.count() == 0:
            reg_btn = page.locator("button:visible", has_text="Email")  # 英文界面：Continue with Email
        if reg_btn.count() > 0:
            human_pause()
            reg_btn.first.click()
            print("已点注册/Continue with Email")
            # 等待注册表单渲染（新 profile 首次加载慢——最长 25s）
            try:
                page.locator('input:visible').first.wait_for(state="visible", timeout=25000)
                print("注册表单已渲染")
            except Exception as e:
                print("表单渲染等待超时:", str(e)[:80])
            human_pause(1.0, 2.2)
        # 填表单（名称/邮箱/密码——中英文 placeholder 兼容）
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
            human_pause()
            name_input.first.click()
            name_input.first.fill(NAME)
            human_pause(0.5, 1.2)
            mail_input.first.click()
            mail_input.first.fill(EMAIL)
            human_pause(0.5, 1.2)
            pwd_input.first.click()
            pwd_input.first.fill(PASSWORD)
            print("已填名称/邮箱/密码——剩余（人机验证等）请用户操作")
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 400) : ""')
        print("当前页面:", body.replace("\n", " ")[:350])
        print(">>> 浏览器保持打开中——名称/邮箱/密码已填好——请用户操作剩余步骤")
        time.sleep(240)  # 保持浏览器（用户人工操作窗口）
        print(">>> 保持结束，最终状态:")
        print("最终 URL:", page.url[:140])
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("最终页面文本:", body2.replace("\n", " ")[:500])
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(30)
    print(">>> 结束")
