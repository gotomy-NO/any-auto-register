r"""login_direct.py — z.ai 直连登录 + device-flow 铸钥（zac_3 蓝本移植，evaluate-fetch 线）

蓝本：zac_3 src-tauri/src/driver/zaccount-driver.js（1651 行状态机）+ gateway-core/captcha.go mintScript。
本脚本只做「登录→授权→落 token」一遍真实走位，产物全部落 scratch 供诊断。

阶段（每阶段截图 + DOM dump，失败即停在当阶段）：
  S0 环境自检（camoufox 可用、台账账密读取）
  S1 Camoufox 直连（无 proxy 字段 = 家宽住宅出口）开 chat.z.ai + 页内网络探针（hook fetch/XHR）
  S2 登录页探测（zac_3 probeLogin 判据）+ 一次性 reload 重试（SIGNUP_RELOAD_MAX 同款）
  S3 台账 token 注入登录态（localStorage + cookie 双试）
  S4 表单账密登录回退（zac_3 fill()：native setter + input/change/blur 三事件）
  S5 device-flow：cli_init（requests 直连）→ 页内打开 authorize_url → 授权页勾协议+点继续
  S6 poll_until_ready → mint_from_ready → 打码打印 key
  S7 产物落盘：storage_state / 截图 / DOM dump / 网络探针日志

边界：不动 core.py / zcode_oauth.py / glm_mint.py；不杀进程；不落明文 key 到文件。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

REG_ROOT = Path(__file__).resolve().parents[2]  # register-8000/（本文件在 platforms/zcode/ 下）
if str(REG_ROOT) not in sys.path:
    sys.path.insert(0, str(REG_ROOT))

_DB_CANDIDATES = [REG_ROOT / "account_manager.db", REG_ROOT.parent / "register-8000" / "account_manager.db",
                  Path(r"D:\AI\临时\Grok注册机\register-8000\account_manager.db")]
DB_PATH = next((p for p in _DB_CANDIDATES if p.exists() and sqlite3.connect(str(p))
                .execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()[0]),
               REG_ROOT / "account_manager.db")

SCRATCH = REG_ROOT / "scratch" / f"login_direct_{time.strftime('%H%M%S')}"
SCRATCH.mkdir(parents=True, exist_ok=True)
DB_PATH = REG_ROOT / "account_manager.db"
CHAT_BASE = "https://chat.z.ai"
AUTH_URL = CHAT_BASE + "/auth"

LOG_LINES: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


def mask(s: str, head: int = 8) -> str:
    s = s or ""
    return f"{s[:head]}…(len={len(s)})" if len(s) > head + 4 else "***"


# ---------------------------------------------------------------- S0 台账
def load_account() -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id,email,password,token,extra_json FROM accounts "
        "WHERE platform='zcode' AND status='registered' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit("台账无 platform='zcode' registered 账号")
    return dict(row)


# ---------------------------------------------------------------- 页内探针（S1 注入，全程记录 auth 相关请求）
TAP_JS = r"""
(() => {
  if (window.__TAP) return;
  const tap = []; window.__TAP = tap;
  const hit = (u) => /auth|login|signin|session|oauth|captcha/i.test(String(u||''));
  const of = window.fetch;
  window.fetch = function(...a){
    const url = (a[0] && a[0].url) || a[0];
    const p = of.apply(this, a);
    if (hit(url)) p.then(r => { try{
      tap.push({k:'fetch', url:String(url), method:(a[1]&&a[1].method)||'GET',
        body:(a[1]&&a[1].body)?String(a[1].body).slice(0,500):null, status:r.status});
    }catch(e){} }).catch(()=>{});
    return p;
  };
  const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m,u){ this.__u=String(u); this.__m=m; return oo.apply(this,arguments); };
  XMLHttpRequest.prototype.send = function(b){ if(hit(this.__u)) this.addEventListener('load',()=>{
    tap.push({k:'xhr', url:this.__u, method:this.__m, body:b?String(b).slice(0,500):null, status:this.status}); });
    return os.apply(this,arguments); };
})();
"""

# zac_3 判据移植 -----------------------------------------------------------
PROBE_LOGIN_JS = r"""
(() => {
  const vis = e => !!e && e.offsetParent !== null;
  const ins = [...document.querySelectorAll('input, textarea')].filter(vis);
  const email = ins.find(e => e.type === 'email') || ins.find(e => /邮箱|mail/i.test(e.placeholder||''));
  const pwd = ins.find(e => e.type === 'password') || ins.find(e => /密码|password/i.test(e.placeholder||''));
  const btns = [...document.querySelectorAll('button, a, div, span, [role=button]')].filter(vis);
  const norm = s => String(s||'').replace(/\s+/g,'');
  const loginBtn = btns.find(b => b.tagName==='BUTTON' && norm(b.innerText)==='登录');
  return {
    url: location.href, host: location.host, path: location.pathname,
    ready: document.readyState, bodyLen: (document.body&&document.body.innerText||'').length,
    inputs: ins.map(e=>({t:e.type, ph:e.placeholder, id:e.id, name:e.name})).slice(0,10),
    hasEmail: !!email, hasPwd: !!pwd, hasLoginBtn: !!loginBtn,
    emailOk: email ? /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email.value) : false,
    pwdLen: pwd ? (pwd.value||'').length : 0,
  };
})()
"""

FILL_LOGIN_JS = r"""
(args) => {
  const vis = e => !!e && e.offsetParent !== null;
  const ins = [...document.querySelectorAll('input, textarea')].filter(vis);
  const email = ins.find(e => e.type === 'email') || ins.find(e => /邮箱|mail/i.test(e.placeholder||''));
  const pwd = ins.find(e => e.type === 'password') || ins.find(e => /密码|password/i.test(e.placeholder||''));
  const fill = (el, v) => {
    if (!el) return false;
    const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    if (d && d.set) d.set.call(el, String(v)); else el.value = String(v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
    return String(el.value||'') === String(v);
  };
  const eOk = fill(email, args.email), pOk = fill(pwd, args.password);
  return {eOk, pOk};
}
"""

# 授权页（zac_3 onAuthorizePage / confirmAuthorize 移植）
AUTHORIZE_JS = r"""
() => {
  const vis = e => !!e && e.offsetParent !== null;
  const norm = s => String(s||'').replace(/\s+/g,'');
  const body = (document.body&&document.body.innerText||'');
  if (/请输入验证码|请输入手机号|输入您的电子邮箱|输入您的密码|创建账号|Enter your email|Create account/i.test(body)) {
    return {onPage: false, reason: 'login-ish', url: location.href, bodyHead: body.slice(0,200)};
  }
  const btns = [...document.querySelectorAll('button, a, div, span, [role=button]')].filter(vis);
  const btn = btns.find(b => /^(继续|同意|确认授权|授权|确认|Continue|Authorize|Allow)$/.test(norm(b.innerText)));
  const looksDis = b => !b ? true : (b.disabled===true || b.getAttribute('aria-disabled')==='true' ||
    (()=>{try{const cs=getComputedStyle(b);return cs.pointerEvents==='none'||parseFloat(cs.opacity)<0.55;}catch(e){return false;}})());
  const boxes = [...document.querySelectorAll('input[type=checkbox], .el-checkbox__original, .ant-checkbox-input, [role=checkbox]')].filter(vis);
  return {onPage: !!btn, btnLabel: btn?norm(btn.innerText):null, btnDisabled: looksDis(btn),
    boxes: boxes.length, url: location.href, bodyHead: body.slice(0,200)};
}
"""

DO_AUTHORIZE_JS = r"""
() => {
  const vis = e => !!e && e.offsetParent !== null;
  const norm = s => String(s||'').replace(/\s+/g,'');
  const looksDis = b => b.disabled===true || b.getAttribute('aria-disabled')==='true' ||
    (()=>{try{const cs=getComputedStyle(b);return cs.pointerEvents==='none'||parseFloat(cs.opacity)<0.55;}catch(e){return false;}})();
  // 先勾协议（真 checkbox，有状态可验证；zac_3 checkAgreement 原则）
  for (const c of document.querySelectorAll('input[type=checkbox], [role=checkbox]')) {
    if (vis(c) && c.checked !== true && c.getAttribute('aria-checked') !== 'true') c.click();
  }
  const btns = [...document.querySelectorAll('button, a, div, span, [role=button]')].filter(vis);
  const btn = btns.find(b => /^(继续|同意|确认授权|授权|确认|Continue|Authorize|Allow)$/.test(norm(b.innerText)) && b.tagName==='BUTTON')
           || btns.find(b => /^(继续|同意|确认授权|授权|确认|Continue|Authorize|Allow)$/.test(norm(b.innerText)));
  if (!btn) return {clicked:false, why:'no-btn'};
  if (looksDis(btn)) return {clicked:false, why:'disabled'};
  btn.click();
  return {clicked:true, label:norm(btn.innerText)};
}
"""

LOGGED_IN_JS = r"""
() => {
  const body = (document.body&&document.body.innerText||'');
  const storage = {};
  for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i);storage[k]=String(localStorage.getItem(k)).slice(0,80);}
  return {url: location.href, hasLogin: /登录|Log ?in|Sign in/i.test(body.slice(0,3000)),
    userHint: /退出|注销|Sign out|Log out|avatar|个人|settings/i.test(body),
    tokenKeys: Object.keys(storage).filter(k=>/token|user|auth|session/i.test(k)),
    bodyHead: body.slice(0,200)};
}
"""


def dump_artifacts(page, tag: str) -> None:
    try:
        page.screenshot(path=str(SCRATCH / f"{tag}.png"), full_page=False)
    except Exception as e:
        log(f"[art] screenshot {tag} 失败: {e}")
    try:
        html = page.content()
        (SCRATCH / f"{tag}.html").write_text(html[:400000], encoding="utf-8")
    except Exception as e:
        log(f"[art] dom {tag} 失败: {e}")


def read_tap(page) -> list:
    try:
        return page.evaluate("() => window.__TAP || []")
    except Exception:
        return []


def main() -> int:
    from camoufox.sync_api import Camoufox

    acct = load_account()
    email, password = acct["email"], acct["password"]
    db_token = acct["token"] or ""
    log(f"S0 台账账号 id={acct['id']} email={email} token={mask(db_token)}")
    (SCRATCH / "account_masked.json").write_text(
        json.dumps({"id": acct["id"], "email": email, "token_head": db_token[:16], "token_len": len(db_token)},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    from platforms.zcode.glm_mint import RequestsHttp, mask_key
    from platforms.zcode import zcode_oauth as zo

    http = RequestsHttp(proxy="", timeout=25.0)  # 直连 = 家宽住宅出口

    with Camoufox(headless=True, exclude_addons=[] if False else None, os=None) as browser:  # type: ignore[arg-type]
        page = browser.new_page()
        page.add_init_script(TAP_JS)
        log("S1 Camoufox 已启动（headless 直连无代理）")

        # ---- S2 开登录页
        page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        probe = page.evaluate(PROBE_LOGIN_JS)
        log(f"S2 首探: {json.dumps(probe, ensure_ascii=False)[:400]}")
        dump_artifacts(page, "s2_first_probe")

        # 入口页形态（Open WebUI 门面）：账密表单藏在 "Continue with Email" 后面，
        # 必须先点它 —— 此前 4 轮「表单 60s 不出现」即卡在这里没点。
        EMAIL_GATE_JS = r"""
