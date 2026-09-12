# -*- coding: utf-8 -*-
"""离屏冒烟测试：空白起步 + 拖动换算 + Qt 快速合成 + 导出。

运行： python _demo_smoke4.py   （QT_QPA_PLATFORM=offscreen 自动设置）
"""
import os
import sys
import time
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\pyprojects\watermark")

from PySide6.QtWidgets import QApplication, QScrollArea
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QImage

app = QApplication.instance() or QApplication([])
from pdf_watermark_demo import (DemoWindow, PageGridDialog, MockPdf,
                                make_pages, MODE_PERCENT, MODE_CM,
                                MODE_ORIGINAL, PageTile)

ROOT = Path(r"D:\pyprojects\watermark")
SAMPLE = str(ROOT / "8号线一期南沂区间消防设计说明 (3).pdf")
STAMP = str(ROOT / "章.png")
TMP = Path(tempfile.mkdtemp(prefix="wm_smoke_"))
errors = []


class FakeEv:
    """只实现 mousePressEvent/mouseMoveEvent 用到的接口。"""

    def __init__(self, x, y, btn=Qt.LeftButton):
        self._p = QPointF(x, y)
        self._b = btn

    def position(self):
        return self._p

    def button(self):
        return self._b


def qimg_bbox(a: QImage, b: QImage):
    """两图差异包围盒（检测水印是否真的在画面上移动）。"""
    w, h = min(a.width(), b.width()), min(a.height(), b.height())
    x0 = y0 = x1 = y1 = None
    for yy in range(h):
        for xx in range(w):
            if a.pixel(xx, yy) != b.pixel(xx, yy):
                if x0 is None or xx < x0:
                    x0 = xx
                if x1 is None or xx > x1:
                    x1 = xx
                if y0 is None or yy < y0:
                    y0 = yy
                if y1 is None or yy > y1:
                    y1 = yy
    return None if x0 is None else (x0, y0, x1, y1)


def bbox_center(bbox):
    if bbox is None:
        return None
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


def run():
    try:
        win = DemoWindow()
        win.show()

        # 0) 顶栏：自绘深色标题栏（含 关于/反馈 + 窗口按钮）
        assert win.title_bar.height() == 44, "标题栏高度应为 44px"
        assert hasattr(win, "_show_about") and hasattr(win, "_send_feedback")
        assert hasattr(win.title_bar, "btn_min")

        # 1) 默认空白：无文档、无水印图、无卡片
        assert win.files == [] and win._images == [], "默认应为空白"
        assert win.flow.count() == 0, f"卡片应为 0，实际 {win.flow.count()}"

        # 2) 用户添加文档与水印图（含拖放导入路径）
        win._import_paths([SAMPLE], [STAMP])
        assert len(win.files) == 1 and len(win._images) == 1, "拖放导入失败"

        # 3) 弹窗渲染（Qt 快速路径）
        dlg = PageGridDialog(win.files[0], win.refresh_cards,
                             image_paths=win._images, selected_image_index=0)
        dlg.show()
        app.processEvents()
        assert isinstance(dlg.scroll, QScrollArea)
        assert len(dlg.tiles) == 2, "应有 2 个瓦片"
        tile = dlg.tiles[0]
        assert tile._pix is not None and tile._pix.width() == PageTile.TILE_W
        assert win.files[0].pages[0].wm is False, "初始不加水印"

        # 4) 打开水印开关后显示水印
        dlg.toggle_page(0)
        app.processEvents()
        assert win.files[0].pages[0].wm is True
        pix_on = tile._pix.toImage().bits().tobytes() if tile._pix.toImage().bits() else None
        assert pix_on is not None

        # 5) 模拟左键拖动：40,10 控件像素 → 应按 pt 换算写入
        k = dlg._pt_per_px(0)
        assert k is not None and 0 < k < 50, f"换算系数异常: {k}"
        tile.mousePressEvent(FakeEv(100, 100))
        tile.mouseMoveEvent(FakeEv(140, 110))
        ox, oy = win.files[0].pages[0].image_offsets[0]
        expect = (40 * k, 10 * k)
        assert abs(ox - expect[0]) < 1e-6 and abs(oy - expect[1]) < 1e-6, \
            f"拖动偏移 {ox, oy} != 预期 {expect}"
        tile.mouseReleaseEvent(FakeEv(140, 110))
        app.processEvents()

        # 6) 拖动后再拖一次：以上次位置为基准继续移动
        tile.mousePressEvent(FakeEv(140, 110))
        tile.mouseMoveEvent(FakeEv(150, 110))
        ox2, oy2 = win.files[0].pages[0].image_offsets[0]
        assert abs((ox2 - ox) - 10 * k) < 1e-6, "第二次拖动应以上次位置为基准"
        tile.mouseReleaseEvent(FakeEv(150, 110))

        # 6b) 画面级回归：拖动必须让瓦片上的水印像素真的位移（1:1）
        pix_before = tile._pix
        tile.mousePressEvent(FakeEv(150, 110))
        tile.mouseMoveEvent(FakeEv(200, 125))      # +50, +15 控件像素
        tile.mouseReleaseEvent(FakeEv(200, 125))
        pix_after = tile._pix
        base_img = dlg._tile_geo(0)[0].toImage()
        c1 = bbox_center(qimg_bbox(base_img, pix_before.toImage()))
        c2 = bbox_center(qimg_bbox(base_img, pix_after.toImage()))
        assert c1 is not None and c2 is not None, "水印未在瓦片上绘制"
        moved = (c2[0] - c1[0], c2[1] - c1[1])
        assert abs(moved[0] - 50) < 4 and abs(moved[1] - 15) < 4, \
            f"水印画面位移 {moved} != (50, 15)，拖动未生效"

        # 7) 高频重绘性能（快速合成路径）
        t0 = time.perf_counter()
        for _ in range(30):
            dlg.rerender_tile(0)
        dt = (time.perf_counter() - t0) / 30 * 1000
        print(f"rerender_tile 平均 {dt:.1f} ms/次")
        assert dt < 80, f"快速合成过慢: {dt:.1f} ms"

        # 8) 尺寸只影响当前页
        dlg.set_current(0)
        dlg._on_percent_changed(55)
        assert win.files[0].pages[0].percent == 55.0
        assert win.files[0].pages[1].percent == 30.0, "其他页不受影响"
        dlg._on_mode_changed(dlg.mode_combo.findData(MODE_CM))
        win.files[0].pages[0].cm_w, win.files[0].pages[0].cm_h = 6.5, 3.2
        dlg.rerender_tile(0)

        # 9) 导出到临时目录（不覆盖任何现有文件）
        win._output_dir = str(TMP)
        win._export_real()
        out = TMP / ("watermarked_" + Path(SAMPLE).name)
        assert out.exists(), f"导出失败: {list(TMP.iterdir())}"
        from PyPDF2 import PdfReader
        assert len(PdfReader(str(out)).pages) == 2
        print("EXPORT_OK ->", out)

        dlg.close()
        win.close()
        print("SMOKE_OK")
    except Exception:
        errors.append(traceback.format_exc())
        print("SMOKE_FAIL")


QTimer.singleShot(100, run)
QTimer.singleShot(20000, app.quit)
app.exec()
if errors:
    print(errors[-1])
    sys.exit(1)
print("DONE")
