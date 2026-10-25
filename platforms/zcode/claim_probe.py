# -*- coding: utf-8 -*-
"""claim_probe.py — zcode Start Plan 免费额度「真领取」探针（2026-10-05，新建文件）

背景（本日实测，scratch/zcode_claim_probe_20261005.json）：
  - preview 用 app_version=3.14.4 返回 plans=[zcode-v3-start-plan-trust-1005]
    （GLM-5.3-Flash，grant_units=1e8，period=one_time，meter=model_usage）；
    主代理的 3.11.2 参数 plans=[] 是版本参数假象。
  - 激活事件 ×2 code=0；user_id=JWT claim id（676dba4e…）。
  - 剩余两堵墙：①claim 验证码头 X-Aliyun-Captcha-Verify-Param（阿里云 Captcha 2.0
    scene=11xygtvd/prefix=no8xfe/region=sgp，client/configs 实测）；②claim token 类型
    疑问（chat JWT 打 claim/balance 得 401 空体，zac_3 claim_token 优先 zcodejwttoken）。

本探针三段：
  A. API 判别：chat JWT + 假 captcha 打 claim → 3007（验证码前置，token 可用）
     vs 401（token 类型墙）——一发定位根因。
  B. 浏览器收割：camoufox 开 https://zcode.z.ai/__claim（route 劫持本地页，
     Origin/Referer 全真）→ AliyunCaptcha.js SDK scene=11xygtvd → 先无痕
     （startTracelessVerification，zac_3 口径「验证码静默通过」）→ fail 则弹滑块
     用 platforms.zcode.aliyun_slider.solve_slider 自动求解 → 收割 verifyParam。
  C. 页内直领：同页 fetch POST /api/v1/zcode-plan/billing/claim
     body {plan_id} + 头 X-Aliyun-Captcha-Verify-Param(+Region) → 真·领取判定。

纪律：token 只打 prefix8+len；不写台账 DB；不碰其它 platforms/zcode/*.py（aliyun_slider 只 import）。
输出：scratch/zcode_claim_attempt_20261005.json（值打码）。
"""
import base64
import json
import os
import sys
import time
import uuid

PROJ = r"D:\AI\临时\Grok注册机\register-8000"
sys.path.insert(0, PROJ)
os.environ.setdefault("SSL_CERT_FILE", os.path.expandvars(r"%LOCALAPPDATA%\anyauto-cacert.pem"))
os.environ.setdefault("CURL_CA_BUNDLE", os.environ["SSL_CERT_FILE"])

import sqlite3
from curl_cffi import requests as creq

DB = os.path.join(PROJ, "account_manager.db")
OUT = os.path.join(PROJ, "scratch", "zcode_claim_attempt_20261005.json")
SCENE, PREFIX, REGION = "11xygtvd", "no8xfe", "sgp"
APP_VER = "3.14.4"
CLAIM_URL = "https://zcode.z.ai/api/v1/zcode-plan/billing/claim"
FAKE_URL = "https://zcode.z.ai/__claim"

conn = sqlite3.connect(DB)
row = conn.execute("SELECT id, email, token FROM accounts WHERE id=89").fetchone()
conn.close()
JWT, EMAIL = row[2], row[1]
pl = JWT.split(".")[1]
pl += "=" * (-len(pl) % 4)
claims = json.loads(base64.urlsafe_b64decode(pl))
USER_ID = str(claims.get("id") or claims.get("sub") or "")
print(f"account id={row[0]} email={EMAIL} jwt_len={len(JWT)} prefix={JWT[:8]} user_id={USER_ID[:8]}…")

MID = ""
try:
    with open(os.path.expanduser(r"~/.zcode/v2/telemetry-state.json"), encoding="utf-8") as f:
        MID = str(json.load(f).get("deviceMid", ""))
except Exception:
    MID = str(uuid.uuid4())

