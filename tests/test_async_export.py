import gc
import os
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PyPDF2 import PdfReader
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication
from reportlab.pdfgen import canvas
from shiboken6 import isValid

import pdf_watermark_demo as demo


class AsyncExportTests(unittest.TestCase):
    """Background export must keep all Qt work on the responsive GUI thread."""

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
        cancel = getattr(self.window, "_cancel_export", None)
        if getattr(self.window, "_export_active", False) and cancel is not None:
            cancel()
            self._wait_until(
                lambda: not getattr(self.window, "_export_active", False),
                timeout=3,
            )
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def _make_pdf(self, name: str, pages: int = 1) -> Path:
        path = self.root / name
        document = canvas.Canvas(str(path), pagesize=(180, 120))
        for page_number in range(pages):
            document.drawString(20, 80, f"page {page_number + 1}")
            document.showPage()
        document.save()
        return path

    def _set_jobs(self, paths: list[Path], *, state=None) -> None:
        self.window._images = [str(self.stamp)]
        self.window.img_list.clear()
        self.window.img_list.addItem(self.stamp.name)
        self.window.files = [
            demo.MockPdf(
                name=path.name,
                path=str(path),
                pages=[state or demo.PageState(wm=True)],
            )
            for path in paths
        ]
        self.window._rebuild_cards()

    def _wait_until(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.app.processEvents()
        return bool(predicate())

    @staticmethod
    def _finish_fake_export(source, output_dir, **kwargs):
        progress = kwargs.get("progress_cb")
        if progress is not None:
            progress(0, 1)
        result = Path(output_dir) / f"fake_{Path(source).name}"
        result.write_bytes(b"completed")
        return result

    def test_slow_export_keeps_timer_responsive_and_blocks_all_editing(self):
        """Moving export back to the GUI thread would freeze timers and allow re-entry."""
        source = self._make_pdf("slow.pdf")
        self._set_jobs([source])
        entered = threading.Event()
        release = threading.Event()

        def slow_export(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release exporter")
            return self._finish_fake_export(*args, **kwargs)

        self.window._exporter = slow_export
        timer_fired = []
        QTimer.singleShot(20, lambda: timer_fired.append(True))

        started = time.monotonic()
        self.window._export_real()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(self._wait_until(entered.is_set))
        self.assertTrue(self._wait_until(lambda: bool(timer_fired)))
        self.assertTrue(self.window._export_active)
        first_thread = self.window._export_thread
        self.assertTrue(self.window.btn_export.isEnabled())
        self.assertIn("取消", self.window.btn_export.text())
        self.assertTrue(all(not control.isEnabled()
                            for control in self.window._export_edit_controls))
        self.assertFalse(self.window.acceptDrops())

        before_files = list(self.window.files)
        before_images = list(self.window._images)
        self.window._import_paths([], [str(self.root / "blocked.png")])
        self.window._apply_layout_to_all_pages()
        self.window._pick_dir()
        self.window._export_real()
        self.assertEqual(self.window.files, before_files)
        self.assertEqual(self.window._images, before_images)
        self.assertIs(self.window._export_thread, first_thread)

        release.set()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))
        self.assertTrue(all(control.isEnabled()
                            for control in self.window._export_edit_controls))
        self.assertTrue(self.window.acceptDrops())
        self.assertEqual(self.window.btn_export.text(), "导出全部")
        self.assertEqual(len(self.window._last_export_result.successes), 1)

        previous = self.window._last_export_result
        self.window._exporter = self._finish_fake_export
        self.window._export_real()
        self.assertIsNone(self.window._last_export_result)
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))
        self.assertIsNot(self.window._last_export_result, previous)
        self.assertEqual(len(self.window._last_export_result.successes), 1)

    def test_worker_uses_deep_snapshot_of_pages_and_image_paths(self):
        """Reading live window state in the worker would observe mutations after start."""
        source = self._make_pdf("snapshot.pdf")
        state = demo.PageState(
            wm=True,
            dx=1.0,
            dy=2.0,
            size_mode=demo.MODE_FIXED,
            fixed_width=77.0,
            opacity=0.25,
            image_offsets={0: (13.0, 14.0)},
        )
        self._set_jobs([source], state=state)
        entered = threading.Event()
        release = threading.Event()
        captured = []

        def inspect_snapshot(source_path, output_dir, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release exporter")
            captured.append((source_path, kwargs))
            return self._finish_fake_export(source_path, output_dir, **kwargs)

        self.window._exporter = inspect_snapshot
        self.window._export_real()
        self.assertTrue(self._wait_until(entered.is_set))

        state.fixed_width = 999.0
        state.opacity = 0.01
        state.image_offsets[0] = (90.0, 91.0)
        self.window._images[0] = str(self.root / "replacement.png")
        release.set()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))

        _, kwargs = captured[0]
        self.assertEqual(kwargs["images"], [self.stamp])
        layout = kwargs["page_image_layouts"][0][0]
        self.assertEqual(layout.fixed_width, 77.0)
        self.assertEqual((layout.dx, layout.dy), (13.0, 14.0))
        self.assertEqual(layout.opacity, 1.0)

    def test_cancel_finishes_current_file_and_skips_remaining_files(self):
        """Checking cancellation inside a file could publish a partial PDF."""
        sources = [self._make_pdf(f"source-{index}.pdf") for index in range(3)]
        self._set_jobs(sources)
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def controlled_export(source, output_dir, **kwargs):
            calls.append(Path(source).name)
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release exporter")
            return self._finish_fake_export(source, output_dir, **kwargs)

        self.window._exporter = controlled_export
        self.window._export_real()
        self.assertTrue(self._wait_until(entered.is_set))
        self.window._cancel_export()
        release.set()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))

        result = self.window._last_export_result
        self.assertEqual(calls, ["source-0.pdf"])
        self.assertEqual(result.total, 3)
        self.assertEqual(len(result.successes), 1)
        self.assertEqual(len(result.failures), 0)
        self.assertTrue(result.cancelled)

    def test_cancelling_during_last_file_is_reported_as_cancelled(self):
        """A loop exhausted after the active file must not erase a late cancel request."""
        source = self._make_pdf("last.pdf")
        self._set_jobs([source])
        entered = threading.Event()
        release = threading.Event()

        def controlled_export(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release exporter")
            return self._finish_fake_export(*args, **kwargs)

        self.window._exporter = controlled_export
        self.window._export_real()
        self.assertTrue(self._wait_until(entered.is_set))
        self.window._cancel_export()
        release.set()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))
        self.assertTrue(self.window._last_export_result.cancelled)
        self.assertEqual(len(self.window._last_export_result.successes), 1)

    def test_close_during_export_is_deferred_until_thread_finishes(self):
        """Accepting close while QThread runs could destroy the live worker thread."""
        source = self._make_pdf("close.pdf")
        self._set_jobs([source])
        entered = threading.Event()
        release = threading.Event()

        def controlled_export(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release exporter")
            return self._finish_fake_export(*args, **kwargs)

        self.window._exporter = controlled_export
        self.window.show()
        self.window._export_real()
        self.assertTrue(self._wait_until(entered.is_set))
        running_thread = self.window._export_thread

        self.assertFalse(self.window.close())
        self.app.processEvents()
        self.assertTrue(self.window.isVisible())
        self.assertIs(self.window._export_thread, running_thread)
        self.assertTrue(running_thread.isRunning())

        release.set()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))
        self.assertTrue(self._wait_until(lambda: not self.window.isVisible()))
        self.assertIsNone(self.window._export_thread)

    def test_missing_source_fails_individually_and_real_exports_continue(self):
        """Filtering missing paths before queuing would hide their failure and alter total."""
        first = self._make_pdf("first.pdf", pages=2)
        missing = self.root / "missing.pdf"
        third = self._make_pdf("third.pdf", pages=1)
        self._set_jobs([first, missing, third])

        self.window._export_real()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active, timeout=8))

        result = self.window._last_export_result
        self.assertEqual(result.total, 3)
        self.assertEqual([item.name for item in result.successes],
                         ["first.pdf", "third.pdf"])
        self.assertEqual([item.name for item in result.failures], ["missing.pdf"])
        self.assertTrue(result.failures[0].error_text)
        self.assertFalse(result.cancelled)
        outputs = [Path(item.output_path) for item in result.successes]
        self.assertTrue(all(path.exists() for path in outputs))
        self.assertEqual([len(PdfReader(str(path)).pages) for path in outputs], [2, 1])

    def test_thread_start_failure_sets_a_terminal_failure_status(self):
        """A QThread start error must not leave the UI claiming export is in progress."""
        source = self._make_pdf("start-error.pdf")
        self._set_jobs([source])
        destroyed = []
        worker_refs = []

        def fail_start():
            worker = self.window._export_worker
            worker.destroyed.connect(
                lambda: destroyed.append(True), Qt.DirectConnection
            )
            worker_refs.append(weakref.ref(worker))
            raise RuntimeError("cannot start worker")

        with patch.object(demo.QThread, "start",
                          side_effect=fail_start):
            self.window._export_real()
        self.app.processEvents()
        gc.collect()

        self.assertFalse(self.window._export_active)
        self.assertEqual(len(self.window._last_export_result.failures), 1)
        self.assertIn("cannot start worker",
                      self.window._last_export_result.failures[0].error_text)
        self.assertIn("失败", self.window.status_label.text())
        self.assertNotIn("正在导出", self.window.status_label.text())
        self.assertEqual(destroyed, [True])
        worker = worker_refs[0]()
        self.assertTrue(worker is None or not isValid(worker))

    def test_empty_preparation_exception_keeps_exception_type_as_error_text(self):
        """An exception with an empty str() must still become a visible job failure."""
        class SilentPreparationError(Exception):
            pass

        source = self._make_pdf("silent-error.pdf")
        self._set_jobs([source])
        with patch.object(demo, "page_layout",
                          side_effect=SilentPreparationError()):
            self.window._export_real()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))

        result = self.window._last_export_result
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].name, "silent-error.pdf")
        self.assertEqual(result.failures[0].error_text,
                         "SilentPreparationError")

    def test_empty_export_exception_keeps_exception_type_as_error_text(self):
        """A silent exporter exception must remain actionable in the batch result."""
        class SilentExportError(Exception):
            pass

        source = self._make_pdf("silent-export.pdf")
        self._set_jobs([source])

        def silent_failure(*_args, **_kwargs):
            raise SilentExportError()

        self.window._exporter = silent_failure
        self.window._export_real()
        self.assertTrue(self._wait_until(lambda: not self.window._export_active))
        self.assertEqual(
            self.window._last_export_result.failures[0].error_text,
            "SilentExportError",
        )


if __name__ == "__main__":
    unittest.main()
