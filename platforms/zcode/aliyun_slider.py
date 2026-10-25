"""阿里云滑块（Captcha 2.0 拼图）自动求解——z.ai 注册/登录页同款。

方案来源（AI算力/投喂区 学习仓库，2026-10-03 调研）：
- qoder-autoreg（arbilaksmana，阿里系 Qoder 同款滑块实战）：缺口检测=纯浏览器内
  取图 + 三策略投票（灰度 NCC / 边缘 NCC / 背景竖边缘对），拖拽=读
  #aliyunCaptcha-puzzle 的 style.left 实时闭环修正——本模块移植该思路到
  Python（numpy FFT 相关 + PIL 解码），零打码平台依赖。
- aliyun-captcha-auto-slide（c99180013-sys）：拟人轨迹（ease-out 三段缓动 +
  微抖 + 松手前停顿 + Y 轴自然抖动，不过冲）。

判定纪律（假阳性改严）：
- 「弹窗关闭」不算通过；必须满足严格 UI 信号（弹窗关闭 且 拼图区销毁/成功态），
  最终以登录后捕获「非 guest 且 email 匹配」的 token 为唯一真验收（core.py）。
"""
import base64
import math
import random
import time

import numpy as np

# ---------------- 页面内取图（JS） ----------------

_PX_JS = """
async () => {
  const bgImg = document.querySelector('#aliyunCaptcha-img');
  const pzImg = document.querySelector('#aliyunCaptcha-puzzle');
  const slider = document.querySelector('#aliyunCaptcha-sliding-slider');
  if (!bgImg || !pzImg || !slider) return null;
  async function px(im) {
    const res = await fetch(im.src, { signal: AbortSignal.timeout(10000) });
    if (!res.ok) throw new Error('fetch ' + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const tmp = new Image();
    await new Promise((r, j) => { tmp.onload = r; tmp.onerror = j; tmp.src = url; });
    const c = document.createElement('canvas');
    c.width = tmp.naturalWidth; c.height = tmp.naturalHeight;
    const ctx = c.getContext('2d');
    ctx.drawImage(tmp, 0, 0);
    const png = c.toDataURL('image/png');
    URL.revokeObjectURL(url);
    return { w: c.width, h: c.height, png };
  }
  const bg = await px(bgImg);
  const pz = await px(pzImg);
  const b = bgImg.getBoundingClientRect();
  const s = slider.getBoundingClientRect();
  const p = pzImg.getBoundingClientRect();
  return {
    bg_png: bg.png, bg_w: bg.w, bg_h: bg.h,
    pz_png: pz.png, pz_w: pz.w, pz_h: pz.h,
    bg_display_w: b.width,
    slider_x: s.x, slider_y: s.y, slider_w: s.width, slider_h: s.height,
    puzzle_left: parseFloat(pzImg.style.left) || 0,
    puzzle_box_x: p.x,
  };
}
"""


def _grab_images(page) -> dict:
    raw = page.evaluate(_PX_JS)
    if not raw:
        return None
    from PIL import Image
    import io

    def _dec(b64png):
        img = Image.open(io.BytesIO(base64.b64decode(b64png.split(",", 1)[1])))
        return np.asarray(img.convert("RGBA"), dtype=np.float64)

    raw["bg"] = _dec(raw["bg_png"])
    raw["pz"] = _dec(raw["pz_png"])
    return raw


# ---------------- 缺口检测（numpy） ----------------

def _to_gray(arr: np.ndarray) -> np.ndarray:
    return arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114


def _blur3(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="edge")
    return (
        p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:]
        + 2 * p[1:-1, :-2] + 4 * p[1:-1, 1:-1] + 2 * p[1:-1, 2:]
        + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]
    ) / 16.0


def _sobel_x(a: np.ndarray) -> np.ndarray:
    g = np.gradient(a)[1]  # 水平梯度（列方向）
    return g


