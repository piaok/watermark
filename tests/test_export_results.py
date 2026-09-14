import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication
from reportlab.pdfgen import canvas
from shiboken6 import isValid

import pdf_watermark_demo as demo
from pdf_watermark_studio.export_worker import (
    BatchResult,
    ExportFailure,
    ExportSuccess,
)


class ExportResultUiTests(unittest.TestCase):
    """The completed batch result must remain inspectable from the main window."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir()
        self.stamp = self.root / "stamp.png"
        Image.new("RGBA", (24, 12), (210, 30, 40, 255)).save(self.stamp)
        self.window = demo.DemoWindow()
        self.window._output_dir = str(self.output_dir)
        self.window.out_dir_label.setText(str(self.output_dir))

    def tearDown(self):
        if self.window._export_active:
            self.window._cancel_export()
            self._wait_until(lambda: not self.window._export_active)
        details = getattr(self.window, "_export_details_dialog", None)
        if details is not None:
            details.close()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def _make_pdf(self, name: str) -> Path:
        path = self.root / name
        document = canvas.Canvas(str(path), pagesize=(180, 120))
        document.drawString(20, 80, name)
        document.showPage()
        document.save()
        return path

    def _set_jobs(self, paths: list[Path]) -> None:
        self.window._images = [str(self.stamp)]
        self.window.img_list.clear()
        self.window.img_list.addItem(self.stamp.name)
        self.window.files = [
            demo.MockPdf(
                name=path.name,
                path=str(path),
                pages=[demo.PageState(wm=True)],
            )
            for path in paths
        ]
        self.window._rebuild_cards()

    def _wait_until(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.app.processEvents()
        return bool(predicate())

    def _details_text(self) -> str:
        self.window.btn_export_details.click()
        self.app.processEvents()
        return self.window._export_details_text.toPlainText()

    def test_partial_failure_retains_every_name_error_and_success_path(self):
        """Replacing detailed outcomes with the final status would hide actionable failures."""
        names = [
            "first-success-with-a-long-name.pdf",
            "middle-failure-with-a-long-name.pdf",
            "last-success-with-a-long-name.pdf",
        ]
        sources = [self._make_pdf(name) for name in names]
        self._set_jobs(sources)

        def mixed_export(source, output_dir, **_kwargs):
            if Path(source).name == names[1]:
                raise RuntimeError("permission denied while publishing the completed PDF")
            output = Path(output_dir) / f"actual-result-{Path(source).name}"
            output.write_bytes(b"exported")
            return output

        self.window._exporter = mixed_export
        self.window._export_real()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))

        self.assertEqual(
            self.window.status_label.text(),
            "导出完成：成功 2/3，失败 1，未处理 0",
        )
        self.assertTrue(self.window.btn_export_details.isEnabled())
        details = self._details_text()
        self.assertIn("成功：2", details)
        self.assertIn("失败：1", details)
        self.assertIn("未处理：0", details)
        self.assertIn(names[0], details)
        self.assertIn(str(self.output_dir / f"actual-result-{names[0]}"), details)
        self.assertIn(names[1], details)
        self.assertIn("permission denied while publishing the completed PDF", details)
        self.assertIn(names[2], details)

    def test_details_show_actual_unique_output_suffix(self):
        """Guessing watermarked_<name> would report the wrong path after a collision."""
        source = self._make_pdf("collision.pdf")
        self._set_jobs([source])
        existing = self.output_dir / "watermarked_collision.pdf"
        existing.write_bytes(b"existing")

        self.window._export_real()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active, 8))

        actual_path = self.window._last_export_result.successes[0].output_path
        self.assertEqual(Path(actual_path).name, "watermarked_collision_2.pdf")
        self.assertIn(actual_path, self._details_text())

    def test_cancelled_result_reports_unprocessed_and_never_becomes_success(self):
        """Finishing the active file after cancellation must preserve cancelled status."""
        sources = [self._make_pdf(f"cancel-{index}.pdf") for index in range(3)]
        self._set_jobs(sources)
        entered = threading.Event()
        release = threading.Event()

        def controlled_export(source, output_dir, **_kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release exporter")
            output = Path(output_dir) / f"done-{Path(source).name}"
            output.write_bytes(b"exported")
            return output

        self.window._exporter = controlled_export
        self.window._export_real()
        self.assertTrue(self._wait_until(entered.is_set))
        self.window._cancel_export()
        release.set()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))

        self.assertEqual(
            self.window.status_label.text(),
            "已取消：成功 1/3，失败 0，未处理 2",
        )
        details = self._details_text()
        self.assertIn("状态：已取消", details)
        self.assertIn("未处理：2", details)

        completed_but_cancelled = BatchResult(
            total=1,
            successes=(ExportSuccess("late.pdf", "C:/exports/late.pdf"),),
            failures=(),
            cancelled=True,
        )
        self.window._on_export_worker_finished(completed_but_cancelled)
        self.assertEqual(
            self.window.status_label.text(),
            "已取消：成功 1/1，失败 0，未处理 0",
        )

    def test_terminal_summary_distinguishes_success_partial_failure_and_all_failed(self):
        """One generic completion prefix would conceal whether the batch needs attention."""
        cases = (
            (
                BatchResult(1, (ExportSuccess("ok.pdf", "C:/ok.pdf"),), (), False),
                "导出完成：全部成功 1/1，未处理 0",
            ),
            (
                BatchResult(
                    2,
                    (ExportSuccess("ok.pdf", "C:/ok.pdf"),),
                    (ExportFailure("bad.pdf", "boom"),),
                    False,
                ),
                "导出完成：成功 1/2，失败 1，未处理 0",
            ),
            (
                BatchResult(1, (), (ExportFailure("bad.pdf", "boom"),), False),
                "导出失败：成功 0/1，失败 1，未处理 0",
            ),
        )
        for result, expected in cases:
            with self.subTest(expected=expected):
                self.window._on_export_worker_finished(result)
                self.assertEqual(self.window.status_label.text(), expected)

    def test_export_summary_wraps_within_the_sidebar(self):
        """Long counts must not force the fixed-width export panel wider."""
        self.assertTrue(self.window.status_label.wordWrap())

    def test_thread_start_failure_is_available_in_details(self):
        """A pre-run thread failure must use the same reopenable result path."""
        source = self._make_pdf("thread-start-failure.pdf")
        self._set_jobs([source])

        with patch.object(demo.QThread, "start",
                          side_effect=RuntimeError("worker could not start")):
            self.window._export_real()
        self.app.processEvents()

        self.assertFalse(self.window._export_active)
        self.assertTrue(self.window.btn_export_details.isEnabled())
        details = self._details_text()
        self.assertIn("状态：全部失败", details)
        self.assertIn("导出任务", details)
        self.assertIn("worker could not start", details)

    def test_new_export_clears_open_details_then_reopen_preserves_new_result(self):
        """A second job must not expose stale results while active or after completion."""
        first = self._make_pdf("old-result.pdf")
        self._set_jobs([first])

        def finish(source, output_dir, **_kwargs):
            output = Path(output_dir) / f"shown-{Path(source).name}"
            output.write_bytes(b"exported")
            return output

        self.window._exporter = finish
        self.window._export_real()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))
        old_text = self._details_text()
        old_dialog = self.window._export_details_dialog
        self.assertIn("old-result.pdf", old_text)

        second = self._make_pdf("new-result.pdf")
        self._set_jobs([second])
        entered = threading.Event()
        release = threading.Event()

        def delayed(source, output_dir, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release exporter")
            return finish(source, output_dir, **kwargs)

        self.window._exporter = delayed
        self.window._export_real()
        self.assertTrue(self._wait_until(entered.is_set))
        self.assertIsNone(self.window._last_export_result)
        self.assertFalse(self.window.btn_export_details.isEnabled())
        self.assertIsNone(self.window._export_details_dialog)
        self.assertTrue(not isValid(old_dialog) or not old_dialog.isVisible())

        release.set()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))
        new_text = self._details_text()
        self.assertIn("new-result.pdf", new_text)
        self.assertNotIn("old-result.pdf", new_text)

        first_new_dialog = self.window._export_details_dialog
        first_new_dialog.close()
        self.app.processEvents()
        self.assertIsNone(self.window._export_details_dialog)
        reopened_text = self._details_text()
        self.assertEqual(reopened_text, new_text)
        self.assertIsNot(self.window._export_details_dialog, first_new_dialog)


if __name__ == "__main__":
    unittest.main()
