# -*- coding: utf-8 -*-
"""用 PyInstaller 把「戳个印」打包为单文件 exe。

用法： python build_exe.py
产物： dist/戳个印.exe

体积控制（本脚本自己生成 spec，在 Analysis 之后过滤）：
- datas 只带 Brand 实际加载的两张品牌图（不整目录 add-data）；
- 模块级 excludes + a.binaries/a.datas/a.pure 过滤，剔除：
  numpy/pandas/lxml（pymupdf.table 懒加载链被 PyInstaller 误当硬依赖）、
  tkinter/sqlite3、PIL AVIF 插件、Qt Quick/Qml/Pdf/Network/Svg/OpenGL、
  opengl32sw 软件渲染器、Qt 翻译与用不到的插件目录；
- 仍显式补打 conda Library\\bin 下的 stdlib 依赖 DLL（ffi.dll 等）。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pefile
from PIL import Image

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "pdf_watermark_studio" / "src"
BUILD = ROOT / "build_assets"
SPEC = BUILD / "戳个印.spec"
PY = sys.executable

# 1) 生成 exe 图标（戳个印.png → .ico）
BUILD.mkdir(exist_ok=True)
ICO = BUILD / "戳个印.ico"
if not ICO.exists():
    img = Image.open(SRC / "戳个印.png").convert("RGBA")
    img.save(ICO, format="ICO", sizes=[(256, 256), (128, 128), (64, 64),
                                       (48, 48), (32, 32), (16, 16)])
print("icon:", ICO)

out = subprocess.run([PY, "-m", "PyInstaller", "--version"],
                     capture_output=True, text=True)
if out.returncode != 0:
    sys.exit("未安装 PyInstaller，请先执行：python -m pip install pyinstaller")
print("PyInstaller 版本:", out.stdout.strip())

# 2) 只把应用实际加载的两张品牌图放进 payload（pdf_watermark_demo.Brand）
PAYLOAD = BUILD / "payload" / "pdf_watermark_studio" / "src"
shutil.rmtree(BUILD / "payload", ignore_errors=True)
for rel in ("戳个印.png", "Wild_Field/wild-field-logo-transparent-master.png"):
    dst = PAYLOAD / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC / rel, dst)
print("payload:", PAYLOAD)

# 3) conda(Anaconda) 的 stdlib 扩展依赖 Library\bin 下的 DLL（如
#    _ctypes.pyd → ffi.dll、_decimal.pyd → libmpdec-4.dll），PyInstaller
#    的依赖分析会漏掉，导致打包后报
#    "ImportError: DLL load failed while importing _ctypes"。
#    这里按导入表显式补打；python.org 版 Python 没有 Library\bin，扫描为空。
SKIP_DLLS = {"tcl86t.dll", "tk86t.dll", "sqlite3.dll"}  # 对应模块已整体排除
bin_dir = Path(sys.prefix) / "Library" / "bin"
extra_dlls = set()
for pyd in Path(sys.prefix, "DLLs").glob("*.pyd"):
    try:
        pe = pefile.PE(str(pyd), fast_load=True)
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    except OSError:
        continue
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        name = entry.dll.decode()
        if name in SKIP_DLLS or name.startswith("api-ms-"):
            continue
        if (bin_dir / name).is_file():
            extra_dlls.add(bin_dir / name)
binaries = [(str(p), ".") for p in sorted(extra_dlls)]
for src, _ in binaries:
    print("extra dll:", src)

# 4) 模块级排除（快路径）；spec 里的文件/模块过滤是兜底，
#    防止 hook 把被排除模块的 DLL/数据再塞回来。
EXCLUDES = [
    # pymupdf.table 的懒加载链 + 未使用的标准库
    "numpy", "pandas", "lxml", "brotli", "brotlicffi", "tkinter", "sqlite3",
    # Pillow 的 AVIF 编解码（只支持 PNG/JPG 等常规水印图）
    "PIL._avif", "PIL.AvifImagePlugin",
    # 未用到的 PySide6 模块（应用只用 Core/Gui/Widgets）
    "PySide6.QtNetwork", "PySide6.QtPdf", "PySide6.QtSvg",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQml", "PySide6.QtQmlModels",
    "PySide6.QtVirtualKeyboard", "PySide6.QtDesigner", "PySide6.QtUiTools",
]

# 注意：FILTER 会被原样写进 spec（普通字符串，反斜杠在源码里双写）。
FILTER = '''
import re as _re

# 名称统一归一化为 / 分隔、小写后再匹配；只删下列类别：
_DROP = _re.compile(
    r"(?i)^(?:"
    r"pyside6/opengl32sw\\.dll$|"
    r"pyside6/qt6(?!core\\.dll$|gui\\.dll$|widgets\\.dll$)[a-z0-9_]+\\.dll$|"
    r"pyside6/qt(?!core\\.pyd$|gui\\.pyd$|widgets\\.pyd$)[a-z0-9_]+\\.pyd$|"
    r"pyside6/translations/|"
    r"pyside6/qml/|"
    r"pyside6/plugins/(?:tls|networkinformation|qmltooling|designer|"
    r"assetimporters|renderers|webview|generic|position|geoservices|"
    r"canbus|sqldrivers|scxmldatamodel|multimedia|platforminputcontexts)/|"
    r"pyside6/plugins/imageformats/q(?:pdf|svg)\\.dll$|"
    r"pyside6/plugins/iconengines/|"
    r"numpy/|numpy\\.libs/|pandas/|lxml/|brotli/|brotlicffi/|"
    r"pil/_avif|_tkinter\\.pyd$|_sqlite3\\.pyd$|"
    r"tcl86t\\.dll$|tk86t\\.dll$|sqlite3\\.dll$"
    r")"
)

def _keep(entry):
    return not _DROP.search(entry[0].replace(chr(92), "/"))

a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

_DROP_MOD = _re.compile(
    r"^(?:numpy|pandas|lxml|brotli|brotlicffi|tkinter|sqlite3)(?:\\.|$)"
    r"|^PIL\\.(?:_avif|AvifImagePlugin)$"
    r"|^PySide6\\.Qt(?:Network|Pdf|Svg|OpenGL\\w*|Quick\\w*|Qml\\w*"
    r"|VirtualKeyboard|Designer|UiTools)"
    r"|^_tkinter$|^_sqlite3$"
)
a.pure = [m for m in a.pure if not _DROP_MOD.search(m[0])]
'''

spec_text = f'''# -*- mode: python ; coding: utf-8 -*-
# 由 build_exe.py 生成（含体积过滤），手工修改会被下次构建覆盖。
a = Analysis(
    [{str(ROOT / 'pdf_watermark_demo.py')!r}],
    pathex=[],
    binaries={binaries!r},
    datas=[({str(PAYLOAD)!r}, 'pdf_watermark_studio/src')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes={EXCLUDES!r},
    noarchive=False,
    optimize=0,
)
{FILTER}
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='戳个印',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 会显著增加杀软误报率，保持关闭
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[{str(ICO)!r}],
)
'''
SPEC.write_text(spec_text, encoding="utf-8")
print("spec:", SPEC)

args = [PY, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(BUILD / "pyinstaller"),
        str(SPEC)]
print("PyInstaller args:")
for a in args:
    print("  ", a)

rc = subprocess.call(args, cwd=str(ROOT))
if rc != 0:
    sys.exit(rc)
print("BUILD DONE ->", ROOT / "dist" / "戳个印.exe")