OUTD = {"zcodejwt": "", "user_id": "", "device_mid": "", "plan_id": PLAN_ID,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": []}

# v2：数据源改为官方 ZCode 客户端解密凭据（billing 全家桶只认 zcodejwttoken）；
# 台账 chat JWT 仅作 A 段对照。两种 token 都备好。
_CREDS = os.path.join(PROJ, "scratch", "zcode_creds_decrypted_20261005.json")
ZCODE_JWT, CLIENT_USER_ID = "", ""
if os.path.exists(_CREDS):
    _d = json.load(open(_CREDS, encoding="utf-8"))
    ZCODE_JWT = _d["zcodejwttoken"]
    CLIENT_USER_ID = str(json.loads(_d["oauth:zai:user_info"]).get("user_id", ""))
    print(f"[creds] zcodejwttoken len={len(ZCODE_JWT)} prefix={ZCODE_JWT[:8]} user_id={CLIENT_USER_ID[:8]}…")
OUTD["zcodejwt"] = (ZCODE_JWT[:8] + f"…len{len(ZCODE_JWT)}") if ZCODE_JWT else "(missing)"
OUTD["user_id"] = (CLIENT_USER_ID[:8] + "…") if CLIENT_USER_ID else USER_ID[:8] + "…"


def mask(v):
    s = str(v)
    return (s[:8] + f"…len{len(s)}") if len(s) > 12 else v


def rec(step, status, body, extra=None):
    e = {"step": step, "status": status, "body": body}
    if extra:
        e.update(extra)
    OUTD["steps"].append(e)
    print(f"[{step}] {status} :: {json.dumps(body, ensure_ascii=False)[:300]}")


def zheaders(tok, extra=None):
    h = {
        "User-Agent": f"ZCode/{APP_VER}", "HTTP-Referer": "https://zcode.z.ai",
        "X-Title": "Z Code@electron", "X-ZCode-App-Version": APP_VER,
        "X-Platform": "windows-x64", "X-Release-Channel": "stable",
        "X-Client-Language": "zh-CN", "X-Client-Timezone": "Asia/Shanghai",
        "X-Os-Category": "windows", "X-Os-Version": "10.0.26100",
        "X-Device-Mid": MID, "x-request-id": str(uuid.uuid4()),
        "Content-Type": "application/json", "Authorization": f"Bearer {tok}",
    }
    if extra:
        h.update(extra)
    return h


# ---------------- A. API 判别：claim + 假 captcha（一发定位 3007 vs 401） ----------------
S = creq.Session(impersonate="chrome", proxies={"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}, timeout=30)
try:
    r = S.post(CLAIM_URL, headers=zheaders(JWT, {"X-Aliyun-Captcha-Verify-Param": "fake_param_for_root_cause"}),
               json={"plan_id": "zcode-v3-start-plan-trust-1005"})
    try:
        body = r.json()
    except Exception:
        body = {"_nonjson": r.text[:160], "_headers": {k: v for k, v in r.headers.items() if k.lower() in ("server", "cf-mitigated", "content-type")}}
    rec("A_claim_fake_captcha_chatjwt", r.status_code, body)
except Exception as e:
    rec("A_claim_fake_captcha_chatjwt", -1, {"_err": str(e)[:160]})

# ---------------- B+C. 浏览器：无痕验证收割 verifyParam → 页内直领 ----------------
PAGE_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><title>c</title></head><body>
<div id="cap"></div>
<button id="btn" style="width:220px;height:44px">verify</button>
<script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
<script>
window.__ready = false; window.__param = null; window.__done = false; window.__fail = null; window.__mode = '';
function boot() {
  if (typeof initAliyunCaptcha !== 'function') { setTimeout(boot, 150); return; }
  initAliyunCaptcha({
    SceneId: '__SCENE__', prefix: '__PREFIX__', mode: 'popup', region: '__REGION__',
    language: 'zh-CN', element: '#cap', button: '#btn', captchaLogoImg: '', showErrorTip: false,
    getInstance: function (inst) {
      window.__inst = inst; window.__ready = true;
      try { (inst.startTracelessVerification || inst.show).call(inst); window.__mode = 'traceless'; }
      catch (e) { console.error('start', e.message); }
    },
    captchaVerifyCallback: function (p) { window.__param = p; window.__done = true; return { captchaResult: true, bizResult: true }; },
    success: function (p) { if (!window.__param) { window.__param = p; window.__done = true; } },
    fail: function (err) { window.__fail = JSON.stringify(err); },
    onError: function (err) { window.__fail = JSON.stringify(err); },
  });
}
boot();
</script></body></html>"""
PAGE_HTML = PAGE_HTML.replace("__SCENE__", SCENE).replace("__PREFIX__", PREFIX).replace("__REGION__", REGION)


def browser_phase():
    from camoufox.addons import DefaultAddons
    from camoufox.sync_api import Camoufox

    SS = os.path.join(PROJ, "scratch", "login_direct_v2_114600", "storage_state.json")
    # v2：headed（用户在场协助滑块/点击）+ geoip 按 7897 出口自动匹配
    with Camoufox(headless=False, proxy={"server": "http://127.0.0.1:7897"}, geoip=True,
                  humanize=True, i_know_what_im_doing=True, exclude_addons=[DefaultAddons.UBO]) as browser:
        page = browser.new_page()
        if os.path.exists(SS):
            try:
                page.context.add_cookies(json.load(open(SS, encoding="utf-8")).get("cookies", []))
                print("[B] 已注入 storage_state cookies（zcode.z.ai acw_tc/cdn_sec_tc）", flush=True)
            except Exception as e:
                print(f"[B] cookie 注入失败: {str(e)[:120]}", flush=True)

        def handle_route(route):
            if route.request.url == FAKE_URL:
                route.fulfill(status=200, content_type="text/html", body=PAGE_HTML)
            else:
                route.continue_(headers={**route.request.headers, "referer": "https://zcode.z.ai/"})

        page.route("**/*", handle_route)
        page.goto(FAKE_URL, wait_until="domcontentloaded", timeout=60000)

        # SDK 加载诊断：jsdom 曾 3 连 F001；这里要确认 alicdn 脚本真的到达
        sdk_err = ""
        for _ in range(90):
            try:
                if page.evaluate("()=>window.__ready===true"):
                    print("[B] SDK 就绪（无痕验证已触发）", flush=True)
                    sdk_err = ""
                    break
                sdk_err = str(page.evaluate("()=>window.__fail||''"))[:120]
            except Exception as e:
                sdk_err = str(e)[:120]
            page.wait_for_timeout(500)
        else:
            try:
                has_sdk = page.evaluate("()=>typeof window.initAliyunCaptcha")
            except Exception:
                has_sdk = "?"
            return None, f"sdk_not_ready(has_sdk={has_sdk},last_fail={sdk_err})", page

        # 等收割：headed + 90s 窗口——无痕通过则零人工；否则用户手动拖滑/程序点按钮
        param = None
        clicked = False
        t0 = time.time()
        while time.time() - t0 < 90:
            try:
                if page.evaluate("()=>window.__done===true"):
                    param = page.evaluate("()=>window.__param")
                    if param:
                        print(f"[B] 收割 verifyParam {mask(param)}（无痕或人工）", flush=True)
                        break
                    break
                log = ",".join(page.evaluate("()=>window.__log||[]"))
                if not clicked and time.time() - t0 > 15 and "gi" not in log:
                    print("[B] getInstance 未触发 → 程序点按钮（用户也可手动点/拖）", flush=True)
                    try:
                        page.click("#btn", timeout=3000)
                    except Exception:
                        pass
                    clicked = True
                fail = page.evaluate("()=>window.__fail")
                if fail:
                    print(f"[B] fail 信号: {str(fail)[:120]}（弹滑块，可人工拖）", flush=True)
                    page.evaluate("()=>{window.__fail=null;}")
            except Exception:
                pass
            time.sleep(1.5)
        if not param:
            return None, "harvest_timeout_90s", page


def inpage_claim(page, param):
    """页内 fetch 直领：Origin= zcode.z.ai 真 origin + 真 TLS/cookie 环境。"""
    js = """async ({url, token, mid, planId, ver, param, region}) => {
      const r = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + token,
          'X-ZCode-App-Version': ver, 'X-Platform': 'windows-x64',
          'X-Device-Mid': mid, 'X-Client-Language': 'zh-CN',
          'X-Aliyun-Captcha-Verify-Param': param,
          'X-Aliyun-Captcha-Verify-Region': region,
        },
        body: JSON.stringify({plan_id: planId}),
      });
      const txt = await r.text();
      return {status: r.status, txt: txt.slice(0, 900)};
    }"""
    return page.evaluate(js, {"url": CLAIM_URL, "token": ZCODE_JWT or JWT, "mid": MID,
                              "planId": "zcode-v3-start-plan-trust-1005", "ver": APP_VER,
                              "param": param, "region": REGION})


def main():
    t0 = time.time()
    try:
        param, how, page = browser_phase()
        OUTD["harvest"] = {"how": how, "param": mask(param) if param else None}
        if param:
            try:
                res = inpage_claim(page, param)
                rec("C_claim_inpage", res.get("status"), {"body": res.get("txt", "")[:600]})
            except Exception as e:
                rec("C_claim_inpage", -1, {"_err": str(e)[:200]})
            # D. 领后复核（balance 是 200 的那个端点，能看出已领套餐）
            try:
                pv = page.evaluate("""async ({token, mid, ver}) => {
                  const h = {'Authorization': 'Bearer ' + token, 'X-Device-Mid': mid, 'X-ZCode-App-Version': ver,
                             'X-Platform': 'windows-x64', 'Content-Type': 'application/json'};
                  const out = {};
                  const r1 = await fetch('/api/v1/zcode-plan/billing/preview?app_version=' + ver + '&platform=windows-x64', {headers: h});
                  out.preview = (await r1.text()).slice(0, 400);
                  const r2 = await fetch('/api/v1/zcode-plan/billing/balance?app_version=' + ver, {headers: h});
                  out.balance = (await r2.text()).slice(0, 700);
                  return out;
                }""", {"token": ZCODE_JWT or JWT, "mid": MID, "ver": APP_VER})
                rec("D_post_claim_preview_balance", 0, pv)
            except Exception as e:
                rec("D_post_claim_preview_balance", -1, {"_err": str(e)[:150]})
        else:
            print(f"[C] 未拿到 verifyParam（{how}），页内直领跳过")
    except Exception as e:
        OUTD["harvest"] = {"how": f"browser_error:{type(e).__name__}", "detail": str(e)[:200]}
        print(f"[B] 浏览器异常: {type(e).__name__}: {str(e)[:200]}")

    OUTD["elapsed_s"] = round(time.time() - t0, 1)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(OUTD, f, ensure_ascii=False, indent=1)
    print("saved:", OUT)
    # 汇总判定
    claim_step = next((s for s in OUTD["steps"] if "claim" in s["step"]), None)
    print("== 判定 ==")
    print("harvest:", OUTD.get("harvest"))
    print("claim:", json.dumps(claim_step, ensure_ascii=False)[:300] if claim_step else "(未执行)")


if __name__ == "__main__":
    main()
