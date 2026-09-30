# -*- coding: utf-8 -*-
"""单页全屏视图回归：右键切换/左键拖动/滚轮调大小/翻页/瓦片入口。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QApplication
from reportlab.pdfgen import canvas

import pdf_watermark_demo as demo


def _make_green_stamp(path: Path) -> None:
    """绿色高辨识度水印图（green 通道检测用）。"""
    Image.new("RGBA", (120, 80), (30, 200, 40, 255)).save(path)


class FakeEv:
    """PageTile/FullScreenPageDialog 事件处理所需的鼠标事件接口。"""

    def __init__(self, x, y, btn=Qt.LeftButton):
        self._p = QPointF(x, y)
        self._b = btn

    def position(self):
        return self._p

    def button(self):
        return self._b

    def globalPosition(self):
        return self._p


class FullScreenPageDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pdf_path = self.root / "doc.pdf"
        document = canvas.Canvas(str(self.pdf_path), pagesize=(420, 297))
        for page_number in range(3):
            document.drawString(40, 200, f"page {page_number + 1}")
            document.showPage()
        document.save()
        self.stamp = self.root / "stamp.png"
        _make_green_stamp(self.stamp)
        self.changes = []

        def record_change(*_args) -> None:
            self.changes.append(True)

        self.record_change = record_change
        self.pdf = demo.MockPdf(
            name="doc.pdf",
            pages=demo.make_pages(3),
            path=str(self.pdf_path),
        )
        self.dialog = demo.FullScreenPageDialog(
            self.pdf, record_change, image_paths=[str(self.stamp)],
            selected_image_index=0, page=1,
        )
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def _page_state(self):
        return self.pdf.pages[self.dialog.current_page]

    def test_opens_on_requested_page_with_page_counter(self):
        self.assertEqual(self.dialog.current_page, 1)
        self.assertIn("2 / 3", self.dialog.page_label.text())

    def test_prev_next_buttons_navigate_and_disable_at_edges(self):
        self.assertTrue(self.dialog.btn_prev.isEnabled())
        self.assertTrue(self.dialog.btn_next.isEnabled())
        self.dialog.btn_next.click()
        self.assertEqual(self.dialog.current_page, 2)
        self.assertFalse(self.dialog.btn_next.isEnabled())
        self.assertTrue(self.dialog.btn_prev.isEnabled())
        self.dialog.btn_prev.click()
        self.dialog.btn_prev.click()
        self.assertEqual(self.dialog.current_page, 0)
        self.assertFalse(self.dialog.btn_prev.isEnabled())
        self.assertTrue(self.dialog.btn_next.isEnabled())

    def test_right_click_inside_page_toggles_stamp(self):
        before = self._page_state().wm
        rect = self.dialog._page_rect()
        self.dialog.mousePressEvent(FakeEv(
            rect.center().x(), rect.center().y(), Qt.RightButton))
        self.app.processEvents()
        self.assertEqual(self._page_state().wm, not before)
        self.assertTrue(self.changes)

    def test_right_click_outside_page_closes(self):
        rect = self.dialog._page_rect()
        self.dialog.mousePressEvent(FakeEv(2, 2, Qt.RightButton))
        self.assertFalse(self.dialog.isVisible())

    def test_left_drag_moves_stamp_offset_in_pt(self):
        st = self._page_state()
        st.wm = True
        self.dialog._rerender()
        rect = self.dialog._page_rect()
        k = self.dialog._pt_per_px()
        self.assertGreater(k, 0.0)
        self.dialog.mousePressEvent(FakeEv(
            rect.center().x(), rect.center().y()))
        self.dialog.mouseMoveEvent(FakeEv(
            rect.center().x() + 40, rect.center().y() + 15))
        self.dialog.mouseReleaseEvent(FakeEv(
            rect.center().x() + 40, rect.center().y() + 15))
        ox, oy = st.image_offsets[0]
        self.assertAlmostEqual(ox, 40 * k, delta=0.5)
        self.assertAlmostEqual(oy, 15 * k, delta=0.5)

    def test_wheel_adjusts_percent_with_clamp(self):
        st = self._page_state()
        st.wm = True
        st.size_mode = demo.MODE_PERCENT
        st.percent = 30.0
        self.dialog.adjust_stamp_size(3)
        self.assertAlmostEqual(st.percent, 36.0)
        self.dialog.adjust_stamp_size(-50)
        self.assertAlmostEqual(st.percent, 1.0)

    def test_wheel_adjusts_cm_and_fixed_modes(self):
        st = self._page_state()
        st.wm = True
        st.size_mode = demo.MODE_CM
        st.cm_w = st.cm_h = 5.0
        self.dialog.adjust_stamp_size(2)
        self.assertAlmostEqual(st.cm_w, 5.4)
        self.assertAlmostEqual(st.cm_h, 5.4)
        st.size_mode = demo.MODE_FIXED
        st.fixed_width = 180.0
        self.dialog.adjust_stamp_size(-2)
        self.assertAlmostEqual(st.fixed_width, 160.0)

    def test_wheel_ignored_without_stamp(self):
        st = self._page_state()
        st.wm = False
        st.percent = 30.0
        self.dialog.adjust_stamp_size(5)
        self.assertAlmostEqual(st.percent, 30.0)

    def test_display_cache_stays_clean_after_multiple_rerenders(self):
        """Painting onto the cached base pixmap would smear the stamp on every drag/wheel frame."""
        st = self._page_state()
        st.wm = True
        for _ in range(5):
            self.dialog._rerender()
        img = self.dialog._display_base_pix().toImage()
        reds = 0
        for y in range(0, img.height(), 5):
            for x in range(0, img.width(), 5):
                c = img.pixelColor(x, y)
                if c.red() > 150 and c.green() < 110 and c.blue() < 110:
                    reds += 1
        self.assertEqual(
            reds, 0,
            "cached background must never contain stamp pixels; "
            "each rerender must start from a clean base",
        )

    def test_repeated_wheel_steps_change_size_exactly_once_per_step(self):
        """Re-compositing on a tainted cache would double every wheel step."""
        st = self._page_state()
        st.wm = True
        st.size_mode = demo.MODE_PERCENT
        st.percent = 30.0
        self.dialog.adjust_stamp_size(1)
        self.assertAlmostEqual(st.percent, 32.0)
        self.dialog.adjust_stamp_size(1)
        self.assertAlmostEqual(st.percent, 34.0)

    def test_stamp_is_painted_in_page_bitmap_coordinates(self):
        """Painting window coords onto the page bitmap displaces and clips the stamp."""
        st = self._page_state()
        st.wm = True
        st.size_mode = demo.MODE_PERCENT
        st.percent = 50.0
        rect = self.dialog._page_rect()
        k = self.dialog._pt_per_px()
        # 拖到中心右下 (+120px, +60px)
        self.dialog.mousePressEvent(FakeEv(
            rect.center().x(), rect.center().y()))
        self.dialog.mouseMoveEvent(FakeEv(
            rect.center().x() + 120, rect.center().y() + 60))
        self.dialog.mouseReleaseEvent(FakeEv(
            rect.center().x() + 120, rect.center().y() + 60))
        self.dialog._rerender()
        img = self.dialog._pix.toImage()

        def green_bbox():
            minx, miny, maxx, maxy = img.width(), img.height(), -1, -1
            for y in range(0, img.height(), 2):
                for x in range(0, img.width(), 2):
                    c = img.pixelColor(x, y)
                    if c.green() > 120 and c.red() < 100 and c.blue() < 100:
                        minx, miny = min(minx, x), min(miny, y)
                        maxx, maxy = max(maxx, x), max(maxy, y)
            return (minx, miny, maxx, maxy)

        # 期望：图章画在「页面位图坐标」里 —— 窗口矩形减去页面原点
        view = self.dialog._stamp_rect_view(st, self.dialog.current_page,
                                            0, str(self.stamp))
        expect = (view[0] - rect.x(), view[1] - rect.y(), view[2], view[3])
        bbox = green_bbox()
        self.assertGreater(bbox[2], 0, "stamp must be painted on the page bitmap")
        self.assertAlmostEqual(bbox[0], expect[0], delta=3)
        self.assertAlmostEqual(bbox[1], expect[1], delta=3)
        # 宽度完整性：画满期望宽度（不被页面位图边界错误裁剪）
        painted_w = bbox[2] - bbox[0]
        self.assertGreater(painted_w, expect[2] * 0.9,
                           f"stamp clipped to {painted_w}px of {expect[2]:.0f}px")

    def test_page_tile_fullscreen_chip_opens_fullscreen_view(self):
        grid = demo.PageGridDialog(
            self.pdf, self.changes.append, image_paths=[str(self.stamp)])
        grid.show()
        self.app.processEvents()
        with patch.object(demo, "FullScreenPageDialog") as factory:
            factory.return_value.exec.return_value = 0
            grid.open_fullscreen(2)
            factory.assert_called_once()
            self.assertEqual(factory.call_args.kwargs.get("page"), 2)
        grid.close()
        grid.deleteLater()
        self.app.processEvents()

    def test_page_tile_chip_hit_area_is_in_tile(self):
        grid = demo.PageGridDialog(
            self.pdf, self.changes.append, image_paths=[str(self.stamp)])
        grid.show()
        self.app.processEvents()
        tile = grid.tiles[0]
        chip = tile._fullscreen_chip_rect()
        self.assertTrue(chip.isValid() and chip.width() > 0)
        self.assertTrue(chip.right() <= tile.width())
        self.assertTrue(chip.top() >= 0)
        grid.close()
        grid.deleteLater()
        self.app.processEvents()

    # ---- 悬浮翻页按钮尺寸 ----
    def test_side_buttons_have_room_for_text(self):
        """A ~30px floating button clipped 上一页/下一页 vertically."""
        fm = self.dialog.btn_prev.fontMetrics()
        text_w = max(fm.horizontalAdvance(self.dialog.btn_prev.text()),
                     fm.horizontalAdvance(self.dialog.btn_next.text()))
        for btn in (self.dialog.btn_prev, self.dialog.btn_next):
            self.assertGreaterEqual(btn.height(), 38,
                                    f"{btn.text()} 高度 {btn.height()}px 放不下文字")
            self.assertGreaterEqual(btn.width(), text_w + 28,
                                    f"{btn.text()} 宽度 {btn.width()}px 放不下文字")

    # ---- 页面视图：左键点击页面进入全屏 ----
    def test_tile_left_click_without_drag_opens_fullscreen(self):
        grid = demo.PageGridDialog(
            self.pdf, self.record_change, image_paths=[str(self.stamp)])
        grid.show()
        self.app.processEvents()
        tile = grid.tiles[1]
        # 注意：不能用 MagicMock 替换 Qt 方法（PySide6 构造实例时崩溃），
        # 必须用普通函数。
        called = []

        def fake_exec(self):
            called.append(True)
            return 0

        with patch.object(demo.FullScreenPageDialog, "exec", fake_exec):
            tile.mousePressEvent(FakeEv(100, 100))
            tile.mouseReleaseEvent(FakeEv(100, 100))
        self.assertEqual(len(called), 1)
        self.assertEqual(grid.current_page, 1)
        grid.close()
        grid.deleteLater()
        self.app.processEvents()

    def test_tile_left_drag_still_moves_stamp_without_fullscreen(self):
        grid = demo.PageGridDialog(
            self.pdf, self.record_change, image_paths=[str(self.stamp)])
        grid.show()
        self.app.processEvents()
        tile = grid.tiles[0]
        k = grid._pt_per_px(0)
        st = self.pdf.pages[0]
        st.wm = True
        called = []

        def fake_exec(self):
            called.append(True)
            return 0

        with patch.object(demo.FullScreenPageDialog, "exec", fake_exec):
            tile.mousePressEvent(FakeEv(100, 100))
            tile.mouseMoveEvent(FakeEv(140, 110))
            tile.mouseReleaseEvent(FakeEv(140, 110))
        self.assertEqual(len(called), 0)
        ox, oy = st.image_offsets[0]
        self.assertAlmostEqual(ox, 40 * k, delta=0.5)
        grid.close()
        grid.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
