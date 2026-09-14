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
- Preview renders at `PREVIEW_ZOOM=1.4`; all layout math goes through `page_layout(st, zoom, image_path)` — zoom=1.4 for preview bitmaps, zoom=1.0 (pt) for export. Drag deltas convert widget px → pt via `PageGridDialog._pt_per_px`.
- `export_pdf` handles pages with a `/Rotate` attribute (overlay drawn in display space, inverse-transformed).
- Verify WYSIWYG after touching layout/export code: `_verify_align.py` compares preview composite vs exported-PDF render pixel-wise (percent/cm/original/rotated/multi-stamp cases).

## Run and build
- Runtime env: `D:\pyprojects\miniconda\python.exe` (Python 3.13, has deps). The default `py` launcher is Python 3.8 without deps.
- Install runtime dependencies: `python -m pip install -r requirements.txt`.
- Start: `python pdf_watermark_demo.py`.
- Packaging uses PyInstaller: `pip install pyinstaller`, then `python build_exe.py` (onefile → `dist/戳个印.exe`; workpath/spec live under `build_assets/`, regenerable).

## Verification
- For headless GUI checks, set `QT_QPA_PLATFORM=offscreen` before creating QApplication.
- Run `D:\pyprojects\miniconda\python.exe -B -m unittest discover -s tests -v` for temporary-fixture regression tests. Cover input identity, offset remapping, output safety, layout controls and export lifecycle after related changes.
- `_demo_smoke4.py` asserts: empty startup, drag-drop import, PageGridDialog + titled dialog render, simulated drag (pt conversion + pixel-level stamp movement), fast-composite timing, and an export into a fresh temp directory. `_verify_align.py` pixel-compares preview vs export (WYSIWYG).
- Do not overwrite sample PDFs or existing exports during tests.

The former Ollama/OpenCode components, standalone add_watermark.py, and unused Studio GUI have been removed. They are not part of this application.
