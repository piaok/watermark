"""帮助/反馈弹窗的文案与交互回归测试（离屏）。"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QPushButton

from pdf_watermark_demo import CONTACT_PHONE, HTCAPTION, DemoWindow


class TitleBarEntryTests(unittest.TestCase):
    """顶栏必须暴露「帮助/反馈」两个入口。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = DemoWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_title_bar_exposes_help_and_feedback_entries(self):
        texts = [b.text() for b in self.window.title_bar.findChildren(
            QPushButton)]
        self.assertIn("帮助", texts)
        self.assertIn("反馈", texts)
        self.assertNotIn("关于", texts)

    def test_every_title_bar_button_stays_client_area(self):
        """HTCAPTION 会让系统把点击当拖动吞掉，按钮就点不动了。"""
        self.window.show()
        self.app.processEvents()
        buttons = self.window.title_bar.findChildren(QAbstractButton)
        self.assertTrue(buttons)
        for btn in buttons:
            center = btn.mapTo(self.window, btn.rect().center())
            self.assertNotEqual(
                self.window._hit_test(center), HTCAPTION,
                f"标题栏按钮「{btn.text()}」落在 HTCAPTION 区域，物理点击会失效")

    def test_title_bar_blank_area_still_drags_window(self):
        """排除按钮不能顺手把拖动区也放开。"""
        self.window.show()
        self.app.processEvents()
        bar = self.window.title_bar
        blank = QPoint(bar.width() // 2 - 60, bar.height() // 2)
        self.assertEqual(self.window._hit_test(blank), HTCAPTION)


class HelpDialogTests(unittest.TestCase):
    """帮助弹窗需覆盖首页与文档详情的四项鼠标操作说明。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = DemoWindow()
        self.dialog = self.window._build_help_dialog()
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _text(self) -> str:
        return "\n".join(lab.text() for lab in self.dialog.findChildren(QLabel))

    def test_help_dialog_documents_all_four_interactions(self):
        text = self._text()
        self.assertIn("文档缩略图", text)
        self.assertIn("进入文档详情", text)
        self.assertIn("添加本页图章", text)
        self.assertIn("删除本页图章", text)
        self.assertIn("按住鼠标左键", text)
        self.assertIn("移动图章", text)
        self.assertIn("空白位置点击鼠标右键", text)
        self.assertIn("返回首页", text)


class FeedbackDialogTests(unittest.TestCase):
    """反馈弹窗需给出开发商与联系电话，并支持一键复制。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = DemoWindow()
        self.dialog = self.window._build_feedback_dialog()
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_feedback_dialog_shows_developer_and_phone(self):
        text = "\n".join(lab.text() for lab in self.dialog.findChildren(QLabel))
        self.assertIn("野草智能", text)
        self.assertIn(CONTACT_PHONE, text)

    def test_copy_button_puts_phone_on_clipboard(self):
        buttons = {b.text(): b for b in self.dialog.findChildren(QPushButton)}
        self.assertIn("复制手机号", buttons)
        QApplication.clipboard().setText("")
        buttons["复制手机号"].click()
        self.assertEqual(QApplication.clipboard().text(), CONTACT_PHONE)


if __name__ == "__main__":
    unittest.main()
