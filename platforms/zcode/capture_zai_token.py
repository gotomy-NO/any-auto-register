"""捕获 chat.z.ai 会话凭据（登录态 token——补传/中转用）
流程：Camoufox 登录 chat.z.ai（zcode 注册账号）→ 提取 session token（localStorage/cookie——Open WebUI JWT）→ 保存
用法: python capture_zai_token.py
"""
import json
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

EMAIL = "<REDACTED_EMAIL>"
PASSWORD = "REDACTED_PLACEHOLDER"  # zcode 注册设的密码
SAVE_PATH = r"E:\AI\openAI\AIClient2API-main\configs\zcode\zai_session.json"

with Camoufox(
    headless=False,
    proxy={"server": "http://127.0.0.1:7897"},
    exclude_addons=[DefaultAddons.UBO],
) as browser:
    page = browser.new_page()
    try:
        # 登录 chat.z.ai
        page.goto("https://chat.z.ai/auth?response_type=code&client_id=client_P8X5CMWmlaRO9gyO-KSqtg&redirect_uri=https%3A%2F%2Fzcode.z.ai%2Flogin", wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)
        print("登录页 URL:", page.url[:110])
        # 邮箱登录（Continue with Email → 邮箱+密码）
        email_btn = page.locator("button:visible", has_text="Continue with Email")
        if email_btn.count() > 0:
            email_btn.first.click()
            print("已点 Continue with Email")
            time.sleep(3)
        mail_input = page.locator(
            'input[placeholder*="Email"]:visible, input[placeholder*="邮箱"]:visible, input[type="email"]:visible'
        ).first
        pwd_input = page.locator(
            'input[placeholder*="Password"]:visible, input[placeholder*="密码"]:visible, input[type="password"]:visible'
        ).first
        if mail_input.count() > 0 and pwd_input.count() > 0:
            mail_input.fill(EMAIL, force=True)
            pwd_input.fill(PASSWORD, force=True)
            print("已填邮箱/密码——点击验证按钮（请用户手动滑动解锁）")
            # 点 "Click to start verification"（触发验证面板）
            verify_el = page.locator("text=Click to start verification")
            if verify_el.count() == 0:
                verify_el = page.locator("text=点击开始验证")
            if verify_el.count() > 0:
                try:
                    verify_el.first.click(timeout=8000)
                    print("已点击验证按钮——验证面板已触发")
                except Exception as e:
                    print("验证按钮点击失败:", str(e)[:80])
                time.sleep(2.5)
            # 轮询检测验证成功（滑块面板消失/状态变化——用户手动滑动解锁）——最长 150s
            verified = False
            for i in range(50):
                time.sleep(3)
                try:
                    captcha_boxes = page.locator(
                        "[class*='aliyun-captcha']:visible, [class*='captcha']:visible, [class*='slider']:visible"
                    ).count()
                    body = page.evaluate('document.body ? document.body.innerText.slice(0, 300) : ""')
                except Exception:
                    captcha_boxes = 0
                    body = ""
                # 检测验证通过：验证面板消失 或 页面出现"请拖动"字样消失
                if captcha_boxes == 0 and "请拖动" not in body and "slider" not in body.lower():
                    verified = True
                    print(f"等待 {i*3}s: 检测到验证已通过（滑块解锁成功）")
                    break
                if i % 4 == 0:
                    print(f"等待中 {i*3}s（请手动滑动解锁）...")
            # 验证通过——立即点 Sign in（登录）
            if verified:
                signin_btn = page.locator("button:visible", has_text="Sign in")
                if signin_btn.count() == 0:
                    signin_btn = page.locator("button:visible", has_text="登录")
                if signin_btn.count() > 0:
                    signin_btn.first.click()
                    print("验证通过——已立即点击 Sign in（登录）")
                    time.sleep(8)
            else:
                print("等待超时——验证未完成（未检测到解锁成功）")
        print("登录后 URL:", page.url[:130])
        # 探测登录后页面（OAuth 授权页/主界面）
        body = page.evaluate('document.body ? document.body.innerText.slice(0, 500) : ""')
        print("登录后页面:", body.replace("\n", " ")[:350])
        # OAuth 授权页（Authorize/Allow/授权）——点击完成 ZCode 授权
        auth_btn = page.locator("button:visible", has_text="Authorize")
        if auth_btn.count() == 0:
            auth_btn = page.locator("button:visible", has_text="Allow")
        if auth_btn.count() == 0:
            auth_btn = page.locator("button:visible", has_text="授权")
        if auth_btn.count() > 0:
            auth_btn.first.click()
            print("已点 OAuth 授权（Authorize）")
            time.sleep(8)
        print("授权后 URL:", page.url[:140])
        # 登录成功后——点 "Explore the GLM-5.3 Series" 按钮（升级弹窗——用户要求点击）
        explore_btn = page.locator("button:visible", has_text="Explore the GLM-5.3 Series")
        if explore_btn.count() == 0:
            explore_btn = page.locator("text=Explore the GLM-5.3 Series")
        if explore_btn.count() > 0:
            try:
                explore_btn.first.click(timeout=8000)
                print("已点击 Explore the GLM-5.3 Series")
                time.sleep(6)
            except Exception as e:
                print("Explore 按钮点击失败:", str(e)[:80])
        else:
            print("未找到 Explore 按钮（弹窗可能未出现）")
        print("Explore 后 URL:", page.url[:140])
        # 重新提取 token（正式用户 token）
        token = page.evaluate(
            "() => { const t = localStorage.getItem('token') || localStorage.getItem('JWT') || ''; return t.slice(0, 120); }"
        )
        print("localStorage token 片段:", token)
        cookies = page.context.cookies()
        cjwt = [c for c in cookies if any(k in c["name"].lower() for k in ("token", "jwt", "session"))]
        print("token cookies:", [(c["name"], c["value"][:40]) for c in cjwt])
        # 保存完整 token（localStorage）
        full_token = page.evaluate(
            "() => localStorage.getItem('token') || localStorage.getItem('JWT') || ''"
        )
        data = {"email": EMAIL, "token": full_token}
        import os
        os.makedirs(os.path.dirname(SAVE_PATH), exist_ok=True)
        with open(SAVE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("已保存:", SAVE_PATH, "| token 长度:", len(full_token))
        print(">>> 浏览器保持打开中（查看登录态）...")
        time.sleep(60)
    except Exception as e:
        print("异常:", str(e)[:250])
        time.sleep(20)
    print(">>> 结束")
