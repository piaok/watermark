# -*- coding: utf-8 -*-
"""用 PyInstaller 把「戳个印」打包为单文件 exe。

用法： python build_exe.py
产物： dist/戳个印.exe（打包品牌素材 pdf_watermark_studio/src；
      应用启动即空白，样例 PDF/PNG 不再打进包里）。"""
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "pdf_watermark_studio" / "src"
BUILD = ROOT / "build_assets"

# 1) 生成 exe 图标（戳个印.png → .ico）
BUILD.mkdir(exist_ok=True)
ICO = BUILD / "戳个印.ico"
if not ICO.exists():
    img = Image.open(SRC / "戳个印.png").convert("RGBA")
    img.save(ICO, format="ICO", sizes=[(256, 256), (128, 128), (64, 64),
                                       (48, 48), (32, 32), (16, 16)])
print("icon:", ICO)

out = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                     capture_output=True, text=True)
if out.returncode != 0:
    sys.exit("未安装 PyInstaller，请先执行：python -m pip install pyinstaller")
print("PyInstaller 版本:", out.stdout.strip())

args = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--onefile", "--windowed",
    "--name", "戳个印",
    "--icon", str(ICO),
    f"--add-data={SRC}{os.pathsep}pdf_watermark_studio/src",
    "--distpath", "dist",
    "--workpath", str(ROOT / "build_assets" / "pyinstaller"),
    "--specpath", "build_assets",
    str(ROOT / "pdf_watermark_demo.py"),
]
print("PyInstaller args:")
for a in args:
    print("  ", a)

rc = subprocess.call(args)
if rc != 0:
    sys.exit(rc)
print("BUILD DONE ->", ROOT / "dist" / "戳个印.exe")
