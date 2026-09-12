import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pdf_watermark_demo import DemoWindow


class ImageImportDeduplicationTests(unittest.TestCase):
    """Regression coverage for image aliases entering through either UI path."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = DemoWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_import_paths_deduplicates_relative_and_case_aliases(self):
        """A missing canonical identity check would append the same image twice."""
        with tempfile.TemporaryDirectory() as directory:
            original_directory = os.getcwd()
            try:
                os.chdir(directory)
                image_path = Path("stamp.png")
                image_path.touch()

                self.window._import_paths([], [str(image_path)])
                self.window._import_paths([], [str(image_path.resolve()).upper()])
            finally:
                os.chdir(original_directory)

        self.assertEqual(len(self.window._images), 1)

    def test_import_paths_keeps_first_seen_order_while_deduplicating_batch(self):
        """A batch must retain its first A then B display paths after alias removal."""
        with tempfile.TemporaryDirectory() as directory:
            original_directory = os.getcwd()
            try:
                os.chdir(directory)
                first_image = Path("first.png")
                second_image = Path("second.png")
                first_image.touch()
                second_image.touch()

                self.window._import_paths(
                    [],
                    [
                        str(first_image),
                        str(first_image.resolve()).upper(),
                        str(second_image),
                    ],
                )
            finally:
                os.chdir(original_directory)

        self.assertEqual(self.window._images, ["first.png", "second.png"])

    def test_file_picker_deduplicates_image_already_imported_by_drop(self):
        """A picker result must share the drop-import identity namespace."""
        with tempfile.TemporaryDirectory() as directory:
            original_directory = os.getcwd()
            try:
                os.chdir(directory)
                image_path = Path("stamp.png")
                image_path.touch()
                self.window._import_paths([], [str(image_path)])

                with patch(
                    "pdf_watermark_demo.QFileDialog.getOpenFileNames",
                    return_value=([str(image_path.resolve()).upper()], ""),
                ):
                    self.window._add_images()
            finally:
                os.chdir(original_directory)

        self.assertEqual(len(self.window._images), 1)
