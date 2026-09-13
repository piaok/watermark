import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from PIL import Image
from PyPDF2 import PdfReader
from reportlab.pdfgen import canvas

from pdf_watermark_studio.core.engine import Layout, export_pdf
from pdf_watermark_studio.core.export_jobs import export_unique_pdf


class UniquePdfExportTests(unittest.TestCase):
    def _make_pdf(self, path: Path, pages: int = 2) -> None:
        document = canvas.Canvas(str(path), pagesize=(200, 120))
        for page_number in range(pages):
            document.drawString(20, 80, f"source page {page_number + 1}")
            document.showPage()
        document.save()

    def _make_stamp(self, path: Path) -> None:
        Image.new("RGBA", (20, 12), (220, 40, 40, 255)).save(path)

    def test_same_named_sources_sharing_output_and_repeats_get_unique_outputs(self):
        """Different x.pdf files sharing one output folder must never overwrite a result."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stamp = root / "stamp.png"
            self._make_stamp(stamp)
            first_source_dir = root / "first"
            second_source_dir = root / "second"
            output_dir = root / "output"
            first_source_dir.mkdir()
            second_source_dir.mkdir()
            output_dir.mkdir()
            first_source = first_source_dir / "x.pdf"
            second_source = second_source_dir / "x.pdf"
            self._make_pdf(first_source, pages=2)
            self._make_pdf(second_source, pages=3)
            first_source_bytes = first_source.read_bytes()
            second_source_bytes = second_source.read_bytes()
            # This spelling is distinct on this test host but collides on Windows.
            existing = output_dir / "WaterMarked_X.PDF"
            existing_bytes = b"pre-existing output must not change"
            existing.write_bytes(existing_bytes)

            first_result = export_unique_pdf(
                first_source, output_dir, layout=Layout(), images=[stamp]
            )
            second_result = export_unique_pdf(
                second_source, output_dir, layout=Layout(), images=[stamp]
            )
            repeated_result = export_unique_pdf(
                first_source, output_dir, layout=Layout(), images=[stamp]
            )

            self.assertEqual(first_result.name, "watermarked_x_2.pdf")
            self.assertEqual(second_result.name, "watermarked_x_3.pdf")
            self.assertEqual(repeated_result.name, "watermarked_x_4.pdf")
            self.assertEqual(existing.read_bytes(), existing_bytes)
            self.assertEqual(first_source.read_bytes(), first_source_bytes)
            self.assertEqual(second_source.read_bytes(), second_source_bytes)
            self.assertEqual(len(PdfReader(str(first_result)).pages), 2)
            self.assertEqual(len(PdfReader(str(second_result)).pages), 3)
            self.assertEqual(len(PdfReader(str(repeated_result)).pages), 2)

    def test_failed_export_removes_only_its_temporary_file(self):
        """A failed exporter must leave a pre-existing output byte-for-byte intact."""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            source = output_dir / "x.pdf"
            self._make_pdf(source)
            existing = output_dir / "watermarked_x.pdf"
            existing_bytes = b"do not overwrite this output"
            existing.write_bytes(existing_bytes)

            def failing_exporter(_source, temporary_output, **_kwargs):
                Path(temporary_output).write_bytes(b"partial export")
                raise RuntimeError("simulated export failure")

            with self.assertRaisesRegex(RuntimeError, "simulated export failure"):
                export_unique_pdf(source, output_dir, exporter=failing_exporter)

            self.assertEqual(existing.read_bytes(), existing_bytes)
            self.assertFalse((output_dir / "watermarked_x_2.pdf").exists())
            self.assertEqual(list(output_dir.glob(".watermark-export-*.pdf")), [])

    def test_concurrent_exports_reserve_different_names(self):
        """A check-only implementation would let concurrent exports overwrite _2."""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            source = output_dir / "x.pdf"
            stamp = output_dir / "stamp.png"
            self._make_pdf(source)
            self._make_stamp(stamp)
            existing = output_dir / "watermarked_x.pdf"
            existing_bytes = b"pre-existing output"
            existing.write_bytes(existing_bytes)
            barrier = Barrier(2)

            def synchronized_real_exporter(*args, **kwargs):
                barrier.wait(timeout=5)
                return export_pdf(*args, **kwargs)

            def export_once():
                return export_unique_pdf(
                    source, output_dir, layout=Layout(), images=[stamp],
                    exporter=synchronized_real_exporter,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _unused: export_once(), range(2)))

            self.assertEqual({result.name for result in results},
                             {"watermarked_x_2.pdf", "watermarked_x_3.pdf"})
            self.assertEqual(existing.read_bytes(), existing_bytes)
            self.assertTrue(all(len(PdfReader(str(result)).pages) == 2
                                for result in results))
