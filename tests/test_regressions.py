import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QItemSelectionModel

from pdf_watermark_demo import DemoWindow, MockPdf, PageState


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


    def test_removing_multiple_images_remaps_offsets_for_every_document_page(self):
        """Keeping B after removing A/C must retain B's old index-1 offset as index 0."""
        self.window._import_paths([], ["A.png", "B.png", "C.png"])
        first = PageState(
            wm=True, dx=101.0, dy=102.0, size_mode="cm", percent=48.0,
            cm_w=6.5, cm_h=7.5,
            image_offsets={0: (1.0, 2.0), 1: (3.0, 4.0), 2: (5.0, 6.0)},
        )
        second = PageState(
            wm=False, dx=201.0, dy=202.0, size_mode="original", percent=49.0,
            cm_w=8.5, cm_h=9.5, image_offsets={1: (30.0, 40.0)},
        )
        third = PageState(
            wm=True, dx=301.0, dy=302.0, image_offsets={0: (50.0, 60.0),
                                                         1: (70.0, 80.0),
                                                         2: (90.0, 100.0)},
        )
        fallback = PageState(wm=True, dx=401.0, dy=402.0, percent=51.0)
        self.window.files = [
            MockPdf(name="first.pdf", pages=[first, second]),
            MockPdf(name="second.pdf", pages=[third, fallback]),
        ]

        selection = self.window.img_list.selectionModel()
        for row in (0, 2):
            selection.select(
                self.window.img_list.model().index(row, 0),
                QItemSelectionModel.Select,
            )
        self.window._remove_img_mock()

        self.assertEqual(self.window._images, ["B.png"])
        self.assertEqual(first.image_offsets, {0: (3.0, 4.0)})
        self.assertEqual(second.image_offsets, {0: (30.0, 40.0)})
        self.assertEqual(third.image_offsets, {0: (70.0, 80.0)})
        self.assertEqual(fallback.image_offsets, {})
        self.assertEqual((fallback.dx, fallback.dy), (401.0, 402.0))
        self.assertEqual((first.wm, first.size_mode, first.percent,
                          first.cm_w, first.cm_h),
                         (True, "cm", 48.0, 6.5, 7.5))
        self.assertEqual((second.wm, second.size_mode, second.percent,
                          second.cm_w, second.cm_h),
                         (False, "original", 49.0, 8.5, 9.5))

    def test_removing_images_with_no_selection_keeps_offsets_unchanged(self):
        """An empty selection must not mutate image order or any page offsets."""
        self.window._import_paths([], ["A.png", "B.png", "C.png"])
        page = PageState(dx=10.0, dy=20.0, image_offsets={0: (1.0, 2.0),
                                                            2: (3.0, 4.0)})
        self.window.files = [MockPdf(name="only.pdf", pages=[page])]

        self.window._remove_img_mock()

        self.assertEqual(self.window._images, ["A.png", "B.png", "C.png"])
        self.assertEqual(page.image_offsets, {0: (1.0, 2.0), 2: (3.0, 4.0)})

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
