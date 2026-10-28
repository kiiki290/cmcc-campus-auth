"""
校园宽带自动认证 CLI 版
用法：
    py -3.11 campus_auth.py              # 单次认证
    py -3.11 campus_auth.py --watch      # 断线自动重连模式
    py -3.11 campus_auth.py --reset      # 重置保存的账号密码
"""

import asyncio
import re
import random
import sys
import time
import json
import os
import argparse
import getpass
from datetime import datetime

import http.client
import socket
import ctypes
from ctypes import wintypes

# 控制台输出统一 UTF-8：Windows 下 stdout 一旦是管道/重定向（> 日志、计划任务、
# 管道捕获），Python 3.11 回落到 locale 编码（GBK），✓/✗/⚠ 等字符无法编码会抛
# UnicodeEncodeError 把进程打崩（2026-09-06 审查发现并本机实测复现）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 常量 ──────────────────────────────────────────────────────────────────────
PORTAL        = "http://218.200.239.185:8888/portalserver"
# 瑞数（Riversec）动态防护引擎脚本的随机目录 / 文件名特征；命中后原样回放，
# 确保浏览器内 JS 正常执行并生成 NT cookie（旧文档曾误记为腾讯云 WAF）。
ANTIBOT       = re.compile(r"4QbVtADbnLVIc|\.a5bda9d\.", re.IGNORECASE)
# 断线探测目标：TEST-NET-1 保留段（RFC 5737），全网不可路由、无任何服务应答——
# 在线时探测必然无响应（不打扰任何外部服务），未认证时被网关（BRAS/NetEngine）
# 无差别 302 到 Portal。2026-09-06 实网验证过两种状态。
BLACKHOLE_IP  = "192.0.2.1"
PROBE_TIMEOUT = 2  # 在线时每次探测等满该值；网关 302 为本地毫秒级应答，2s 足够且
                   # 留足 WLAN 重传余量；同时决定在线 tick 的固定耗时，越小轮询越密
CONFIG_DIR    = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "campus_auth")
CONFIG_FILE   = os.path.join(CONFIG_DIR, "config.json")
POLL_INTERVAL     = 5    # 断线检测间隔默认值（秒），config.json 可覆盖；2026-09-06 由旧项目
                        # 的 15 调低：黑洞探测零外部压力，5s 处于甜点区（检测延迟 ~7s）
MIN_POLL_INTERVAL = 3    # 最小间隔下限。轮询不碰 Portal/瑞数 WAF（仅认证时碰），黑洞探测
                        # 零外部压力——旧项目 10s 下限（防 1.1.1.1 压力）已随 BLACKHOLE_IP 失效；
                        # 实际甜点在 3-5s：检测延迟 = 间隔 + PROBE_TIMEOUT + 认证 ~6-8s
MAX_RETRY         = 3    # 单次认证最大尝试次数（含首次；即首次 + 2 次重试）

# 服务器返回中包含以下关键词时，判定为「不应重试」的致命错误
FATAL_PATTERNS = [
    "密码错误", "密码不正确", "密码无效", "密码错",
    "账号不存在", "用户不存在", "帐号不存在",
    "欠费", "已停机", "已停用", "暂停使用",
    "已锁定", "已冻结", "已禁用", "已注销",
    "账号过期", "帐号过期", "已到期",
]

class FatalAuthError(Exception):
    """不应重试的致命认证错误（密码错误、欠费、账号停用等）"""
    pass


# ── 密码加密（Windows DPAPI，绑定当前用户+本机）───────────────────────────
# config.json 中密文离开本机/本账户即不可解密，零外部依赖。
def _dpapi_protect(plain: bytes) -> bytes:
    """调用 CryptProtectData 加密（当前用户级别）"""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(plain, len(plain))
    blob_in = DATA_BLOB(len(plain), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), 0, 0, 0, 0, 0x01,
        ctypes.byref(blob_out)
    ):
        raise OSError("CryptProtectData 失败")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result

