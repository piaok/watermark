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

在应用中添加 PDF 和 PNG/JPG 等水印图片，调整页面上的水印位置，再选择输出目录完成导出。

## 项目结构

```
├── pdf_watermark_demo.py       # Qt 桌面应用入口
├── pdf_watermark_studio/
│   ├── core/engine.py           # PDF 渲染、预览与水印导出引擎
│   └── src/                     # 应用图标与界面素材
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
