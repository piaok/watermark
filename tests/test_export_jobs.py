import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import fitz
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


class BulkExportPerformanceTests(unittest.TestCase):
    """Long documents must export fast without re-embedding the stamp per page."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _make_multipage_pdf(self, name: str, pages: int) -> Path:
        # fitz insert_text 产生与真实文档相当的富内容流（reportlab 的
        # drawString 内容流过小，无法触发 PyPDF2 逐页解析的性能悬崖）。
        path = self.root / name
        doc = fitz.open()
        for page_number in range(pages):
            page = doc.new_page(width=595, height=842)
            for line in range(40):
                page.insert_text(
                    (50, 62 + line * 18),
                    f"page {page_number + 1} line {line + 1} "
                    "sample body text ABCDEFG 0123456789",
                    fontsize=10,
                )
        doc.save(str(path), garbage=3)
        doc.close()
        return path

    def _make_stamp(self) -> Path:
        # 高熵 RGBA：压缩后仍有 ~1.5MB，代表真实印章图（用户的章.png
        # 为 1132x513/2.2MB）。纯色小图会让旧路径的每页图像流拷贝
        # 成本被低估，无法触发性能回归断言。
        import os
        path = self.root / "bulk_stamp.png"
        raw = bytearray(os.urandom(1132 * 513 * 4))
        raw[3::4] = b"\xff" * (1132 * 513)
        Image.frombytes("RGBA", (1132, 513), bytes(raw)).save(
            path, compress_level=1)
        return path

    def test_sixty_page_export_completes_quickly_with_small_output(self):
        """PyPDF2 per-page merging + per-page image embedding made 200-page exports take minutes and produce 100 MB+ files."""
        source = self._make_multipage_pdf("bulk60.pdf", pages=60)
        stamp = self._make_stamp()

        started = time.perf_counter()
        out = self.root / "bulk60_out.pdf"
        export_pdf(str(source), str(out),
                   layout=Layout(size_mode="percent", percent=30.0),
                   images=[stamp])
        elapsed = time.perf_counter() - started

        size_mb = out.stat().st_size / 1048576
        self.assertLess(elapsed, 15.0,
                        f"60-page export took {elapsed:.1f}s")
        self.assertLess(size_mb, 3.0,
                        f"60-page output is {size_mb:.1f} MB; the stamp "
                        "must not be re-embedded on every page")
        self.assertEqual(len(PdfReader(str(out)).pages), 60)

    def test_every_page_carries_the_stamp_at_the_same_position(self):
        """A page skipped by the merge step would silently lose its watermark."""
        source = self._make_multipage_pdf("bulk3.pdf", pages=3)
        stamp = self._make_stamp()
        out = self.root / "bulk3_out.pdf"
        export_pdf(str(source), str(out),
                   layout=Layout(size_mode="percent", percent=30.0),
                   images=[stamp])

        # 严格检查：渲染三页，水印红色像素包围盒中心一致
        def stamp_bbox(page_index: int):
            doc = fitz.open(str(out))
            page = doc[page_index]
            pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5))
            w, h, n = pix.width, pix.height, pix.n
            data = pix.samples
            doc.close()
            minx, miny, maxx, maxy = w, h, -1, -1
            for y in range(h):
                row = y * w * n
                for x in range(w):
                    r, g, b = data[row + x * n], data[row + x * n + 1], \
                        data[row + x * n + 2]
                    if r > 150 and g < 110 and b < 110:
                        minx, miny = min(minx, x), min(miny, y)
                        maxx, maxy = max(maxx, x), max(maxy, y)
            return (minx + maxx) / 2, (miny + maxy) / 2, maxx >= 0

        first = stamp_bbox(0)
        self.assertTrue(first[2], "page 0 lost its stamp")
        for page_index in (1, 2):
            other = stamp_bbox(page_index)
            self.assertTrue(other[2], f"page {page_index} lost its stamp")
            self.assertAlmostEqual(first[0], other[0], delta=2)
            self.assertAlmostEqual(first[1], other[1], delta=2)