def _dpapi_unprotect(cipher: bytes) -> bytes | None:
    """调用 CryptUnprotectData 解密"""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(cipher, len(cipher))
    blob_in = DATA_BLOB(len(cipher), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), 0, 0, 0, 0, 0x01,
        ctypes.byref(blob_out)
    ):
        return None
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result

def _encrypt(plain: str) -> str:
    """DPAPI 加密，返回 base64 密文"""
    import base64
    return base64.b64encode(_dpapi_protect(plain.encode())).decode()

def _decrypt(cipher: str) -> str | None:
    """DPAPI 解密，失败返回 None"""
    import base64
    try:
        raw = base64.b64decode(cipher.encode())
        plain = _dpapi_unprotect(raw)
        return plain.decode() if plain else None
    except Exception:
        return None


# ── 工具函数 ──────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_exit():
    """等待用户按回车后退出。stdin 被关闭（计划任务/管道/后台运行）时 input() 抛
    EOFError——无人值守场景直接放行退出即可，不要崩出 traceback。"""
    try:
        input("按回车键退出 ...")
    except EOFError:
        pass


def load_config() -> dict | None:
    """加载配置，返回 {student_id, password, poll_interval}"""
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        pwd = cfg.get("password", "")
        if pwd:
            decrypted = _decrypt(pwd)
            if decrypted is not None:
                cfg["password"] = decrypted
            else:
                log("⚠ 密码解密失败（换了机器或账户？），请重新输入账号密码")
                return None
        else:
            # config 存在但没有密码（--reset 后保留的间隔），触发重新输入
            return None
        # 学号缺失/非字符串（手写半截配置）同样触发重新输入，否则 main() 取键 KeyError
        sid = cfg.get("student_id")
        if not isinstance(sid, str) or not sid.strip():
            log("⚠ 配置中的学号缺失或无效，请重新输入账号密码")
            return None
        cfg["student_id"] = sid.strip()
        cfg.setdefault("poll_interval", POLL_INTERVAL)
        # 防护：即使 config.json 中手写小于 MIN_POLL_INTERVAL 的值，也强制兜底
        cfg["poll_interval"] = max(cfg["poll_interval"], MIN_POLL_INTERVAL)
        return cfg
    except Exception:
        return None


