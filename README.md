# 戳个印

桌面版 PDF 图片水印工具。启动入口是 `pdf_watermark_demo.py`，可载入多个 PDF 和图片水印，在页面预览中调整位置后导出（预览与导出所见即所得）。启动时首页为空白，文档与水印图均由用户自行添加。

## 环境与安装

需要 Python 3.10+。安装运行所需依赖：

```bash
pip install -r requirements.txt
```

## 运行

```bash
python pdf_watermark_demo.py
```

本机已配置的解释器是 `D:\pyprojects\miniconda\python.exe`。在 PowerShell 中可使用 `& 'D:\pyprojects\miniconda\python.exe' pdf_watermark_demo.py` 启动，避免使用缺少依赖的其他 Python 环境。

在应用中添加 PDF 和 PNG/JPG 等水印图片，调整页面上的水印位置，再选择输出目录完成导出。

页面预览右上角的「全屏」按钮进入单页全屏视图：右键切换本页图章、按住左键拖动图章、滚轮调节图章大小，左右悬浮按钮（或方向键）翻页，右键页面外空白返回。全屏中的调整与预览、导出所见即所得。

首页“图片摆放布局”作用于全部已添加文档的页面，修改参数后点击“应用到全部页面”才生效。默认“保留当前位置”会保留单页拖动的位置与水印开关；选择其他对齐方式会重新定位每张水印。固定宽度使用 PDF 点（pt，72 pt = 1 英寸）。透明度滑块仅影响预览，导出保留图片本身的透明度。

导出结果以 `watermarked_原文件名.pdf` 命名；同名结果已存在时自动使用 `_2`、`_3` 等后缀，不覆盖已有文件。

导出在后台运行，期间会锁定编辑操作。点击“取消导出”后，当前 PDF 会安全完成，后续文件不再处理；导出过程中关闭窗口，也会等当前文件完成后再关闭。长文档导出经过专门优化：水印图在输出中只嵌入一次（多页共享），导出速度与输出体积不随页数显著膨胀。

导出结束后，点击“导出详情”可查看并复制成功文件的实际输出路径、失败文件名与错误原因，以及取消后未处理的数量。详情保留至下一次导出开始，可关闭后重新打开。

## 项目结构

```
├── pdf_watermark_demo.py       # Qt 桌面应用入口
├── pdf_watermark_studio/
│   ├── core/engine.py           # PDF 渲染、预览与水印导出引擎
│   ├── core/export_jobs.py      # 唯一输出命名与文件安全写入
│   ├── export_worker.py         # Qt 后台导出、取消与任务结果
│   └── src/                     # 应用图标与界面素材
├── tests/                       # 临时样本回归测试
├── build_exe.py                 # Windows 可执行文件打包脚本（PyInstaller）
├── _demo_smoke4.py              # 离屏冒烟测试（空白起步/拖动/导出）
├── _verify_align.py             # 预览与导出的像素级对齐验证
└── requirements.txt
```

## 打包 Windows 可执行文件

```bash
pip install pyinstaller
python build_exe.py               # onefile → dist/戳个印.exe
```

脚本会打包 `pdf_watermark_studio/src/` 品牌素材（图标、样式资源），产物为 `dist/戳个印.exe`。

## 本地版本管理与修复记录

项目已启用 Git。源码、品牌素材、测试和文档纳入版本管理；根目录用户 PDF/PNG、打包产物、缓存和本机配置由 `.gitignore` 排除，仍保留在本机。

```bash
git status
git log --oneline -10
```

逐项修复与验收记录见 [修复计划](docs/superpowers/plans/2026-09-12-watermark-fixes.md)。每完成一项会勾选并单独提交。

## 验证

```bash
python -m unittest discover -s tests -v
python _demo_smoke4.py
python _verify_align.py
```

两项脚本使用 Qt 离屏渲染，并只向临时目录导出。它们需要根目录已有的示例 PDF 和 `章.png`；这些用户样本不随 Git 分发。独立回归测试会自行创建临时 PDF 和图片。
