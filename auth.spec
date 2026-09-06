# -*- mode: python ; coding: utf-8 -*-
import os, sys
from PyInstaller.utils.hooks import collect_all

ddddocr_datas, ddddocr_binaries, ddddocr_hiddenimports = collect_all('ddddocr')
ort_datas, ort_binaries, ort_hiddenimports = collect_all('onnxruntime')
pw_datas, pw_binaries, pw_hiddenimports = collect_all('playwright')

# onnxruntime DLL
ort_path = None
for p in sys.path:
    candidate = os.path.join(p, 'onnxruntime', 'capi')
    if os.path.isdir(candidate):
        ort_path = candidate
        break

extra_binaries = []
if ort_path:
    for f in os.listdir(ort_path):
        if f.endswith('.dll'):
            full = os.path.join(ort_path, f)
            extra_binaries.append((full, '.'))
            extra_binaries.append((full, 'onnxruntime/capi'))

# Chromium headless shell（动态查找，避免硬编码用户路径与版本号）
# 本地可能残留多个版本：只收含可执行文件的候选，取版本号最新的一个；
# 目标目录名沿用源目录版本号（playwright install 装了什么版本就带什么版本），
# 不硬编码 1228——playwright 升级后硬编码版本会与安装版本错位，产物运行时静默缺浏览器。
import glob
local_appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
ms_pw_dir = os.path.join(local_appdata, "ms-playwright")
candidates = []
for d in glob.glob(os.path.join(ms_pw_dir, "chromium_headless_shell-*")):
    if os.path.isdir(os.path.join(d, "chrome-headless-shell-win64")):
        candidates.append(d)

extra_datas = []
if candidates:
    candidates.sort(reverse=True)
    chromium_src = os.path.join(candidates[0], "chrome-headless-shell-win64")
    rev = os.path.basename(candidates[0])
    chromium_dst = f"playwright/driver/package/.local-browsers/{rev}/chrome-headless-shell-win64"
    print(f"找到 Chromium headless shell：{chromium_src}")
    extra_datas.append((chromium_src, chromium_dst))
else:
    print("警告：未找到 Chromium headless shell 目录")

a = Analysis(
    ['campus_auth.py'],
    binaries=ddddocr_binaries + ort_binaries + pw_binaries + extra_binaries,
    datas=ddddocr_datas + ort_datas + pw_datas + extra_datas,
    hiddenimports=ddddocr_hiddenimports + ort_hiddenimports + pw_hiddenimports,
    hookspath=[],
    runtime_hooks=['hook_ort.py'],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='campus_auth',
    console=True,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    name='campus_auth',
)
