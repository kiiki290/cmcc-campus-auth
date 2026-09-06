# 校园宽带自动认证 CLI（cmcc-campus-auth）

中国移动校园宽带 Portal 认证的**断线自动重连**工具 —— Windows 单文件 Python 应用。

检测到掉线后自动重新认证，全程无需人工干预；验证码由本地 OCR 识别，密码用 Windows DPAPI 加密存储，凭据不出本机。

```
单次认证        python campus_auth.py
断线自动重连    python campus_auth.py --watch
清除已存账号    python campus_auth.py --reset
```

## 功能特性

- **断线自动重连**：轮询探测在线状态，掉线即自动重走认证流程（`--watch` 模式）
- **验证码自动识别**：ddddocr 本地 OCR，不调任何外部识别服务
- **凭据加密存储**：密码经 Windows DPAPI（CryptProtectData）加密后写入本地配置，**绑定当前 Windows 账户 + 本机**，换机/换账户即不可解密
- **浏览器自动化**：Playwright Chromium headless 模拟登录，自动回放 Portal 反自动化/风控资源，绕过无头浏览器检测
- **零打扰探测**：在线检测只向黑洞地址发请求，不会给任何外部服务器加压力

## 工作原理（简版）

工具不知道"服务器在哪里"，全靠**推断网关行为**判断在线/断线：

1. **探测 = 找 Portal**：向黑洞 IP `192.0.2.1`（TEST-NET-1 保留段，全网不可路由）发 `:80` 请求。
   - 未认证时：BRAS 网关对任意目的 `:80` 无差别 302 重定向到 Portal → 判为**断线**，并顺势拿到认证页地址（内含本机 WAN IP）；
   - 已在线时：黑洞目标永不应答 → 超时 → 判为**在线**。
2. **认证**：Playwright 打开认证页，OCR 识别验证码，填入账号密码提交。
3. **确认放行**：收到"登录成功"页后，再向真实主机 1.1.1.1 探测——网关放行则正常应答，未放行则仍被 302 到 Portal，以此确认 BRAS 真正放行（避免慢放行误触发整轮重试）。

> 认证页仅校园网内可达：在校外运行时探测永远超时（视为在线），程序静默待命，不会报错。

## 环境要求

- Windows 10/11（依赖 `ctypes.windll` / DPAPI，不支持其它系统）
- Python 3.11（或更高 3.x；代码用了 `X | None` 类型语法，需 3.10+）

## 快速开始

```powershell
# 1. 创建虚拟环境并安装依赖
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# 2. 安装 Chromium headless shell（Playwright 必需，仅首次需要）
python -m playwright install chromium

# 3. 首次运行：交互输入账号密码（密码输入不可见），单次认证
python campus_auth.py
```

之后挂机用 `--watch` 断线自动重连：

```powershell
python campus_auth.py --watch
```

### 凭据与配置

- 凭据保存位置：`%APPDATA%\campus_auth\config.json`
- 密码字段是 base64(DPAPI 密文)，绑定当前用户+本机，复制到其它机器无法解密（解密失败时会静默提示重新输入，不会崩）
- `--reset` 只清除账号密码，保留检测间隔设置
- 断线检测间隔：默认 **5 秒**，可在配置中加 `"poll_interval": 10` 覆盖（下限 3 秒，程序自动兜底）

### 致命错误（不重试）

密码错误、账号不存在、欠费、停机/停用、冻结/锁定、过期等情形会打印原因后**等待回车退出**，避免做无意义重试——先处理账户状态，再重新运行。

## 打包成 exe

```powershell
python -m PyInstaller auth.spec
# 产物：dist\campus_auth\
```

- `auth.spec` 已处理 onnxruntime DLL 定位（`runtime_hooks=['hook_ort.py']`），并把本机 Playwright headless shell 打进去
- 打包后 Chromium headless shell 找不到时**只警告不报错**——产物的首次运行会静默失败，此时需重新打包

## 技术架构

单文件 `campus_auth.py`，按注释分区：常量 → DPAPI 加密 → 配置读写 → 在线探测 → 认证核心 → 重试/监控循环 → CLI 入口。同步/异步纪律：每次认证/重试都新建事件循环跑 `do_auth`（`asyncio.run` 不可重入）。

**改动的注意点**（维护者必读）：

- 轮询探测只发黑洞地址请求，**勿改回真实主机轮询**（旧版 1.1.1.1 方案会给外部服务加压力）
- 定位认证 frame 时优先按 URL 含 `unionautologin.do` 特征查找，回落 `frames[1]`——Portal 调 frameset 时索引会错位，勿改回硬编码
- 单次模式与 `--watch` 模式各自内置一份相同的 3 轮重试逻辑，改动时两处要同步

## 局限与声明

- 只针对特定校园网 Portal（BRAS/NetEngine 行为 + 认证页结构）验证过，换网络环境不一定可用
- 在线/断线判定依赖对网关行为的推断，不查询任何服务器 API；网关行为变化可能导致误判（多数情形会退化为"按在线处理"，不会误发认证请求）
- 认证页仅校内可达，本工具在校外无实际作用

## License

[MIT](LICENSE) © 2026 kiiki290