def _corr2(src: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """有效区互相关：out[oy, ox] = sum kernel[i,j]*src[oy+i, ox+j]（FFT 加速）"""
    H, W = src.shape
    h, w = kernel.shape
    fh, fw = H + h - 1, W + w - 1
    F = np.fft.rfft2(src, s=(fh, fw))
    K = np.fft.rfft2(kernel[::-1, ::-1], s=(fh, fw))
    full = np.fft.irfft2(F * K, s=(fh, fw))
    return full[h - 1 : H, w - 1 : W]


def _masked_ncc_scan(bg_map: np.ndarray, tpl: np.ndarray, mask: np.ndarray,
                     y_lo: int, y_hi: int, x_lo: int = 15) -> tuple:
    """mask 加权 NCC 全扫描，返回 (best_x, best_y, best_score)。

    数学：sum(ts)、sum(s) 用 FFT 相关一次算全图；模板侧统计是常量。
    """
    kh, kw = tpl.shape
    m = mask.astype(np.float64)
    n = float(m.sum())
    if n < 50:
        return -1, -1, -2.0
    tm = (tpl * m).sum() / n
    tv = (tpl * tpl * m).sum() / n - tm * tm
    if tv < 1e-6:
        return -1, -1, -2.0
    s1 = _corr2(bg_map, m)                       # sum(s)
    s2 = _corr2(bg_map * bg_map, m)              # sum(s^2)
    st = _corr2(bg_map, tpl * m)                 # sum(t*s)
    smean = s1 / n
    svar = s2 / n - smean * smean
    svar = np.maximum(svar, 1e-6)
    cross = st - smean * (tpl * m).sum()         # sum((t-tm)(s-sm)) 展开式的分子（未除 n）
    ncc = (cross / n) / (math.sqrt(tv) * np.sqrt(svar))
    H, W = bg_map.shape
    y_lo = max(0, y_lo)
    y_hi = min(y_hi, H - kh)
    best = (-1, -1, -2.0)
    for oy in range(y_lo, y_hi + 1):
        row = ncc[oy, x_lo : W - kw + 1]
        if row.size == 0:
            continue
        ox = int(np.argmax(row)) + x_lo
        sc = float(row[ox - x_lo])
        if sc > best[2]:
            best = (ox, oy, sc)
    return best


def _gap_edges_from_bg(gray: np.ndarray, expected_w: int) -> tuple:
    """背景竖边缘对检测（缺口左右边界），返回 (left_x, right_x, score 0~1) 或 None"""
    sx = np.abs(_sobel_x(gray))
    h, w = sx.shape
    y0, y1 = int(h * 0.1), int(h * 0.9)
    col = sx[y0:y1].sum(axis=0)
    k = np.ones(5) / 5.0
    col = np.convolve(col, k, mode="same")
    thr = col.max() * 0.3
    margin = 15
    peaks = [
        x
        for x in range(margin, w - margin)
        if col[x] > thr
        and col[x] >= col[x - 1] and col[x] >= col[x + 1]
        and col[x] >= col[x - 2] and col[x] >= col[x + 2]
    ]
    tol = int(expected_w * 0.4)
    best = None
    for i in range(len(peaks)):
        for j in range(i + 1, len(peaks)):
            dist = peaks[j] - peaks[i]
            if expected_w - tol <= dist <= expected_w + tol:
                # 归一化到 0~1 量级，便于与其他策略同台投票
                score = float(col[peaks[i]] + col[peaks[j]]) / max(2.0 * float(col.max()), 1.0)
                if best is None or score > best[2]:
                    best = (peaks[i], peaks[j], score)
    return best


def _vote_gap(candidates, agree_px: int = 12, single_min_score: float = 0.70, log=None):
    """多策略共识投票——返回 (x, score, method) 或 None（无共识，不拖）。

    纪律（2026-10-04 12:04 注册页八连败实证）：三个候选互相打架时
    （如 gray:57 / edge:210 / gap-edges:128），旧口径赌最高分必错——
    八轮里无共识的 x 拖了全败。宁可不拖（刷新换图），也不拿错 x 去撞风控。
    - 任两策略 x 差 < agree_px → 取均值（score=均值）；
    - 否则仅当最佳单策略 score ≥ single_min_score 才单独采信
      （0.70 阈值依据：真网通过轮 gray 单独 0.71@186 拖之即过；
      而八连败轮的单策略分最高只有 0.56）。
    """
    best = None
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a, b = candidates[i], candidates[j]
            if abs(a["x"] - b["x"]) < agree_px:
                avg = (a["x"] + b["x"]) / 2
                sc = (a["score"] + b["score"]) / 2
                if best is None or sc > best[1]:
                    best = (int(round(avg)), sc, f"{a['method']}+{b['method']}")
    if best is not None:
        return best
    if candidates:
        top = max(candidates, key=lambda c: c["score"])
        if top["score"] >= single_min_score:
            return (top["x"], top["score"], f"{top['method']}(fallback)")
    if log:
        log(
            "[slider] 候选无共识，不拖（刷新换图）: "
            + ", ".join("%s:%d:%.2f" % (c["method"], c["x"], c["score"]) for c in candidates)
        )
    return None


def detect_gap(page, log=None) -> dict:
    """主检测：返回 {target_x_natural, score, method, ...} 或 None（检测失败）"""
    data = _grab_images(page)
    if not data:
        return None
    bg, pz = data["bg"], data["pz"]
    if bg.size == 0 or pz.size == 0:
        return None
    alpha = pz[..., 3]
    ys, xs = np.where(alpha > 128)
    if xs.size == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    crop_w, crop_h = x1 - x0 + 1, y1 - y0 + 1
    bh, bw = bg.shape[:2]
    if crop_w >= bw - 20 or crop_h >= bh:
        return None

    mask = (alpha[y0 : y1 + 1, x0 : x1 + 1] > 128)
    tpl_gray = _blur3(_to_gray(pz[y0 : y1 + 1, x0 : x1 + 1]))
    bg_gray = _blur3(_to_gray(bg))

    # 策略1：灰度 NCC（Y 限制在拼图原位 ±10）
    gx, gy, gs = _masked_ncc_scan(bg_gray, tpl_gray, mask, y0 - 10, y0 + 10)
    # 策略2：边缘 NCC
    tpl_edge = _sobel_x(tpl_gray)
    bg_edge = _sobel_x(bg_gray)
    ex, ey, es = _masked_ncc_scan(bg_edge, tpl_edge, mask, y0 - 10, y0 + 10)

    candidates = []
    if gx >= 15:
        candidates.append({"x": gx, "score": gs, "method": "gray-ncc"})
    if ex >= 15:
        candidates.append({"x": ex, "score": es, "method": "edge-ncc"})
    pair = _gap_edges_from_bg(bg_gray, crop_w)
    if pair and pair[0] >= 15 and pair[1] < bw - 15:
        candidates.append(
            {"x": pair[0], "score": pair[2], "method": "gap-edges",
             "gap_left": pair[0], "gap_right": pair[1]}
        )
    if not candidates:
        return None

    voted = _vote_gap(candidates, log=log)
    if voted is None:
        return None
    final_x, final_score, final_method = voted
    if final_x < 15:
        return None

    scale = data["bg_display_w"] / float(bw)
    if not (0.2 <= scale <= 5.0):
        # 2026-10-04 12:04 实证：display 布局塌缩瞬间 bg_display_w 可返回 0/垃圾值，
        # scale 异常时 target_display 全错（曾拖出 0.0px「收敛」）。丢弃本轮。
        if log:
            log(f"[slider] display 尺度异常 (bg_display_w={data['bg_display_w']}, scale={scale:.3f})——丢弃本轮")
        return None
    return {
        "target_x_natural": final_x,
        "score": round(final_score, 4),
        "method": final_method,
        "pz_offset_x": x0,
        "bg_natural_w": bw,
        "bg_display_w": data["bg_display_w"],
        "candidates": candidates,
        "slider_x": data["slider_x"],
        "slider_y": data["slider_y"],
        "slider_w": data["slider_w"],
        "slider_h": data["slider_h"],
        "puzzle_left": data["puzzle_left"],
        "scale": scale,
        "target_display": (final_x - x0) * scale,
    }


# ---------------- 拟人拖拽（闭环反馈） ----------------

def _human_track(distance: float):
    """变速拟人轨迹（kangleyao/slider-captcha-lab n=22 实测特征，2026-10-04 外部学习）。

    - 速度曲线：慢启动 → 巡航 → 约 75% 进度处峰值（末端甩鞭）→ 缓停；
      分类器实证「一切平滑匀速曲线=机器」（AUC=1.000 可分真人）。
    - y 平滑正弦漂移 8~20px（真人可达 38px；旧 uniform(±1px)=机器指纹）。
    - 轨迹内嵌 1~3 帧静止（120~260ms）。
    - 末段一次 ≤6px 的自然过冲回拉（连续轨迹段，非「读 DOM→微调」循环——
      松手前反复微调是实证机器反模式）。
    返回累计偏移点列 [(dx, dy, pause_ms), ...]，终点精确等于 distance。
    """
    if distance <= 0:
        return [(0.0, 0.0, 0)]
    n = max(26, min(64, int(distance / 5)))
    t = np.linspace(0.0, 1.0, n)
    w = np.ones(n)
    warm = t < 0.15  # 慢启动
    w[warm] = 0.22 + 0.78 * (t[warm] / 0.15) ** 2
    burst = (t >= 0.6) & (t <= 0.88)  # 甩鞭爆发（峰值在 0.75）
    w[burst] = 1.0 + 2.2 * np.exp(-(((t[burst] - 0.75) / 0.07) ** 2))
    tail = t >= 0.88  # 缓停
    w[tail] = np.maximum(1.0 - (t[tail] - 0.88) / 0.12, 0.06)
    s = np.cumsum(w) / float(w.sum()) * distance
    # 自然过冲回拉：倒数第 3 点冲过目标 ≤6px，两帧内回正（连续微 S）
    over = min(2.0 + distance * 0.015, 6.0)
    if n >= 6:
        s[-3] = max(s[-3], distance + over)
        s[-2] = distance + over * 0.4
        s[-1] = distance
    amp = random.uniform(8.0, 20.0)
    freq = random.uniform(1.2, 2.2)
    phase = random.uniform(0.0, 2.0 * math.pi)
    ys = amp * np.sin(2.0 * math.pi * freq * t + phase) + np.random.uniform(-1.2, 1.2, n)
    pts = []
    for i in range(n):
        pause = random.randint(6, 16)
        pts.append([float(s[i]), float(ys[i]), pause])
    # 1~3 帧静止（避开首尾）：位置重复前一点，停顿放大（真人中途停顿特征）
    for idx in random.sample(range(4, max(n - 5, 5)), min(3, max(n - 9, 1))):
        px, py, _ = pts[idx - 1]
        pts[idx] = [px, py, random.randint(120, 260)]
    return [tuple(p) for p in pts]


def _read_slider_left(page) -> float:
    """读滑块按钮本体的实时位移（style.left px）——比 puzzle 视差层更直接"""
    try:
        return float(
            page.evaluate(
                "() => { const el = document.querySelector('#aliyunCaptcha-sliding-slider');"
                " if (!el) return 0;"
                " const v = parseFloat(el.style.left);"
                " return isNaN(v) ? (el.getBoundingClientRect().x || 0) : v; }"
            )
        )
    except Exception:
        return 0.0


def _read_puzzle_left(page) -> float:
    try:
        return float(
            page.evaluate(
                "() => parseFloat(document.querySelector('#aliyunCaptcha-puzzle')?.style.left) || 0"
            )
        )
    except Exception:
        return 0.0


def drag_to(page, det: dict, log=print) -> bool:
    """拖滑块到目标：按按钮几何换算鼠标位移 + 甩鞭轨迹一次到位（不做拖后精修）。

    2026-10-04 登录页 dump 实证链（scratch/zcode_login_probe_dump.json）：
    - 鼠标 y 漂移 ±20px 会滑出 40px 高的滑块按钮 → SDK 停止跟手（slider.left
      只走 10px 就锁死），松手后回弹 0px——轨迹再拟人也白拖。
    - 故拖动期间 y 锁定按钮中心线 ±4px；y 漂移只在按下前/后的宏观看不见层做。
    - 位移换算：鼠标位移 = target_display × (track_w−btn_w)/(bg_w−btn_w)，
      拖满轨道 ≈ 拼图走完全图宽。
    """
    btn_w = 40.0
    sx = det["slider_x"] + det["slider_w"] / 2.0
    sy = det["slider_y"] + det["slider_h"] / 2.0
    target = det["target_display"]
    bg_w = det["bg_display_w"]
    # 2026-10-04 13:25 实证：_PX_JS 的 slider rect 是按钮本体（40px）不是轨道！
    # track_travel 必须用 bg_display_w−btn_w（轨道与背景图同宽 300px，dump 佐证：
    # sliderBody.w=300、bg.w=300、按钮 w=40）。缺口 target px 与鼠标位移 1:1。
    track_travel = max(bg_w - btn_w, 1.0)
    sweep = max(0.0, min(track_travel, target))

    page.mouse.move(sx, sy)
    page.wait_for_timeout(random.randint(60, 140))
    page.mouse.down()
    page.wait_for_timeout(random.randint(30, 70))

    # 甩鞭粗扫：x 变速一次到位；y 锁中心线 ±4px（按下期间丢事件=反模式实证）
    for dx, dy, pause in _human_track(sweep):
        page.mouse.move(sx + dx, sy + max(-4.0, min(4.0, dy * 0.25)))
        page.wait_for_timeout(pause)
    page.wait_for_timeout(random.randint(90, 180))
    page.mouse.up()

    # 松手后读滑块本体位置复核（只读不动作）：left≈sweep 判收敛，回弹=失败
    cur = _read_slider_left(page)
    reached = abs(cur - sweep) <= 5.0
    if reached:
        log(f"[slider] 滑块停在 {cur:.1f}px（目标 {sweep:.1f}，缺口 {target:.1f}px）")
    else:
        log(f"[slider] 对齐失败：滑块停在 {cur:.1f}px / 目标 {sweep:.1f}px（回弹=风控拒/丢事件）")
    return reached


# ---------------- 判定（严格口径） ----------------

_STATE_JS = """
() => {
  const q = s => document.querySelector(s);
  const win = q('#aliyunCaptcha-window-float');
  const body = q('#aliyunCaptcha-captcha-body');
  const wrap = q('#aliyunCaptcha-captcha-wrapper');
  const err = q('.aliyunCaptcha-tips-error');
  const vis = el => !!el && el.getBoundingClientRect().width > 0;
  return {
    windowShown: !!(win && win.classList.contains('window-show')),
    bodyClass: body ? body.className : null,
    bodyVisible: vis(body),
    sliderClass: (q('#aliyunCaptcha-sliding-slider') || {}).className || null,
    wrapperText: wrap ? String(wrap.innerText || '').slice(0, 120) : '',
    errorText: err ? String(err.innerText || '').slice(0, 80) : '',
    certifyId: (q('#aliyunCaptcha-certifyId') || {}).innerText || '',
  };
}
"""


def slider_state(page) -> dict:
    try:
        return page.evaluate(_STATE_JS)
    except Exception:
        return {"windowShown": False, "bodyVisible": False, "bodyClass": None, "wrapperText": "", "errorText": "state_error"}


def slider_passed_strict(page) -> bool:
    """严格通过判定：弹窗关闭 且（拼图区销毁/隐藏 或 成功态 class/文案）。

    「弹窗关闭」单独不构成通过（旧口径假阳性根源）；
    页面状态读不到（state_error）一律判否——未知状态不许放行。
    """
    st = slider_state(page)
    if st.get("errorText") == "state_error":
        return False
    if st.get("windowShown"):
        return False
    cls = (st.get("bodyClass") or "").lower()
    scl = (st.get("sliderClass") or "").lower()
    txt = st.get("wrapperText") or ""
    ok = (
        not st.get("bodyVisible")
        or "success" in cls
        or "pass" in cls
        or "success" in scl
        or "pass" in scl
        or "验证通过" in txt
        or "verification passed" in txt.lower()
    )
    return bool(ok)


def wait_passed_strict(page, timeout_s: float = 8.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if slider_passed_strict(page):
            return True
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    return False


# ---------------- 主入口 ----------------

def solve_slider(page, click_trigger_fn=None, log=print, max_attempts: int = 8, manual_wait_sec: int = 0, cloud_solve_fn=None) -> tuple:
    """自动求解当前滑块弹窗。返回 (passed: bool, how: str)。

    click_trigger_fn：弹窗被吞/刷新后重新调出滑块的回调（core._click_captcha_trigger）。
    manual_wait_sec>0：纯人工模式（用户代滑）——不调任何拖拽，弹窗出现后只轮询
    严格判定直到通过或超时；配合有头窗口，人在屏幕前把拼图滑上即过。
    cloud_solve_fn：可选，云打码闭包（调用方持有 scene_id/prefix 上下文时包
    core.captcha_router.solve_aliyun2 注入）；返回 captcha_verify_param 即视为通过，
    how 带 "cloud:" 前缀便于日志区分。未配置（CaptchaNotConfigured）自动回落本地滑块。
    """
    if cloud_solve_fn is not None:  # 云打码预留通道：调用方配了 captcha_provider 才传
        from core.captcha_router import CaptchaNotConfigured

        try:
            param = cloud_solve_fn()
            if str(param or "").strip():
                log("[slider] 云打码求解成功（captcha_verify_param）")
                return True, f"cloud:{param}"
            log("[slider] 云打码返回空 captcha_verify_param → 回落本地滑块")
        except CaptchaNotConfigured:
            log("[slider] 云打码未配置 → 回落本地滑块")
    if manual_wait_sec and manual_wait_sec > 0:
        import time as _t
        deadline = _t.time() + manual_wait_sec
        log(f"[slider][manual] 等待人工滑动（{manual_wait_sec}s 内）……")
        while _t.time() < deadline:
            try:
                if slider_passed_strict(page):
                    log("[slider][manual] 人工滑动通过")
                    return True, "manual_human"
            except Exception:
                pass
            _t.sleep(1)
        log("[slider][manual] 人工滑动超时未通过")
        return False, "manual_timeout"
    for attempt in range(1, max_attempts + 1):
        try:
            st = slider_state(page)
            if not st.get("windowShown"):
                if click_trigger_fn and not click_trigger_fn(page):
                    return False, f"第{attempt}次：弹窗调不出"
            det = detect_gap(page, log=log)
            if det is None and not _wait_bg_ready(page):
                # 图未就绪/未加载——等到就绪再试一次 grab
                det = detect_gap(page, log=log)
            if not det:
                log(f"[slider] 第{attempt}次：缺口检测失败（图像未就绪/无解）——刷新重试")
                _refresh_puzzle(page)
                continue
            log(
                f"[slider] 第{attempt}次检测：x={det['target_x_natural']}nat "
                f"→ {det['target_display']:.1f}px 显示 (score={det['score']}, {det['method']}, "
                f"候选={['%s:%d:%.2f' % (c['method'], c['x'], c['score']) for c in det['candidates']]})"
            )
            drag_reached = drag_to(page, det, log=log)
            page.wait_for_timeout(1500)
            if wait_passed_strict(page, 8):
                return True, f"第{attempt}次拖拽通过（{det['method']}）"
            st = slider_state(page)
            log(
                f"[slider] 第{attempt}次未通过严格判定"
                f"（windowShown={st.get('windowShown')}, err={st.get('errorText')!r}, "
                f"对齐{'收敛' if drag_reached else '未收敛'}）——刷新换图重试"
            )
            _refresh_puzzle(page)
        except Exception as e:
            log(f"[slider] 第{attempt}次异常: {str(e)[:120]}")
            try:
                _refresh_puzzle(page)
            except Exception:
                pass
    return False, f"{max_attempts}次尝试均未通过"


def _wait_bg_ready(page, timeout_s: float = 5.0) -> bool:
    """等背景图真正就绪：rect.w>0 且已加载——防「图未加载就检测」拖出全错坐标。

    2026-10-04 13:08 探针实证：refresh 换图后立刻 grab 会拿到 bg_display_w=0
    （naturalWidth 有值但 rect 宽 0），detect 直接拖出垃圾坐标。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            ok = page.evaluate(
                "() => { const b = document.querySelector('#aliyunCaptcha-img');"
                " if (!b) return false;"
                " const r = b.getBoundingClientRect();"
                " return r.width > 30 && r.height > 30 && b.naturalWidth > 30; }"
            )
            if ok:
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False


def _refresh_puzzle(page, log=None):
    """点刷新按钮换一张拼图；弹窗若已收起则什么都不做（外层会重新调出）"""
    try:
        btn = page.locator("#aliyunCaptcha-btn-refresh")
        if btn.count() > 0 and btn.first.is_visible():
            btn.first.click(timeout=3000)
            _wait_bg_ready(page)
            if log:
                log("[slider] 已点刷新换图（图已就绪）")
            return
    except Exception:
        pass
    try:
        page.wait_for_timeout(1200)
    except Exception:
        pass
