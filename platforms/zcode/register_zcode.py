"""ZCode 注册（用 2e-mail 临时邮箱）——用户指导流程
名称: 随机英文 5-10 位 | 邮箱: 2e-mail 临时邮箱 | 密码: 随机 8-14 位大小写特殊符号
验证: 防人机（拖动图块拼图——尝试自动 3 次，不行停下等用户帮助）
用法: python register_zcode.py [网址]  （网址不写死）
"""
import random
import string
import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "https://zcode.z.ai"
PROFILE_DIR = r"E:\AI\zcode_browser_profile"
EMAIL = "<REDACTED_EMAIL>"  # 临时邮箱（已注册可用）
NAME = "".join(random.choice(string.ascii_letters) for _ in range(random.randint(5, 10)))
PASSWORD = "Aa!" + "".join(
    random.choice(string.ascii_letters + string.digits + "!@#$%") for _ in range(random.randint(5, 11))
)
print(">>> 名称:", NAME, "| 邮箱:", EMAIL, "| 密码:", PASSWORD)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        proxy={"server": "http://127.0.0.1:7897"},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    new_pages = []
    ctx.on("page", lambda np: new_pages.append(np))
    try:
        # 1. 打开 zcode.z.ai
        print(">>> 打开", URL)
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        # 2. 点右上角登录（可见按钮）
        login_btn = page.locator("button:visible", has_text="登录")
        if login_btn.count() > 0:
            login_btn.first.click()
            print("已点右上角登录")
            page.wait_for_timeout(4000)
        # 3. 新标签页（chat.z.ai/auth 登录页）
        if new_pages:
            page = new_pages[-1]
            print("已切到新标签页:", page.url[:100])
        # 4. 点"注册"
        reg_btn = page.locator("button:visible", has_text="注册")
        if reg_btn.count() > 0:
            reg_btn.first.click()
            print("已点注册")
            page.wait_for_timeout(4000)
        # 5. 注册页填表单：名称/邮箱/密码
        name_input = page.locator('input[placeholder*="名称"]:visible')
        mail_input = page.locator('input[placeholder*="电子邮箱"]:visible')
        pwd_input = page.locator('input[placeholder*="密码"]:visible')
        print("名称框:", name_input.count(), "| 邮箱框:", mail_input.count(), "| 密码框:", pwd_input.count())
        if name_input.count() > 0 and mail_input.count() > 0 and pwd_input.count() > 0:
            name_input.first.fill(NAME)
            mail_input.first.fill(EMAIL)
            pwd_input.first.fill(PASSWORD)
            print("已填名称/邮箱/密码")
        # 6. 点"点击开始验证"（防人机——文本元素非 button，用文本定位）
        verify_el = page.locator("text=点击开始验证")
        if verify_el.count() == 0:
            verify_el = page.locator("text=开始验证")
        if verify_el.count() == 0:
            verify_el = page.locator("text=验证").first
        if verify_el.count() > 0:
            try:
                verify_el.first.click(timeout=8000)
                print("已点验证（点击开始验证）")
            except Exception as e:
                print("验证点击失败:", str(e)[:80])
                page.mouse.click(500, 400)  # 兜底坐标点击
            page.wait_for_timeout(4000)
            # 探测防人机结构（frames/滑块）
            print("页面 frames:", [(f.name, f.url[:90]) for f in page.frames])
            body = page.evaluate('document.body ? document.body.innerText.slice(0, 500) : ""')
            print("验证后页面文本:", body.replace("\n", " ")[:400])
            # 探测防人机控件详细结构（找滑块把手）
            control_info = page.eval_on_selector_all(
                "[class*='slider'], [class*='captcha'], [class*='verify'], [class*='slide'], [class*='drag'], [class*='block'], [class*='piece'], [class*='img']",
                "els => els.map(e => e.tagName + '.' + (e.className || '').toString().slice(0, 70) + '|' + Math.round(e.getBoundingClientRect().width) + 'x' + Math.round(e.getBoundingClientRect().height) + '@' + Math.round(e.getBoundingClientRect().x) + ',' + Math.round(e.getBoundingClientRect().y))",
            )
            print("防人机控件:", control_info[:20])
            # 尝试自动拖动滑块（3 次）——阿里云滑块（slider-move 把手，正向向右拖）
            for attempt in range(1, 4):
                handle = page.locator("[class*='slider-move'], [class*='slider'] [class*='move']")
                if handle.count() == 0:
                    handle = page.locator("[role='slider'], [class*='drag'], [class*='handle']")
                if handle.count() > 0:
                    box = handle.first.bounding_box()
                    if box:
                        # 滑块条宽度（找轨道/容器）
                        track_el = page.locator(
                            "[class*='aliyun-captcha'] [class*='slider'], [class*='captcha'] [class*='slide'], [class*='slider']"
                        ).first
                        track_box = track_el.bounding_box() if track_el.count() > 0 else box
                        dist = (track_box["width"] - box["width"]) * 0.85 if track_box else 220
                        # 正向拖动（向右——从把手当前位置）
                        sx = box["x"] + box["width"] / 2
                        sy = box["y"] + box["height"] / 2
                        page.mouse.move(sx, sy)
                        page.mouse.down()
                        page.mouse.move(sx + dist, sy, steps=30)
                        page.mouse.up()
                        print(f"尝试 {attempt}: 已拖动滑块 (正向 {dist:.0f}px)")
                        page.wait_for_timeout(2500)
                        body3 = page.evaluate('document.body ? document.body.innerText.slice(0, 400) : ""')
                        if "请完成安全验证" not in body3 and "拖动滑块" not in body3 and "请拖动" not in body3:
                            print(f"尝试 {attempt}: 验证已通过！")
                            break
                        print(f"尝试 {attempt}: 仍在验证页")
                    else:
                        print(f"尝试 {attempt}: 无滑块位置")
                        break
                else:
                    print(f"尝试 {attempt}: 未找到滑块把手")
                    break
            body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 500) : ""')
            print("验证尝试后页面文本:", body2.replace("\n", " ")[:400])
        else:
            print("未找到验证按钮")
        # 7. 点"创建账号"
        create_btn = page.locator("button:visible", has_text="创建账号")
        if create_btn.count() > 0:
            create_btn.first.click()
            print("已点创建账号")
            page.wait_for_timeout(5000)
        print("最终 URL:", page.url[:140])
        body2 = page.evaluate('document.body ? document.body.innerText.slice(0, 600) : ""')
        print("最终页面文本:", body2.replace("\n", " ")[:500])
        time.sleep(30)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
