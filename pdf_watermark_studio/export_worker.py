"""Qt worker and stable result values for asynchronous batch export."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

from pdf_watermark_studio.core.export_jobs import export_unique_pdf


def _error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


@dataclass(frozen=True)
class ExportSuccess:
    name: str
    output_path: str


@dataclass(frozen=True)
class ExportFailure:
    name: str
    error_text: str


@dataclass(frozen=True)
class BatchResult:
    total: int
    successes: tuple[ExportSuccess, ...]
    failures: tuple[ExportFailure, ...]
    cancelled: bool


@dataclass(frozen=True)
class ExportJob:
    """Complete immutable-enough input snapshot for one source PDF."""

    name: str
    path: str
    images: tuple[str, ...]
    page_image_layouts: object
    page_count: int
    preparation_error: str = ""


class ExportWorker(QObject):
    """Run PDF jobs sequentially, checking cancellation only at file boundaries."""

    progress = Signal(int, int, str)
    finished = Signal(object)

    def __init__(
        self,
        jobs: tuple[ExportJob, ...],
        output_dir: str,
        cancel_event: Event,
        exporter: Callable | None = None,
    ) -> None:
        super().__init__()
        self._jobs = jobs
        self._output_dir = output_dir
        self._cancel_event = cancel_event
        self._exporter = exporter or export_unique_pdf

    @Slot()
    def run(self) -> None:
        successes: list[ExportSuccess] = []
        failures: list[ExportFailure] = []
        cancelled = False
        total_work = sum(max(1, job.page_count) for job in self._jobs)
        completed_work = 0
        active_job: ExportJob | None = None

        try:
            for job in self._jobs:
                active_job = job
                if self._cancel_event.is_set():
                    cancelled = True
                    break

                work_units = max(1, job.page_count)
                if job.preparation_error:
                    failures.append(ExportFailure(job.name, job.preparation_error))
                else:
                    def report_page(done_pages, total_pages, *, _job=job,
                                    _base=completed_work,
                                    _units=work_units) -> None:
                        source_total = max(1, int(total_pages or _units))
                        within = min(
                            _units,
                            max(0, round(int(done_pages) * _units / source_total)),
                        )
                        self.progress.emit(_base + within, total_work, _job.name)

                    try:
                        output_path = self._exporter(
                            job.path,
                            self._output_dir,
                            images=[Path(path) for path in job.images],
                            page_image_layouts=job.page_image_layouts,
                            progress_cb=report_page,
                        )
                    except Exception as exc:
                        failures.append(ExportFailure(job.name, _error_text(exc)))
                    else:
                        successes.append(ExportSuccess(
                            job.name, str(output_path)
                        ))

                completed_work += work_units
                self.progress.emit(completed_work, total_work, job.name)
                if self._cancel_event.is_set():
                    cancelled = True
                    break
        except BaseException as exc:
            # A worker bug must still deliver a terminal result so QThread can
            # quit and the window can restore or finish a deferred close.
            name = active_job.name if active_job is not None else "导出任务"
            failures.append(ExportFailure(name, _error_text(exc)))
        finally:
            if self._cancel_event.is_set():
                cancelled = True
            self.finished.emit(BatchResult(
                total=len(self._jobs),
                successes=tuple(successes),
                failures=tuple(failures),
                cancelled=cancelled,
            ))
