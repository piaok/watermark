# -*- coding: utf-8 -*-
"""「打开输出文件夹」按钮回归：位置在导出详情之前、状态联动、点击行为。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import pdf_watermark_demo as demo


class OpenOutputDirTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.window = demo.DemoWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_open_button_precedes_details_button(self):
        """按钮必须排在「导出详情」之前（用户明确要求的顺序）。"""
        details = self.window.btn_export_details
        layout = details.parentWidget().layout()
        indexes = {}
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if widget is self.window.btn_open_dir:
                indexes["open"] = index
            elif widget is details:
                indexes["details"] = index
        self.assertIn("open", indexes)
        self.assertIn("details", indexes)
        self.assertLess(indexes["open"], indexes["details"])

    def test_disabled_without_output_dir_and_enabled_after_valid_selection(self):
        self.assertFalse(self.window.btn_open_dir.isEnabled())
        self.assertTrue(self.window._apply_output_dir(str(self.root)))
        self.assertTrue(self.window.btn_open_dir.isEnabled())
        self.assertFalse(
            self.window._apply_output_dir(str(self.root / "nope")))
        # 失败不改已有选择：按钮状态保持
        self.assertTrue(self.window.btn_open_dir.isEnabled())

    def test_click_opens_system_file_manager_at_output_dir(self):
        self.window._apply_output_dir(str(self.root))
        with patch.object(demo.os, "startfile") as startfile:
            self.window.btn_open_dir.click()
            startfile.assert_called_once_with(str(self.root))
        self.assertNotIn("无法", self.window.status_label.text())

    def test_click_without_valid_dir_does_nothing_but_reports(self):
        with patch.object(demo.os, "startfile") as startfile:
            self.window._open_output_dir()
            startfile.assert_not_called()
        self.assertIn("输出目录", self.window.status_label.text())

    def test_click_failure_reports_error_and_keeps_button_enabled(self):
        self.window._apply_output_dir(str(self.root))
        with patch.object(demo.os, "startfile",
                          side_effect=OSError("no shell")):
            self.window.btn_open_dir.click()
        self.assertIn("无法打开输出目录", self.window.status_label.text())
        self.assertTrue(self.window.btn_open_dir.isEnabled())


if __name__ == "__main__":
    unittest.main()