() => {
  const vis = e => !!e && e.offsetParent !== null;
  const norm = s => String(s||'').replace(/\s+/g,'');
  const btns = [...document.querySelectorAll('button, a, div, span, [role=button]')].filter(vis);
  const b = btns.find(x => /continue with email|邮箱登录|邮箱继续|continue with mail/i.test(norm(x.innerText)))
         || btns.find(x => /email/i.test(norm(x.innerText)) && x.tagName==='BUTTON');
  if (!b) return {clicked:false, bodyHead:(document.body&&document.body.innerText||'').slice(0,150)};
  b.click(); return {clicked:true, label:norm(b.innerText)};
}
"""
        if not (probe.get("hasEmail") and probe.get("hasPwd")):
            gate = page.evaluate(EMAIL_GATE_JS)
            log(f"S2 email 入口点击: {json.dumps(gate, ensure_ascii=False)[:200]}")
            page.wait_for_timeout(5000)
            probe = page.evaluate(PROBE_LOGIN_JS)
            log(f"S2 点入口后: {json.dumps(probe, ensure_ascii=False)[:400]}")
            dump_artifacts(page, "s2_after_gate")
        if not (probe.get("hasEmail") and probe.get("hasPwd")):
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(9000)
            gate = page.evaluate(EMAIL_GATE_JS)
            page.wait_for_timeout(5000)
            probe = page.evaluate(PROBE_LOGIN_JS)
            log(f"S2 reload+gate 后: gate={gate} probe={json.dumps(probe, ensure_ascii=False)[:300]}")
            dump_artifacts(page, "s2_after_reload")
        if not (probe.get("hasEmail") and probe.get("hasPwd")):
            log("S2 卡点：登录页账密表单未出现（连 Continue with Email 后也没有）")
            (SCRATCH / "tap.json").write_text(json.dumps(read_tap(page), ensure_ascii=False, indent=1), encoding="utf-8")
            (SCRATCH / "log.txt").write_text("\n".join(LOG_LINES), encoding="utf-8")
            return 2

        # ---- S3 台账 token 注入登录态（判据：访问 chat.z.ai 首页是否被弹回 /auth）
        if db_token:
            page.evaluate(
                """(t) => { localStorage.setItem('token', t); try{localStorage.setItem('access_token',t);}catch(e){}
                     document.cookie = `token=${t}; path=/; domain=.z.ai; secure`; }""",
                db_token,
            )
            page.goto(CHAT_BASE + "/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            state = page.evaluate("""() => ({url: location.href, path: location.pathname,
                bodyHead: (document.body&&document.body.innerText||'').slice(0,200)})""")
            state["userHint"] = "/auth" not in str(state.get("path", "")) and "login" not in str(state.get("url", "")).lower()
            log(f"S3 token 注入后: {json.dumps(state, ensure_ascii=False)[:300]}")
            dump_artifacts(page, "s3_after_token")
        else:
            state = {"userHint": False}

        # ---- S4 表单账密登录回退
        if not state.get("userHint"):
            page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            filled = page.evaluate(FILL_LOGIN_JS, {"email": email, "password": password})
            log(f"S4 表单填充: {filled}")
            dump_artifacts(page, "s4_filled")
            if filled.get("eOk") and filled.get("pOk"):
                clicked = page.evaluate(
                    """() => { const vis=e=>!!e&&e.offsetParent!==null;
                        const norm=s=>String(s||'').replace(/\\s+/g,'');
                        const btns=[...document.querySelectorAll('button, a, div, span, [role=button]')].filter(vis);
                        const b=btns.find(x=>x.tagName==='BUTTON'&&norm(x.innerText)==='登录');
                        if(!b) return {clicked:false,why:'no-btn'};
                        if(b.disabled||b.getAttribute('aria-disabled')==='true') return {clicked:false,why:'disabled'};
                        b.click(); return {clicked:true}; }"""
                )
                log(f"S4 点登录: {clicked}")
                page.wait_for_timeout(8000)
                dump_artifacts(page, "s4_after_submit")
                tap = read_tap(page)
                (SCRATCH / "tap_login.json").write_text(json.dumps(tap, ensure_ascii=False, indent=1), encoding="utf-8")
                for t in tap:
                    log(f"S4 tap: {t.get('method')} {t.get('url')} -> {t.get('status')} body={str(t.get('body'))[:120]}")
                state = page.evaluate(LOGGED_IN_JS)
                log(f"S4 登录后状态: {json.dumps(state, ensure_ascii=False)[:300]}")
            else:
                log("S4 卡点：表单填充失败（选择器/渲染问题）")
        else:
            log("S3 命中：台账 token 即登录态，跳过表单登录")

        # ---- S5 device-flow 授权
        device = zo.cli_init(http)
        log(f"S5 flow_id={device.flow_id[:8]}… authorize_url={device.authorize_url[:110]}")
        page.goto(device.authorize_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        az = page.evaluate(AUTHORIZE_JS)
        log(f"S5 授权页: {json.dumps(az, ensure_ascii=False)[:400]}")
        dump_artifacts(page, "s5_authorize_page")
        if az.get("onPage"):
            res = page.evaluate(DO_AUTHORIZE_JS)
            log(f"S5 点授权: {res}")
            dump_artifacts(page, "s5_after_click")
        elif not az.get("onPage"):
            # 会话没带过去：授权页被弹回登录 → 记录卡点（这就是此前 60s 表单不出现的近亲）
            log("S5 卡点：授权页未出现（会话未生效或被弹回登录）")
            (SCRATCH / "tap_authorize.json").write_text(json.dumps(read_tap(page), ensure_ascii=False, indent=1), encoding="utf-8")
            (SCRATCH / "log.txt").write_text("\n".join(LOG_LINES), encoding="utf-8")
            page.screenshot(path=str(SCRATCH / "s5_stuck.png"))
            return 3

        # ---- S6 poll + mint
        try:
            poll = zo.poll_until_ready(device, http=http, deadline_s=90.0)
        except zo.ZcodeOAuthError as e:
            log(f"S6 poll 失败: {e}")
            (SCRATCH / "log.txt").write_text("\n".join(LOG_LINES), encoding="utf-8")
            return 4
        log(f"S6 poll ready: token={mask_key(poll.token or '')} zai_at={mask(poll.zai_access_token or '')}")
        try:
            mint = zo.mint_from_ready(poll, http=http)
            log(f"S6 MINT OK steps={mint.steps} key={mask_key(mint.combined_key)}")
            (SCRATCH / "result.json").write_text(json.dumps(
                {"ok": True, "email": email, "key_masked": mask_key(mint.combined_key), "steps": mint.steps},
                ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log(f"S6 mint 失败（token 已到手，仅铸钥段挂）: {type(e).__name__}: {str(e)[:200]}")
            (SCRATCH / "result.json").write_text(json.dumps(
                {"ok": "token_only", "email": email, "err": str(e)[:300]}, ensure_ascii=False, indent=1), encoding="utf-8")

        try:
            page.context.storage_state(path=str(SCRATCH / "storage_state.json"))
        except Exception as e:
            log(f"[art] storage_state 失败: {e}")
        (SCRATCH / "log.txt").write_text("\n".join(LOG_LINES), encoding="utf-8")
        return 0


if __name__ == "__main__":
    sys.exit(main())
