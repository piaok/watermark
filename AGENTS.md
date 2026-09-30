# AGENTS.md

Windows project: 戳个印, a PySide6 desktop PDF watermark application.

## Active code path
- `pdf_watermark_demo.py` is the application entry point and the entry packaged by PyInstaller.
- `pdf_watermark_studio/core/engine.py` provides PDF rendering, watermark geometry and export; keep this dependency.
- `pdf_watermark_studio/core/export_jobs.py` publishes uniquely named outputs without overwriting existing files.
- `pdf_watermark_studio/export_worker.py` runs batch exports with independent input snapshots, file-boundary cancellation and structured results. Only the GUI thread updates widgets; retain thread references until `QThread.finished`.
- `pdf_watermark_studio/src/` contains application branding assets.
- The app starts with an EMPTY document/watermark list; users add PDFs and images themselves. Root PDF/PNG files are user samples — never auto-discovered, never overwritten.

## Coordinate contract (preview == export)
- `PageState.dx/dy` and `image_offsets` are in PDF points (pt), relative to page center.
- Preview renders at `PREVIEW_ZOOM=1.4`; all layout math goes through `page_layout(st, zoom, image_path)` — zoom=1.4 for preview bitmaps, zoom=1.0 (pt) for export. Drag deltas convert widget px → pt via `PageGridDialog._pt_per_px` (fullscreen view: `FullScreenPageDialog._pt_per_px`).
- `export_pdf` handles pages with a `/Rotate` attribute (overlay drawn in display space, inverse-transformed). Implementation is performance-critical — do NOT revert to per-page PyPDF2 `merge_page`: the export builds one reportlab thin overlay (stamp image embedded once, XObject shared across pages) and merges every page via PyMuPDF `show_pdf_page(..., overlay=True)` on the unrotated mediabox rect, then writes with `doc.tobytes(garbage=1, deflate=True)`. PyPDF2 per-page merging took 0.5–1 s per content-rich page (minutes-long "frozen" exports) and re-embedded the stamp per page (200 pages → 108 MB output vs ~1 MB now).
- Verify WYSIWYG after touching layout/export code: `_verify_align.py` compares preview composite vs exported-PDF render pixel-wise (percent/cm/original/rotated/multi-stamp cases).

## Run and build
- Runtime env: `D:\pyprojects\miniconda\python.exe` (Python 3.13, has deps). The default `py` launcher is Python 3.8 without deps.
- Install runtime dependencies: `python -m pip install -r requirements.txt`.
- Start: `python pdf_watermark_demo.py`.
- Packaging uses PyInstaller: `pip install pyinstaller`, then `python build_exe.py` (onefile → `dist/戳个印.exe`, ~52 MB; workpath/payload/spec live under `build_assets/`, regenerable). The script generates its own spec and applies size filters — keep them all: conda `Library\bin` DLL injection (`_ctypes.pyd` → `ffi.dll`, else the exe dies at startup on Anaconda Python 3.13); drops of unused Qt modules (Quick/Qml/Pdf/Network/Svg/OpenGL), `opengl32sw`, Qt translations, PIL AVIF, tkinter/sqlite3; and numpy/pandas/lxml (only reachable via `pymupdf.table` lazy imports). Re-enable numpy/pandas/lxml if `pymupdf.find_tables` is ever used.

## Verification
- For headless GUI checks, set `QT_QPA_PLATFORM=offscreen` before creating QApplication.
- Run `D:\pyprojects\miniconda\python.exe -B -m unittest discover -s tests -v` for temporary-fixture regression tests. Cover input identity, offset remapping, output safety, layout controls, export lifecycle and help/feedback dialog copy after related changes.
- `_demo_smoke4.py` asserts: empty startup, drag-drop import, help/feedback dialog render, PageGridDialog + titled dialog render, simulated drag (pt conversion + pixel-level stamp movement), fast-composite timing, fullscreen-view interactions (wheel resize / prev-next navigation / right-click toggle / left-drag), and an export into a fresh temp directory. `_verify_align.py` pixel-compares preview vs export (WYSIWYG).
- Title bar entries are `帮助` (`DemoWindow._show_help`) and `反馈` (`DemoWindow._send_feedback`); both dialogs are built by `_build_help_dialog` / `_build_feedback_dialog` so headless tests can inspect them without `exec()`. Keep the help text in sync with the real interactions: left click a home thumbnail to open the document, left click a page preview to open that page fullscreen, hold left button on a preview to move the stamp, right click a page preview to add/remove that page's stamp, right click blank space to return home.
- Page tiles carry a `全屏` chip (`PageTile._fullscreen_chip_rect` → `PageGridDialog.open_fullscreen`). Left-clicking a tile (press+release under 3 px) also opens fullscreen for that page; hold-drag still moves the stamp. `FullScreenPageDialog` is the single-page fullscreen editor: right click inside the page toggles that page's stamp, right click outside the page closes, left-drag moves the stamp, mouse wheel resizes it (percent/cm/fixed modes), floating `上一页`/`下一页` buttons (fixed 112×40 and ←/→ keys navigate. All state stays in the shared `PageState` (pt units), so fullscreen edits are WYSIWYG with grid preview and export.
- Never stub a PySide6 method with `unittest.mock.MagicMock` (e.g. `patch.object(FullScreenPageDialog, "exec")` without `new=`): constructing any wrapped instance afterwards crashes with 0xC0000005. Patch with a plain Python function instead.
- The export group box keeps this order: 导出全部 → progress → status → `btn_open_dir` (打开输出文件夹, `os.startfile`, enabled only when the output dir is valid) → 导出详情.
- Output directory picking must stay machine-independent: `_pick_dir` -> `_ask_directory` uses an instance `QFileDialog` (never the static `getExistingDirectory`, which returns "" both for a cancel and for an accepted-but-unconvertible selection) and checks `exec()` before reading `selectedFiles()` — Qt still reports the current directory there after a cancel. If the native dialog accepts without a path (virtual shell locations such as `此电脑`/`网络` have none), it retries with `DontUseNativeDialog` and skips the native dialog for the rest of the run. `_type_dir` / double-clicking the path label is the always-available fallback; it accepts quoted paths pasted from Explorer.
- Do not overwrite sample PDFs or existing exports during tests.

The former Ollama/OpenCode components, standalone add_watermark.py, and unused Studio GUI have been removed. They are not part of this application.
