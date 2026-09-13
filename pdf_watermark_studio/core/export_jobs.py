"""Safe publication helpers for PDF export jobs.

The rendering engine intentionally accepts an exact destination path for
backwards compatibility.  UI and background jobs use this module when they
need a non-destructive, uniquely named output instead.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from .engine import export_pdf


def _output_name(source_name: str, sequence: int) -> str:
    """Return the requested watermarked filename for a one-based sequence."""
    base = f"watermarked_{source_name}"
    if sequence == 1:
        return base
    suffix = Path(base).suffix
    stem = base[:-len(suffix)] if suffix else base
    return f"{stem}_{sequence}{suffix}"


def _directory_names_casefolded(directory: Path) -> set[str]:
    """Treat existing names conservatively on case-insensitive file systems."""
    return {entry.name.casefold() for entry in directory.iterdir()}


def _file_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _remove_if_same_file(path: Path, expected_identity: tuple[int, int]) -> None:
    """Remove a reservation only when it is still the file this job created."""
    try:
        if _file_identity(path) == expected_identity:
            path.unlink()
    except FileNotFoundError:
        pass


def _reserve_unique_output(output_dir: Path, source_name: str) -> tuple[Path, tuple[int, int]]:
    """Create an empty target exclusively, returning its path and file identity."""
    existing_names = _directory_names_casefolded(output_dir)
    sequence = 1
    while True:
        candidate = output_dir / _output_name(source_name, sequence)
        sequence += 1
        if candidate.name.casefold() in existing_names:
            continue
        try:
            descriptor = os.open(
                os.fspath(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL
            )
        except FileExistsError:
            existing_names.add(candidate.name.casefold())
            continue
        try:
            reservation_identity = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        return candidate, (reservation_identity.st_dev, reservation_identity.st_ino)


def export_unique_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    *args: Any,
    exporter: Callable[..., None] = export_pdf,
    **kwargs: Any,
) -> Path:
    """Export a PDF without replacing any existing result and return its path.

    The PDF is first written to a private temporary file in ``output_dir``.
    Only after rendering succeeds does the helper reserve an available
    ``watermarked_<source>.pdf`` name using exclusive creation and replace that
    reservation atomically.  If either phase fails, files created by this call
    are removed; pre-existing outputs are never altered.
    """
    source = Path(pdf_path)
    destination_dir = Path(output_dir)
    if not destination_dir.is_dir():
        raise NotADirectoryError(f"Output directory does not exist: {destination_dir}")

    temporary_path: Path | None = None
    reserved_path: Path | None = None
    reservation_identity: tuple[int, int] | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".watermark-export-", suffix=".pdf",
            dir=destination_dir, delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        exporter(str(source), str(temporary_path), *args, **kwargs)
        reserved_path, reservation_identity = _reserve_unique_output(
            destination_dir, source.name
        )
        try:
            os.replace(temporary_path, reserved_path)
        except Exception:
            _remove_if_same_file(reserved_path, reservation_identity)
            raise
        temporary_path = None
        return reserved_path
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        if reserved_path is not None and reservation_identity is not None:
            # A successful os.replace consumes the reservation, so this does
            # nothing.  A failure removes only the still-identical placeholder.
            _remove_if_same_file(reserved_path, reservation_identity)

