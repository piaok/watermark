"""导出目录选择：区分「取消」与「确定却拿不到路径」（离屏）。

静态方法 QFileDialog.getExistingDirectory() 两种情况都返回空串，用户点了确定
却什么都没发生。_pick_dir 必须把后者识别出来并换内建对话框重试。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog

from pdf_watermark_demo import DemoWindow


class FakeInputDialog:
    """QInputDialog 替身：返回预设的 (文本, 是否确定)。"""

    scripted: tuple = ("", False)
    created: list = []

    @classmethod
    def reset(cls, scripted) -> None:
        cls.scripted = scripted
        cls.created = []

    @staticmethod
    def getText(parent, title, label, mode, text=""):
        FakeInputDialog.created.append(
            {"title": title, "label": label, "initial": text})
        return FakeInputDialog.scripted


class FakeFileDialog:
    """QFileDialog 替身：按 scripted 顺序返回 (是否确定, 选中的路径)。"""

    class FileMode:
        Directory = "Directory"

    class Option:
        ShowDirsOnly = "ShowDirsOnly"
        DontUseNativeDialog = "DontUseNativeDialog"

    scripted: list = []
    created: list = []

    @classmethod
    def reset(cls, scripted) -> None:
        cls.scripted = list(scripted)
        cls.created = []

    def __init__(self, parent=None, title=""):
        self.title = title
        self.options = {}
        self.directory = ""
        self.file_mode = None
        self._outcome = FakeFileDialog.scripted[len(FakeFileDialog.created)]
        FakeFileDialog.created.append(self)

    def setFileMode(self, mode) -> None:
        self.file_mode = mode

    def setOption(self, option, value=True) -> None:
        self.options[option] = value

    def setDirectory(self, directory) -> None:
        self.directory = directory

    def deleteLater(self) -> None:
        self.deleted = True

    def exec(self) -> int:
        return (QDialog.DialogCode.Accepted if self._outcome[0]
                else QDialog.DialogCode.Rejected)

    def selectedFiles(self) -> list:
        return list(self._outcome[1])


class DirectoryPickerTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = DemoWindow()
        self.target = tempfile.TemporaryDirectory()
        FakeFileDialog.reset([])

    def tearDown(self):
        self.target.cleanup()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _pick(self, scripted):
        FakeFileDialog.reset(scripted)
        with patch("pdf_watermark_demo.QFileDialog", FakeFileDialog):
            self.window._pick_dir()
        return FakeFileDialog.created

    def test_accept_without_path_retries_with_builtin_dialog(self):
        """原生对话框确定后返回空，必须自动换内建对话框再选一次。"""
        dialogs = self._pick([(True, []), (True, [self.target.name])])
        self.assertEqual(len(dialogs), 2)
        self.assertIsNot(dialogs[1].options.get(
            FakeFileDialog.Option.DontUseNativeDialog), False)
        self.assertEqual(self.window._output_dir,
                         os.path.normpath(self.target.name))
        self.assertEqual(self.window.out_dir_label.text(),
                         os.path.normpath(self.target.name))

    def test_cancel_keeps_state_and_opens_no_second_dialog(self):
        """取消是正常操作：不重试、不报错、不改动已有选择。"""
        dialogs = self._pick([(False, [])])
        self.assertEqual(len(dialogs), 1)
        self.assertEqual(self.window._output_dir, "")
        self.assertEqual(self.window.out_dir_label.text(), "未选择输出目录")
        self.assertEqual(self.window.status_label.text(), "就绪")

    def test_two_empty_results_leave_a_hint(self):
        """两次都拿不到路径时要说明原因，不能静默失败。"""
        dialogs = self._pick([(True, []), (True, [])])
        self.assertEqual(len(dialogs), 2)
        self.assertEqual(self.window._output_dir, "")
        self.assertIn("没有取到目录路径", self.window.status_label.text())

    def test_non_directory_selection_is_rejected(self):
        """返回的不是真实目录时不能当成有效选择。"""
        self._pick([(True, [os.path.join(self.target.name, "nope")]),
                    (False, [])])
        self.assertEqual(self.window._output_dir, "")

    def test_dialog_starts_in_an_existing_directory(self):
        """起始目录必须真实存在，避免落到 Qt 记住的陈旧位置上。"""
        dialogs = self._pick([(False, [])])
        self.assertTrue(dialogs[0].directory)
        self.assertTrue(os.path.isdir(dialogs[0].directory))

    def test_second_pick_starts_from_previous_choice(self):
        self._pick([(True, [self.target.name])])
        dialogs = self._pick([(False, [])])
        self.assertEqual(dialogs[0].directory, os.path.normpath(
            self.target.name))

    def test_broken_native_dialog_is_skipped_after_first_failure(self):
        """原生对话框一旦确定后返回空，本次运行内不再让它挡在前面。"""
        self._pick([(True, []), (True, [self.target.name])])
        dialogs = self._pick([(False, [])])
        self.assertEqual(len(dialogs), 1)
        self.assertTrue(dialogs[0].options.get(
            FakeFileDialog.Option.DontUseNativeDialog))

    def test_cancel_with_stale_selection_is_still_a_cancel(self):
        """真实 Qt 取消后 selectedFiles() 仍会返回当前目录，不能当选中。"""
        dialogs = self._pick([(False, [str(self.target.name)])])
        self.assertEqual(len(dialogs), 1)
        self.assertEqual(self.window._output_dir, "")
        self.assertEqual(self.window.out_dir_label.text(), "未选择输出目录")

    def test_cancel_in_builtin_dialog_is_still_a_plain_cancel(self):
        """在内建对话框里取消仍是普通取消：不报错、不改动已有状态。"""
        self._pick([(True, []), (False, [])])
        self.assertEqual(self.window._output_dir, "")
        self.assertEqual(self.window.out_dir_label.text(), "未选择输出目录")
        self.assertEqual(self.window.status_label.text(), "就绪")


class ManualDirectoryEntryTests(unittest.TestCase):
    """选择框不可用时的兜底：手动填写输出目录。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = DemoWindow()
        self.target = tempfile.TemporaryDirectory()
        FakeInputDialog.reset(("", False))

    def tearDown(self):
        self.target.cleanup()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _type(self, text, accepted=True):
        FakeInputDialog.reset((text, accepted))
        with patch("pdf_watermark_demo.QInputDialog", FakeInputDialog):
            self.window._type_dir()
        return FakeInputDialog.created

    def test_manual_path_is_accepted(self):
        self._type(self.target.name)
        self.assertEqual(self.window._output_dir,
                         os.path.normpath(self.target.name))
        self.assertEqual(self.window.out_dir_label.text(),
                         os.path.normpath(self.target.name))

    def test_pasted_quoted_path_is_accepted(self):
        """从资源管理器「复制文件地址」粘来的路径带引号，也要能用。"""
        self._type(f'"{self.target.name}"')
        self.assertEqual(self.window._output_dir,
                         os.path.normpath(self.target.name))

    def test_missing_directory_reports_and_keeps_previous(self):
        self._type(self.target.name)
        self._type(os.path.join(self.target.name, "nope"))
        self.assertEqual(self.window._output_dir,
                         os.path.normpath(self.target.name))
        self.assertIn("目录不存在", self.window.status_label.text())

    def test_cancel_changes_nothing(self):
        created = self._type(self.target.name, accepted=False)
        self.assertEqual(len(created), 1)
        self.assertEqual(self.window._output_dir, "")
        self.assertEqual(self.window.status_label.text(), "就绪")

    def test_double_click_on_path_label_opens_manual_entry(self):
        """路径标签双击要能唤起手动输入（事件过滤器接线正确）。"""
        FakeInputDialog.reset((self.target.name, True))
        event = QMouseEvent(
            QEvent.Type.MouseButtonDblClick, QPointF(5, 5),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        with patch("pdf_watermark_demo.QInputDialog", FakeInputDialog):
            self.app.sendEvent(self.window.out_dir_label, event)
        self.assertEqual(len(FakeInputDialog.created), 1)
        self.assertEqual(self.window._output_dir,
                         os.path.normpath(self.target.name))


if __name__ == "__main__":
    unittest.main()
