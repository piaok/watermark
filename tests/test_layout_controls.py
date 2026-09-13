import copy
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PIL import Image, ImageChops
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import pdf_watermark_demo as demo
from pdf_watermark_studio.core.engine import export_pdf, render_page_bg


class LayoutControlTests(unittest.TestCase):
    """The home layout panel changes every page only after explicit apply."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pdf_path = root / "two-pages.pdf"
        doc = fitz.open()
        doc.new_page(width=200, height=300)
        rotated = doc.new_page(width=200, height=300)
        rotated.set_rotation(90)
        doc.save(self.pdf_path)
        doc.close()

        self.square_stamp = root / "square.png"
        self.tall_stamp = root / "tall.png"
        Image.new("RGBA", (20, 20), (255, 0, 0, 255)).save(
            self.square_stamp)
        Image.new("RGBA", (10, 20), (255, 0, 0, 255)).save(
            self.tall_stamp)

        self.first = demo.PageState(
            wm=True, dx=11.0, dy=12.0, percent=31.0,
            image_offsets={0: (1.0, 2.0), 1: (3.0, 4.0)},
        )
        self.second = demo.PageState(
            wm=False, dx=21.0, dy=22.0, percent=32.0,
            image_offsets={0: (5.0, 6.0), 1: (7.0, 8.0)},
        )
        self.window = demo.DemoWindow()
        self.window._import_paths(
            [], [str(self.square_stamp), str(self.tall_stamp)])
        self.window.files = [demo.MockPdf(
            name=self.pdf_path.name,
            pages=[self.first, self.second],
            path=str(self.pdf_path),
        )]
        self.window._rebuild_cards()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def _select_combo_data(self, combo, value):
        index = combo.findData(value)
        self.assertGreaterEqual(index, 0, f"missing combo value {value!r}")
        combo.setCurrentIndex(index)

    def test_home_inputs_are_pending_and_percent_controls_sync_to_200(self):
        """Removing the apply boundary would overwrite per-page edits while tuning."""
        before = copy.deepcopy(self.window.files[0].pages)

        self.assertEqual(self.window.size_slider.maximum(), 200)
        self.window.size_spin.setValue(137)
        self.assertEqual(self.window.size_slider.value(), 137)
        self.window.size_slider.setValue(199)
        self.assertEqual(self.window.size_spin.value(), 199)
        self.window.opacity_slider.setValue(47)
        self.assertEqual(self.window.opacity_value.text(), "47%（仅预览）")
        self.assertIn("全部文档页面", self.window.layout_scope_label.text())
        self.assertIn("应用", self.window.apply_layout_button.text())

        self.assertEqual(self.window.files[0].pages, before)

    def test_preserve_anchor_applies_percent_and_opacity_without_moving_pages(self):
        """The default anchor must preserve every page/image offset and wm switch."""
        offsets = [copy.deepcopy(p.image_offsets)
                   for p in self.window.files[0].pages]
        positions = [(p.dx, p.dy, p.wm) for p in self.window.files[0].pages]

        self._select_combo_data(self.window.size_mode_combo, demo.MODE_PERCENT)
        self._select_combo_data(self.window.anchor_combo, demo.ANCHOR_KEEP)
        self.window.size_spin.setValue(42)
        self.window.opacity_slider.setValue(60)
        self.window.apply_layout_button.click()

        for index, page in enumerate(self.window.files[0].pages):
            self.assertEqual(page.size_mode, demo.MODE_PERCENT)
            self.assertEqual(page.percent, 42.0)
            self.assertAlmostEqual(page.opacity, 0.60)
            self.assertEqual(page.image_offsets, offsets[index])
            self.assertEqual((page.dx, page.dy, page.wm), positions[index])

    def test_fixed_width_anchor_uses_display_page_size_and_each_image_ratio(self):
        """Top-right placement must use rotated display dimensions and image height."""
        self._select_combo_data(self.window.size_mode_combo, demo.MODE_FIXED)
        self._select_combo_data(
            self.window.anchor_combo, demo.Anchor.TOP_RIGHT.value)
        self.window.fixed_width_spin.setValue(60.0)
        self.window.apply_layout_button.click()

        self.assertEqual(self.first.image_offsets,
                         {0: (58.0, -108.0), 1: (58.0, -78.0)})
        self.assertEqual(self.second.image_offsets,
                         {0: (108.0, -58.0), 1: (108.0, -28.0)})
        self.assertEqual((self.first.dx, self.first.dy), (11.0, 12.0))
        self.assertEqual((self.second.dx, self.second.dy), (21.0, 22.0))
        self.assertEqual((self.first.wm, self.second.wm), (True, False))
        for page in (self.first, self.second):
            self.assertEqual(page.size_mode, demo.MODE_FIXED)
            self.assertEqual(page.fixed_width, 60.0)

        dialog = demo.PageGridDialog(
            self.window.files[0], lambda: None,
            image_paths=self.window._images)
        try:
            dialog._sync_size_controls()
            self.assertEqual(dialog.mode_combo.currentData(), demo.MODE_FIXED)
            self.assertEqual(dialog.fixed_width_spin.value(), 60.0)
            dialog.fixed_width_spin.setValue(75.0)
            self.assertEqual(self.first.fixed_width, 75.0)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_preview_opacity_affects_pil_and_qt_but_not_export(self):
        """Preview alpha may fade the stamp while exported PDF stays fully opaque."""
        page = self.first
        page.size_mode = demo.MODE_FIXED
        page.fixed_width = 40.0
        page.opacity = 0.25
        page.dx = page.dy = 0.0
        page.image_offsets = {0: (0.0, 0.0)}
        self.window._images = [str(self.square_stamp)]

        _, _, bg = render_page_bg(self.pdf_path, 0, zoom=demo.PREVIEW_ZOOM)
        faded = demo._composite_multi(bg, page, self.window._images)
        center = (faded.width // 2, faded.height // 2)
        self.assertEqual(faded.getpixel(center)[:3], (255, 192, 192))

        dialog = demo.PageGridDialog(
            self.window.files[0], lambda: None,
            image_paths=self.window._images)
        try:
            dialog._initial_layout()
            rect = dialog._stamp_rect_px(
                page, 0, 0, str(self.square_stamp))
            image = dialog.tiles[0]._pix.toImage()
            pixel = image.pixelColor(round(rect[0] + rect[2] / 2),
                                     round(rect[1] + rect[3] / 2))
            self.assertGreaterEqual(pixel.green(), 185)
            self.assertLessEqual(pixel.green(), 198)
            self.assertGreaterEqual(pixel.blue(), 185)
            self.assertLessEqual(pixel.blue(), 198)
        finally:
            dialog.close()
            dialog.deleteLater()

        faded_out = Path(self.tmp.name) / "faded.pdf"
        opaque_out = Path(self.tmp.name) / "opaque.pdf"
        faded_layout = demo.page_layout(
            page, image_path=str(self.square_stamp))
        opaque_page = copy.deepcopy(page)
        opaque_page.opacity = 1.0
        opaque_layout = demo.page_layout(
            opaque_page, image_path=str(self.square_stamp))
        export_pdf(
            str(self.pdf_path), str(faded_out), images=[self.square_stamp],
            page_image_layouts={0: {0: faded_layout}},
        )
        export_pdf(
            str(self.pdf_path), str(opaque_out), images=[self.square_stamp],
            page_image_layouts={0: {0: opaque_layout}},
        )
        _, _, faded_export = render_page_bg(
            faded_out, 0, zoom=demo.PREVIEW_ZOOM)
        _, _, opaque_export = render_page_bg(
            opaque_out, 0, zoom=demo.PREVIEW_ZOOM)
        self.assertIsNone(ImageChops.difference(
            faded_export, opaque_export).getbbox())


if __name__ == "__main__":
    unittest.main()