def save_config(student_id: str, password: str, poll_interval: int | None = None):
    """保存配置，密码自动 DPAPI 加密"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = {
        "student_id": student_id,
        "password": _encrypt(password),
    }
    if poll_interval is not None:
        data["poll_interval"] = poll_interval
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    log(f"账号密码已 DPAPI 加密保存到 {CONFIG_FILE}")


def prompt_credentials() -> tuple[str, str, int]:
    """输入账号密码，间隔默认值优先从已有 config 读取"""
    default_interval = POLL_INTERVAL
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                default_interval = json.load(f).get("poll_interval", POLL_INTERVAL)
        except Exception:
            pass
    print("请输入校园宽带账号密码（将加密保存到本地，输入时密码不可见）：")
    while True:
        sid = input("账号: ").strip()
        if sid:
            break
        print("  账号不能为空，请重新输入")
    while True:
        pwd = getpass.getpass("密码: ").strip()
        if pwd:
            break
        print("  密码不能为空，请重新输入")
    while True:
        try:
            raw = input(f"输入断线检测间隔（秒，不小于{MIN_POLL_INTERVAL}，直接回车为默认 {default_interval}）: ").strip()
            interval = int(raw) if raw else default_interval
        except ValueError:
            print(f"  请输入有效数字，或不输入使用默认值 {default_interval}s")
            continue
        if interval < MIN_POLL_INTERVAL:
            print(f"  间隔不能小于 {MIN_POLL_INTERVAL} 秒，请重新输入")
            continue
        break
    save_config(sid, pwd, interval)
    return sid, pwd, interval


# ── 检测是否在线（轻量级，不用浏览器）─────────────────────────────────────────
def is_offline_sync() -> bool:
    """探测是否断线：仅当网关 302 重定向到 Portal（带 wlanuserip=）才算断线；
    其余任何结局（超时/空连接/不可达）都视为在线继续等——黑洞目标在线时本就无响应，
    网络完全中断时也没有可认证的对象，安静等待直到链路恢复被 302。"""
    try:
        return _get_portal_url() is not None
    except Exception:
        return False


def _get_portal_url() -> str | None:
    """探测 BLACKHOLE_IP（不可路由保留 IP）。被网关拦截 → 返回完整 Portal URL；
    已在线 → 无任何 HTTP 响应（超时/空连接），返回 None。
    2026-09-06 实网验证：未认证时 BRAS(NetEngine Server) 对任意目的 :80 一律
    302 + wlanuserip，且与目的 IP 无关；故 302 之外的结局都可判定在线。"""
    conn = http.client.HTTPConnection(BLACKHOLE_IP, timeout=PROBE_TIMEOUT)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location", "")
            resp.read()
            return loc if "wlanuserip=" in loc else None
        resp.read()
        return None
    finally:
        conn.close()


def _is_released() -> bool | None:
    """认证后确认 BRAS 是否放行：GET 真实可达的 HTTP 端点。
    三态：True=已放行；False=仍被网关 302 拦截（未放行）；None=探测异常
    （网络瞬断等），状态未知——探测异常不等于"未放行"，勿混淆。
    放行 → 端点正常应答（如 Cloudflare 301 跳 https）；未放行 → 网关仍 302 拦截。
    端点正常应答时毫秒级，探测异常时单次探测超时设为 3s；确认目标用真实主机而非黑洞 IP，
    因为放行态下黑洞探测要白等满 PROBE_TIMEOUT（每次登录仅确认请求，外部负担可忽略）。"""
    try:
        conn = http.client.HTTPConnection("1.1.1.1", timeout=3)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location", "")
                resp.read()
                if "wlanuserip=" in loc:
                    return False  # 仍被网关注入，未放行
            else:
                resp.read()
            return True
        finally:
            conn.close()
    except Exception:
        return None


def _load_ocr():
    """加载 ddddocr 模型（供后台线程调用）。返回 OCR 实例或 None。"""
    try:
        import ddddocr
        return ddddocr.DdddOcr(show_ad=False)
    except ImportError:
        return None


# ── 认证核心 ──────────────────────────────────────────────────────────────────
async def do_auth(student_id: str, password: str) -> bool:
    # 提前获取 Portal 地址（同步 HTTP，无需浏览器）
    log("获取 Portal 地址 ...")
    try:
        outer_url = _get_portal_url()
    except (socket.timeout, ConnectionResetError):
        # 黑洞探测目标在线时无应答是常态（超时/连接被空关闭）→ 已在线
        log("✓ 已在线，无需认证")
        return True
    except OSError as e:
        # 与 is_offline_sync() 不同：单次认证无法把"网卡不可用"当作在线，按失败处理
        log(f"  网络不可达（errno {e.errno or '?'}），无法确认认证状态")
        return False
    if not outer_url:
        log("✓ 已在线，无需认证")
        return True
    m = re.search(r"wlanuserip=(\d+\.\d+\.\d+\.\d+)", outer_url)
    log(f"  WAN IP = {m.group(1)}" if m else f"  Portal = {outer_url[:60]}...")

    # 懒导入：只有真正走认证路径才付 playwright import 成本（纯探测/已在线路径免）
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        # OCR 模型在后台线程加载，与浏览器启动并行
        loop = asyncio.get_running_loop()
        ocr_future = loop.run_in_executor(None, _load_ocr)

        browser = await pw.chromium.launch(
            headless=True,
            # 禁系统代理：探测走直连 http.client，而 Chromium 默认跟随 Windows 系统
            # 代理（Clash/V2Ray 等），两者不一致会让 Portal 请求被送进代理走错路径
            args=["--disable-blink-features=AutomationControlled", "--no-proxy-server"]
        )
        try:
            # UA 的作用是去掉 Chromium 默认的 HeadlessChrome 标记（瑞数首层按 UA
            # 分流）；版本号固定为 Chrome/124，实网可用。Playwright 升级后实际
            # Chromium 版本会变（本机 1.61 为 149.0.7827.55），如需严格一致可从
            # browser.version 动态生成（未实网验证）。
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )

            async def intercept(route):
                try:
                    if ANTIBOT.search(route.request.url):
                        resp = await route.fetch()
                        await route.fulfill(response=resp)
                    else:
                        await route.continue_()
                except Exception as e:
                    msg = str(e)
                    if "closed" not in msg.lower() and "disposed" not in msg.lower():
                        log(f"  ⚠ 请求拦截异常（{route.request.url[:60]}）：{e}")
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            page = await context.new_page()
            await page.route("**/*", intercept)

            # 加载认证页面（直接使用网关重定向的目标地址）
            log("加载认证页面 ...")
            await page.goto(outer_url, wait_until="domcontentloaded", timeout=15000)

            # 轮询等待认证 Frame 出现（每次 0.3s，最多 10s）。
            # 优先按 URL 特征定位（unionautologin.do），不依赖 frames[1] 固定索引——
            # Portal 调整 frameset 结构时索引可能错位；找不到特征时回落旧逻辑。
            auth_frame = None
            for _ in range(33):
                await asyncio.sleep(0.3)
                for candidate in page.frames:
                    if "unionautologin.do" in candidate.url:
                        try:
                            cnt = await candidate.content()
                        except Exception:
                            continue
                        if cnt.strip() not in ("", "<html><head></head><body></body></html>"):
                            auth_frame = candidate
                            break
                if auth_frame is None and len(page.frames) >= 2:
                    candidate = page.frames[1]
                    cnt = await candidate.content()
                    if cnt.strip() not in ("", "<html><head></head><body></body></html>"):
                        auth_frame = candidate
                if auth_frame is not None:
                    break
            if auth_frame is None:
                log("  ✗ 找不到认证 Frame，请检查网络连接或稍后重试")
                return False
            log("  认证页面加载成功")

            # 验证码
            log("识别验证码 ...")
            cookies = await context.cookies()
            jsessionid = next((c["value"] for c in cookies if c["name"] == "JSESSIONID"), "")
            if not jsessionid:
                jsid_match = re.search(r"jsessionid=([A-F0-9]+)", auth_frame.url, re.IGNORECASE)
                jsessionid = jsid_match.group(1) if jsid_match else ""
            log(f"  JSESSIONID = {jsessionid[:12]}..." if jsessionid else "  ⚠ 未获取到 JSESSIONID，验证码可能无法正常获取")

            # 等待 OCR 模型就绪（与浏览器启动 + Portal 加载并行，此时通常已完成）
            ocr = await ocr_future
            if ocr is None:
                log("  ✗ 缺少依赖 ddddocr，请运行：pip install ddddocr")
                return False

            captcha_code = None
            for attempt in range(1, 4):
                cap_url = (
                    f"{PORTAL}/user/randomimage"
                    + (f";jsessionid={jsessionid}" if jsessionid else "")
                    + f"?id={random.randint(100000, 999999)}"
                )
                cap_resp = await context.request.get(cap_url)
                if "image" not in cap_resp.headers.get("content-type", ""):
                    log(f"  第 {attempt} 次验证码获取失败，重试 ...")
                    await asyncio.sleep(1)
                    continue
                img_bytes = await cap_resp.body()
                captcha_code = ocr.classification(img_bytes).strip().replace(" ", "")
                log(f"  第 {attempt} 次识别：{captcha_code}")
                if len(captcha_code) >= 4:
                    break
                captcha_code = None

            if not captcha_code:
                log("  验证码识别失败")
                return False

            # 提交
            log("提交登录表单 ...")
            await auth_frame.fill("#username", student_id)
            await auth_frame.fill("#pwd", password)
            await auth_frame.fill("#ps", captcha_code)

            async with page.expect_response(
                lambda r: "unionautologin.do" in r.url and r.request.method == "POST",
                timeout=20000
            ) as resp_info:
                escaped_jsessionid = jsessionid.replace("'", "\\'")
                await auth_frame.evaluate(f"""
                    var f = document.querySelector('form');
                    if (!f) throw new Error('找不到登录表单');
                    if ('{escaped_jsessionid}' && !f.action.includes('jsessionid')) {{
                        f.action = f.action.replace(
                            'unionautologin.do',
                            'unionautologin.do;jsessionid={escaped_jsessionid}'
                        );
                    }}
                    f.submit();
                """)

            resp = await resp_info.value
            text = await resp.text()

            # ── 解析服务器返回的具体信息 ──
            title_match = re.search(r"<title>(.*?)</title>", text)
            title = title_match.group(1) if title_match else ""
            admin_match = re.search(r'<div id="admin">\s*(.*?)\s*</div>', text, re.DOTALL)
            admin_text = re.sub(r"<[^>]+>", "", admin_match.group(1)) if admin_match else ""
            admin_text = re.sub(r"\s+", " ", admin_text).strip()  # 压缩空白

            if title == "登录失败":
                log(f"✗ 登录失败 — {admin_text}" if admin_text else "✗ 登录失败")
                # 检查是否为致命错误（不应重试）
                for pat in FATAL_PATTERNS:
                    if pat in admin_text or pat in text:
                        raise FatalAuthError(f"{pat} — {admin_text}" if admin_text else pat)
                return False

            page_ok = title == "登录成功" or "logout" in text or "online" in text
            if not page_ok:
                # 服务器返回了意外内容（可能是瑞数 WAF 拦截或 Portal 异常）
                preview = text[:200].replace("\n", " ")
                log(f"✗ 服务器返回异常响应：{preview}")
                return False

            log(f"✓ 认证成功！{admin_text}" if admin_text else "✓ 认证成功！")
            # 页面成功 ≠ BRAS 已放行（姊妹项目 CampusAuth 实测）：POST 返回后立即探测，
            # 未放行则按 0/1/2/3/5/8s 递增等待（sleep 合计 ~19s，按每次探测 3s 估算最坏 ~37s，
            # 覆盖 BRAS 慢放行，避免误触发整轮重试）。
            # 确认用 _is_released() 的真实主机探测（正常应答毫秒级），而非黑洞 IP——
            # 后者"放行=无应答"，6 次探测各要白等满 PROBE_TIMEOUT（2s），会把确认拖慢 ~12s
            for delay in (0, 1, 2, 3, 5, 8):
                if delay:
                    await asyncio.sleep(delay)
                state = _is_released()
                if state is True:
                    log("✓ 已确认在线（BRAS 放行）")
                    return True
                if state is None:
                    # 探测异常（网络瞬断等）≠ 明确未放行：不再烧完剩余递增等待，
                    # 本轮按失败处理，由外层重试的入口探测去区分"已放行/仍拦截/断网"，
                    # 避免一次链路抖动把确认拖到 ~37s 再叠加整轮重试。
                    log("  ⚠ 确认探测异常（状态未知），本轮按失败处理，稍后整轮重试")
                    return False
                if delay == 0:
                    log("  等待 BRAS 放行 ...")
            log("✗ 页面已接受但 BRAS 未放行，按失败重试")
            return False
        finally:
            await browser.close()


# ── 单次认证（带重试）────────────────────────────────────────────────────────
def auth_with_retry(student_id: str, password: str) -> bool:
    for attempt in range(1, MAX_RETRY + 1):
        if attempt > 1:
            log(f"第 {attempt}/{MAX_RETRY} 次尝试 ...")
        try:
            # 每次重试创建全新事件循环；asyncio.run() 不能在已有事件循环中嵌套调用，
            # 新建循环也用于隔离 Playwright 连接
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(do_auth(student_id, password))
            finally:
                loop.close()
        except FatalAuthError as e:
            log(f"✗ 致命错误，不再重试：{e}")
            wait_exit()
            return False
        except Exception as e:
            log(f"认证异常：{e}")
            result = False
        if result:
            return True
        if attempt < MAX_RETRY:
            log("5 秒后重试 ...")
            time.sleep(5)
    return False


# ── 断线监控模式 ──────────────────────────────────────────────────────────────
def watch_mode(student_id: str, password: str, poll_interval: int = POLL_INTERVAL):
    # 周期写实：在线时每轮探测要白等满 PROBE_TIMEOUT，检测周期 ≈ 间隔 + 探测超时；
    # 掉线瞬间探测立即返回 302，故实际发现掉线不慢于一个周期
    log(f"进入断线监控模式（间隔 {poll_interval}s，在线探测超时 {PROBE_TIMEOUT}s，"
        f"检测周期约 {poll_interval + PROBE_TIMEOUT}s），Ctrl+C 退出")
    while True:
        try:
            offline = is_offline_sync()
            if offline:
                log("检测到断线，开始认证（最多 3 轮，间隔 5 秒）...")
                for i in range(1, MAX_RETRY + 1):
                    if i > 1:
                        log(f"第 {i}/{MAX_RETRY} 轮认证尝试 ...")
                    try:
                        loop = asyncio.new_event_loop()
                        try:
                            ok = loop.run_until_complete(do_auth(student_id, password))
                        finally:
                            loop.close()
                    except FatalAuthError as e:
                        log(f"✗ 致命错误，停止监控：{e}")
                        wait_exit()
                        sys.exit(1)
                    except Exception as e:
                        log(f"认证异常：{e}")
                        ok = False
                    if ok:
                        log("✓ 重连成功，继续监控")
                        break
                    if i < MAX_RETRY:
                        log("5 秒后重试 ...")
                        time.sleep(5)
                else:
                    # 3 轮全部失败
                    log("✗ 断线后 3 轮认证均失败，可能原因：网络中断 / 账号异常 / 服务器故障")
                    wait_exit()
                    sys.exit(1)
            else:
                log("在线 ✓")
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            log("已退出监控模式")
            break
        except Exception as e:
            log(f"监控异常：{e}，{poll_interval} 秒后重试")
            time.sleep(poll_interval)


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="校园宽带自动认证")
    parser.add_argument("--watch", action="store_true", help="断线自动重连模式")
    parser.add_argument("--reset", action="store_true", help="重置保存的账号密码")
    args = parser.parse_args()

    if args.reset:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    old = json.load(f)
                interval = old.get("poll_interval", POLL_INTERVAL)
            except Exception:
                interval = POLL_INTERVAL
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"poll_interval": interval}, f, ensure_ascii=False)
            log(f"已清除账号密码，检测间隔保留为 {interval}s")
        else:
            log("没有找到已保存的账号密码")
        return

    config = load_config()
    if config:
        student_id = config["student_id"]
        password   = config["password"]
        interval   = config.get("poll_interval", POLL_INTERVAL)
        log(f"已加载账号：{student_id}")
    else:
        student_id, password, interval = prompt_credentials()

    if args.watch:
        # 入口兜底：interval 最终不应小于 MIN_POLL_INTERVAL
        watch_mode(student_id, password, max(interval, MIN_POLL_INTERVAL))
    else:
        success = auth_with_retry(student_id, password)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
