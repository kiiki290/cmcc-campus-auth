# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

校园宽带自动认证 CLI：针对校园网 Portal 认证的断线自动重连工具。Windows-only 单文件 Python 应用（`campus_auth.py`，依赖 ctypes.windll / DPAPI），全部注释与日志为中文 UTF-8。无测试框架、无 lint 配置、非 git 仓库。改动后只能实网运行验证（需直连校园网，Portal 仅校内可达）。

## 常用命令

项目 Python 为 `.venv`（3.11.9），docstring 中的 `py -3.11` 等价。

```powershell
.\.venv\Scripts\python.exe campus_auth.py              # 单次认证
.\.venv\Scripts\python.exe campus_auth.py --watch      # 断线自动重连（轮询 + 认证）
.\.venv\Scripts\python.exe campus_auth.py --reset      # 清除已存账号密码（保留检测间隔）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium   # 首次安装/更新后必做：headless shell
.\.venv\Scripts\python.exe -m PyInstaller auth.spec    # 打包，产物 dist\campus_auth\
```

无自动化测试；验证手段是实网运行单次/`--watch`。

## 架构

单文件、按注释分区：常量 → DPAPI 加密 → 配置读写 → 在线探测 → 认证核心 → 重试/监控循环 → CLI 入口。认证逻辑本身不大，**难理解的是"在线/断线状态机"——它完全靠推断网关行为判定，不查任何服务器 API**：

- **探测 = 找 Portal**：对黑洞 IP `BLACKHOLE_IP = 192.0.2.1`（TEST-NET-1 保留段，RFC 5737）发 `:80` 请求。
  - 未认证：BRAS(NetEngine) 对任意目的 `:80` 无差别 302 到 Portal，Location 带 `wlanuserip=` → 断线，且该 Location 就是认证页 URL（内含 WAN IP）。
  - 已在线：黑洞目标不可路由、永不应答 → 超时/空连接 → 在线。
  - **其余任何结局都按"在线"处理**（网络全断时也无对象可认证，静默等链路恢复后重新被 302）——见 `_get_portal_url()` / `is_offline_sync()`。
- **认证成功 ≠ BRAS 放行**：`do_auth` 收到"登录成功"页后，用真实主机 1.1.1.1（放行=正常应答如 301 跳 https；未放行=仍 302 带 wlanuserip）毫秒级确认，按 0/1/2/3/5/8s 递增等待（总窗口 ~19s 覆盖 BRAS 慢放行，避免误触发整轮重试）。确认用真实主机而轮询用黑洞 IP，原因正好相反：放行态下黑洞探测要白等满 `PROBE_TIMEOUT`。
- **致命错误**：返回页含 `FATAL_PATTERNS`（密码错误/欠费/停机/冻结等）→ 抛 `FatalAuthError`，不再重试、等回车退出。

## 认证流程（do_auth，playwright chromium headless）

1. 同步探测拿 `outer_url`（前一步已在线则直接返回）。
2. OCR 模型（ddddocr）在线程池后台加载，与浏览器启动并行；**playwright/ddddocr 均懒导入**，纯探测路径不付 import 成本。
3. 路由拦截：URL 命中 `ANTIBOT` 正则的请求走 fetch→fulfill 原样回放（Portal 反自动化/风控资源），其余 continue_。
4. goto 认证页后轮询定位认证 frame：**优先按 URL 含 `unionautologin.do` 特征找，回落 `frames[1]`**（Portal 调 frameset 结构时索引会错位，勿改回硬编码索引）。
5. 取 JSESSIONID（cookie 或 frame URL），请求 `/user/randomimage` 拉验证码，ddddocr 识别（≥4 字符才接受，3 次机会）。
6. fill `#username`/`#pwd`/`#ps` 后 JS 提交（把 jsessionid 拼进 form action 的 `unionautologin.do` 前）。
7. 解析返回页 `<title>` + `<div id="admin">` 判结果（成功 = title"登录成功"或含 logout/online；其余意外内容按 WAF 拦截/Portal 异常处理）。

## 同步/异步纪律与配置

- **每次认证/重试新建事件循环**跑 `do_auth`（`asyncio.run` 重入会崩）。单次模式的重试在 `auth_with_retry`，`--watch` 模式在 `watch_mode` 内联复制了一份同样的 3 轮重试逻辑——改重试行为时两处要同步。
- 凭据存 `%APPDATA%\campus_auth\config.json`：`{student_id, password, poll_interval}`。password 是 base64(DPAPI CryptProtectData，当前用户级)，**绑定本机+账户，跨机不可解**；解密失败或无密码 → `load_config` 返回 None → 静默触发重新输入。`--reset` 只清账号密码、保留间隔。
- 断线检测间隔 `POLL_INTERVAL` 默认 5s、下限 `MIN_POLL_INTERVAL` 3s（2026-09-06 调低：黑洞探测零外部压力，旧 10s 下限是 1.1.1.1 轮询时代遗留）；config 手写值也会被 `max()` 兜底。`PROBE_TIMEOUT` 2s 决定在线 tick 固定耗时，改小 = 轮询更密。
- 探测只发 `:80` 黑洞请求，对真实外部网络零打扰；**不要把轮询探测改回真实主机**（如旧版 1.1.1.1）——那是给外部服务加压力。

## 打包（auth.spec，PyInstaller one-folder）

- `runtime_hooks=['hook_ort.py']`：frozen 时把 `_MEIPASS`、`onnxruntime/capi`、exe 所在目录 `os.add_dll_directory`（onnxruntime DLL 定位）。
- 各库用 `collect_all` 收集；onnxruntime `capi` 目录全部 DLL 额外拷到根与 `onnxruntime/capi` 两处；Chromium headless shell 从 `%LOCALAPPDATA%\ms-playwright\chromium_headless_shell-*` 动态查找打进固定目标目录（**找不到只警告不报错** → 产物运行时缺浏览器，静默失败在 `pw.chromium.launch`）。
- `build\`（中间产物）与 `dist\`（产物）可随时重建。本机删除规范：一律回收站 `pwsh -File C:\Users\KI\bin\trash.ps1 <路径>`，勿用 rm/del。
