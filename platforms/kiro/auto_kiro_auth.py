# -*- coding: utf-8 -*-
"""Kiro GitHub Social 授权 v3 全自动：自动填 GitHub → 授权 → 跟随跳转链(GitHub->Cognito->kiro)
直到捕获最终回调 URL(kiro:// 或 auth.desktop.kiro.dev 含 code)，存文件供 A2 manual-callback。"""
import time

from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

AUTH_URL = open(
    r"E:\AI\openAI\注册机1\any-auto-register-main\scripts\kiro_authurl.txt", encoding="utf-8"
).read().strip()
SAVE_PATH = r"E:\AI\openAI\注册机1\any-auto-register-main\scripts\kiro_redirect_url.txt"
# 7897 代理版（v2 曾用此配置成功完成 GitHub 授权走到 cognito；直连版 v4 Problem loading 失败）
PROXY = {"server": "http://127.0.0.1:7897"}
GH_USER = "<REDACTED_EMAIL>"
GH_PASS = "<REDACTED_GH_PASSWORD>"

captured_url = ""
with Camoufox(headless=False, proxy=PROXY, exclude_addons=[DefaultAddons.UBO]) as browser:
    page = browser.new_page()
    try:
        page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        print("opened, url:", page.url[:120])

        submitted = False
        cognito_followed = False
        deadline = time.time() + 600  # 10 分钟
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            try:
                cur = page.url

                # 1) 最终捕获：kiro:// scheme 或 auth.desktop.kiro.dev 带 code（A2 要的最终回调）
                if cur.startswith("kiro://") and "code=" in cur:
                    captured_url = cur
                    print(">>> FINAL KIRO URL:", cur[:400])
                    break
                if "auth.desktop.kiro.dev" in cur and "code=" in cur:
                    captured_url = cur
                    print(">>> FINAL AUTH URL:", cur[:400])
                    break

                # 2) GitHub 登录表单自动填
                if (not submitted) and page.locator("#login_field").count() > 0:
                    pw = page.locator("#password")
                    if pw.count() > 0:
                        page.locator("#login_field").fill(GH_USER)
                        pw.fill(GH_PASS)
                        page.wait_for_timeout(300)
                        page.locator('input[type="submit"], button[type="submit"]').first.click()
                        submitted = True
                        print("github creds submitted, url:", page.url[:120])
                        continue

                # 3) 2FA/设备验证页（自动填常见 OTP 失败则打印等待 30s）
                if page.locator('input[name="otp"], input[name="verification_code"]').count() > 0 and "github.com" in cur:
                    print("OTP required, waiting 30s for manual entry")
                    page.wait_for_timeout(30000)
                    continue

                # 4) Cognito idpresponse（含 GitHub code）：继续跟随跳转链
                if (not cognito_followed) and "amazoncognito.com/oauth2/idpresponse" in cur and "code=" in cur:
                    print("following cognito chain...", cur[:150])
                    page.goto(cur, wait_until="domcontentloaded", timeout=45000)
                    cognito_followed = True
                    continue

                # 5) GitHub OAuth 中转页：自动点 setup page / reload
                body_text = ""
                try:
                    body_text = page.evaluate("document.body ? document.body.innerText : ''")
                except Exception:
                    pass
                if ("redirected to the authorized application" in body_text or "setup page" in body_text) and not cognito_followed:
                    links = page.evaluate(
                        """() => {
                            const as = [...document.querySelectorAll('a')];
                            for (const a of as) {
                                const h = (a.href || '').toLowerCase();
                                if (h.includes('setup') || h.includes('idpresponse') || h.includes('cognito')) return a.href;
                            }
                            return null;
                        }"""
                    )
                    if links:
                        print("clicking setup link:", links[:120])
                        page.goto(links, wait_until="domcontentloaded", timeout=45000)
                        cognito_followed = True
                        continue
                    else:
                        print("setup page no link, reload")
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                        continue

                print("waiting...", cur[:100])
            except Exception as e:
                print("loop err:", str(e)[:80])

        if captured_url:
            with open(SAVE_PATH, "w", encoding="utf-8") as f:
                f.write(captured_url)
            print("saved final redirect to:", SAVE_PATH)
        else:
            print("no final code in 10min, last url:", page.url[:250])
            with open(SAVE_PATH, "w", encoding="utf-8") as f:
                f.write("TIMEOUT:" + page.url)
    except Exception as e:
        print("exception:", str(e)[:250])
