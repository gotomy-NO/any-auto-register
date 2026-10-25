"""ZCode 注册（Camoufox 防检测浏览器）——模拟人工登录
自动：打开 zcode.z.ai → 登录 → 注册 → 填表单（humanize）→ 触发验证 → 模拟人工拖动滑块
用法: python camoufox_zcode.py [网址]  （网址不写死）
"""
import random
import sys
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

URL = sys.argv[1] if len(sys.argv) > 1 else "https://zcode.z.ai"
NAME = "ZtestAbc8"
EMAIL = "<REDACTED_EMAIL>"
PASSWORD = "REDACTED_PLACEHOLDER"
print(">>> 名称:", NAME, "| 邮箱:", EMAIL, "| 密码:", PASSWORD)


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
        jitter = random.uniform(-2.5, 2.5)  # 人类拖动不精确（微抖）
        page.mouse.move(sx + dist * t + jitter, sy + random.uniform(-1.5, 1.5))
        time.sleep(random.uniform(0.01, 0.025))
    # 到达后微调（人类会微调对齐）
    page.mouse.move(sx + dist + random.uniform(-3, 3), sy + random.uniform(-1, 1))
    human_pause(0.1, 0.3)
    page.mouse.up()
    return True


with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],  # 排除默认 ublock addon（其路径无效——跳过 addons 检查）
) as browser:
    page = browser.new_page()
    new_pages = []
    browser.on("page", lambda np: new_pages.append(np))
    try:
        # 直接 goto OAuth 登录页（不先访问 zcode.z.ai——避免导航冲突）
        from urllib.parse import quote

        oauth_url = (
            "https://chat.z.ai/auth?response_type=code&client_id=client_P8X5CMWmlaRO9gyO-KSqtg"
            "&redirect_uri=" + quote("https://zcode.z.ai/login", safe="")
        )
        page.goto(oauth_url, wait_until="domcontentloaded", timeout=45000)
        print("已直接 goto OAuth 登录页")
        time.sleep(4)
        print("当前 URL:", page.url[:110])
        # 登录选项页：先点 Continue with Email（邮箱登录方式）→ 再点 Sign up（注册入口）
        email_btn = page.locator("button:visible", has_text="Continue with Email")
        if email_btn.count() > 0:
            email_btn.first.click()
            print("已点 Continue with Email")
            time.sleep(2.5)
            print("点后 URL:", page.url[:100])
        # 注册入口：Sign up（排查元素结构 + 点击 + 注册模式 URL 兜底）
        signup_info = page.eval_on_selector_all(
            "text=Sign up",
            "els => els.map(e => e.tagName + '|' + (e.href || '') + '|' + (e.className || '').toString().slice(0, 60))",
        )
        print("Sign up 元素:", signup_info)
        target = page.locator("a", has_text="Sign up")
        if target.count() == 0:
            target = page.locator("button:visible", has_text="Sign up")
        if target.count() == 0:
            target = page.locator("text=Sign up").last
        if target.count() > 0:
            href = target.first.get_attribute("href") if target.first.locator("a") else None
            if href and href.startswith("http"):
                print("Sign up 链接 href:", href[:120])
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)
            else:
                # 多方式点击（Camoufox 对 React 按钮点击兼容——click/force/坐标/Enter）
                clicked = False
                try:
                    target.first.click(timeout=5000)
                    clicked = True
                except Exception:
                    pass
                if not clicked:
                    try:
                        target.first.click(force=True, timeout=5000)
                        clicked = True
                    except Exception:
                        pass
                if not clicked:
                    box = target.first.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        clicked = True
                    else:
                        target.first.press("Enter")
                        clicked = True
                print("已点 Sign up（方式:", "click/force/坐标/Enter" if clicked else "失败", ")")
                time.sleep(3)
        # 检查注册表单（名称框）是否出现——没出现则尝试注册模式 URL
        name_input = page.locator(
            'input[placeholder*="名称"]:visible, input[placeholder*="Name"]:visible'
        )
        if name_input.count() == 0:
            print("注册表单未出现——尝试注册模式 URL (mode=signup)")
            reg_url = oauth_url.replace("response_type=code", "mode=signup&response_type=code")
            try:
                page.goto(reg_url, wait_until="domcontentloaded", timeout=30000)
                print("已 goto 注册模式 URL")
                time.sleep(4)
            except Exception as e:
                print("注册模式 goto 异常:", str(e)[:80])
        # 打印当前状态
        print("当前按钮:", page.eval_on_selector_all(
            "button:visible", "els => els.map(e => e.innerText.trim()).filter(t => t)")[:12])
        print("当前输入框:", page.eval_on_selector_all(
            "input:visible", "els => els.map(e => (e.placeholder || '')).filter(t => t)"))
        # 等待注册表单渲染（名称输入框出现——英文 Name）
        try:
            page.locator(
                'input[placeholder*="名称"]:visible, input[placeholder*="Name"]:visible'
            ).first.wait_for(state="visible", timeout=15000)
            print("注册表单已渲染")
        except Exception as e:
            print("注册表单等待超时:", str(e)[:80])
        time.sleep(1.5)
        # 填表单（中英文 placeholder 兼容）
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
            mail_input.first.fill(EMAIL)
            pwd_input.first.fill(PASSWORD)
            print("已填名称/邮箱/密码")
        # 触发验证（点击开始验证——文本定位）
        verify_el = page.locator("text=点击开始验证")
        if verify_el.count() == 0:
            verify_el = page.locator("text=开始验证")
        if verify_el.count() > 0:
            verify_el.first.click(timeout=8000)
            print("已触发人机验证")
            time.sleep(3)
        # 模拟人工拖动滑块（3 次尝试——Camoufox 环境不被检测，行为拟人化应可过）
        for attempt in range(1, 4):
            handle = page.locator("[class*='slider-move'], [role='slider'], [class*='slider'] [class*='move']")
            if handle.count() > 0:
                track = page.locator(
                    "[class*='aliyun-captcha'] [class*='slider'], [class*='captcha'] [class*='slide'], [class*='slider']"
                ).first
                track_box = track.bounding_box() if track.count() > 0 else handle.first.bounding_box()
                # 随机距离（缺口位置不确定——0.45-0.9 滑块条宽）
                dist = (track_box["width"] - 40) * random.uniform(0.45, 0.9) if track_box else 200
                ok = human_drag(page, handle.first, dist)
                if ok:
                    print(f"尝试 {attempt}: 已模拟人工拖动滑块 ({dist:.0f}px)")
                time.sleep(2.8)
                body = page.evaluate('document.body ? document.body.innerText.slice(0, 400) : ""')
                if "请完成安全验证" not in body and "拖动滑块" not in body and "请拖动" not in body:
                    print(f"尝试 {attempt}: 验证通过！")
                    break
                print(f"尝试 {attempt}: 验证未过——尝试刷新重试")
                # 尝试刷新验证码
                refresh_el = page.locator("[class*='refresh'], [class*='reload'], text=刷新")
                if refresh_el.count() > 0:
                    refresh_el.first.click()
                    time.sleep(2)
                else:
                    # 无刷新按钮——重新触发验证
                    verify_el2 = page.locator("text=点击开始验证")
                    if verify_el2.count() > 0:
                        verify_el2.first.click()
                        time.sleep(2.5)
            else:
                print(f"尝试 {attempt}: 未找到滑块")
                break
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 400) : ""')
        print("验证后页面:", body2.replace("\n", " ")[:350])
        # 点创建账号
        create_btn = page.locator("button:visible", has_text="创建账号")
        if create_btn.count() > 0:
            human_pause()
            create_btn.first.click()
            print("已点创建账号")
            time.sleep(5)
        print("最终 URL:", page.url[:140])
        body3 = page.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("最终页面文本:", body3.replace("\n", " ")[:500])
        time.sleep(30)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
