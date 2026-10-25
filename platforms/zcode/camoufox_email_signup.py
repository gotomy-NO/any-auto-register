"""ZCode 邮箱注册（Camoufox）——直接到邮箱注册页面，填好名称/邮箱/密码——用户图形化验证+创建账号
自动：Camoufox → 直接 goto OAuth 邮箱注册入口(chat.z.ai/auth) → Continue with Email → 注册表单 → 填名称/邮箱/密码
人工：用户在浏览器：图形化验证（拖动滑块）+ 点击创建账号
用法: python camoufox_email_signup.py
"""
import random
import sys
import time
from urllib.parse import quote

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

NAME = "ZtestAbc8"
EMAIL = "<REDACTED_EMAIL>"
PASSWORD = "REDACTED_PLACEHOLDER"
print(">>> 表单值 | 名称:", NAME, "| 邮箱:", EMAIL, "| 密码:", PASSWORD)


def human_pause(lo=0.5, hi=1.5):
    """人类节奏随机停顿"""
    time.sleep(random.uniform(lo, hi))


def human_drag(page, handle, dist):
    """模拟人工拖动滑块（慢速 + 随机微抖 + 人类节奏）"""
    box = handle.bounding_box()
    if not box:
        return False
    sx = box["x"] + box["width"] / 2
    sy = box["y"] + box["height"] / 2
    page.mouse.move(sx, sy)
    human_pause(0.2, 0.5)
    page.mouse.down()
    steps = 30
    for i in range(steps):
        t = (i + 1) / steps
        jitter = random.uniform(-2.5, 2.5)
        page.mouse.move(sx + dist * t + jitter, sy + random.uniform(-1.5, 1.5))
        time.sleep(random.uniform(0.01, 0.025))
    page.mouse.move(sx + dist + random.uniform(-3, 3), sy + random.uniform(-1, 1))
    human_pause(0.1, 0.3)
    page.mouse.up()
    return True

# 直接访问 OAuth 邮箱注册入口（不经过 zcode.z.ai 主页——用户要求直接到邮箱注册页面）
OAUTH_URL = (
    "https://chat.z.ai/auth?response_type=code&client_id=client_P8X5CMWmlaRO9gyO-KSqtg"
    "&redirect_uri=" + quote("https://zcode.z.ai/login", safe="")
)

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    try:
        page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        print("OAuth 页 URL:", page.url[:110])
        # 点 Continue with Email（英文邮箱入口）
        email_btn = page.locator("button:visible", has_text="Continue with Email")
        if email_btn.count() > 0:
            email_btn.first.click()
            print("已点 Continue with Email")
            time.sleep(3)
        # 轮询等待表单出现（最长 25s——每 2s 检查）
        form_ready = False
        for _ in range(12):
            time.sleep(2)
            n_inputs = page.locator("input:visible").count()
            print(f"轮询 {_}: 可见输入框数:", n_inputs)
            # 登录表单页（Email/Password——无名称框）——点 Sign up 切换注册模式
            if n_inputs >= 2 and page.locator(
                'input[placeholder*="名称"]:visible, input[placeholder*="Name"]:visible'
            ).count() == 0:
                signup_link = page.locator("a", has_text="Sign up")
                if signup_link.count() == 0:
                    signup_link = page.locator("text=Sign up").last
                if signup_link.count() > 0:
                    href = signup_link.first.get_attribute("href")
                    if href and href.startswith("http"):
                        page.goto(href, wait_until="domcontentloaded", timeout=30000)
                        print("已跳转 Sign up 链接（注册模式）:", href[:80])
                    else:
                        try:
                            signup_link.first.click(force=True, timeout=5000)
                        except Exception:
                            signup_link.first.press("Enter")
                        print("已点 Sign up（注册模式）")
                    time.sleep(3)
            if page.locator(
                'input[placeholder*="名称"]:visible, input[placeholder*="Name"]:visible'
            ).count() > 0 and page.locator("input:visible").count() >= 3:
                form_ready = True
                break
        print("注册表单出现:", form_ready)
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
        print(">>> 自动注册：触发验证 + 自动拖动滑块 + 创建账号")
        # 触发验证（中英文按钮文本兼容）
        verify_el = page.locator(
            "text=点击开始验证, text=开始验证, text=Click to start verification, text=Start verification"
        )
        if verify_el.count() > 0:
            try:
                verify_el.first.click(timeout=8000)
            except Exception:
                verify_el.first.press("Enter")
            print("已触发人机验证")
            time.sleep(3.5)
        # 自动拖动滑块（多次尝试不同距离——缺口位置未知）
        verified = False
        for attempt in range(1, 8):
            handle = page.locator("[class*='slider-move'], [role='slider'], [class*='slider'] [class*='move']")
            if handle.count() == 0:
                print(f"尝试 {attempt}: 无滑块")
                break
            track = page.locator(
                "[class*='aliyun-captcha'] [class*='slider'], [class*='captcha'] [class*='slide'], [class*='slider']"
            ).first
            track_box = track.bounding_box() if track.count() > 0 else handle.first.bounding_box()
            dist = (track_box["width"] - 40) * random.uniform(0.35, 0.95) if track_box else 200
            ok = human_drag(page, handle.first, dist)
            if ok:
                print(f"尝试 {attempt}: 已拖动滑块 ({dist:.0f}px)")
            time.sleep(3)
            body = page.evaluate('document.body ? document.body.innerText.slice(0, 400) : ""')
            if "请完成安全验证" not in body and "拖动滑块" not in body and "请拖动" not in body:
                verified = True
                print(f"尝试 {attempt}: 验证通过！")
                break
            print(f"尝试 {attempt}: 验证未过——刷新重试")
            refresh_el = page.locator("[class*='refresh'], [class*='reload'], text=刷新")
            if refresh_el.count() > 0:
                refresh_el.first.click()
                time.sleep(2)
        print("验证结果:", "通过" if verified else "未通过")
        if verified:
            # 点创建账号
            create_btn = page.locator("button:visible", has_text="创建账号")
            if create_btn.count() == 0:
                create_btn = page.locator("button:visible", has_text="Create account")
            if create_btn.count() > 0:
                human_pause()
                create_btn.first.click()
                print("已点创建账号")
                time.sleep(6)
        print("最终 URL:", page.url[:140])
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("最终页面文本:", body2.replace("\n", " ")[:500])
        time.sleep(20)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
