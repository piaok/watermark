#!/usr/bin/env python3
"""纯前端 PDF 水印交互 Demo（无后端，模拟数据）。

交互约定：
1. 只有鼠标右键切换该页 添加/不添加水印；左键不影响水印有无。
2. 左侧保留第一版面板：PDF 列表、图片列表、布局控件、导出控件。
3. 左键拖动只移动「被拖那一页」的水印位置（文件内其他页、其他文件均不动）。
4. 弹出窗口：所有页面按 3 列连续平铺，页面多于 9 页时靠右侧垂直滚动条查看；
   水印大小含「页面宽度百分比」与「原始尺寸」两种模式，放大/缩小只影响当前页。
5. 首页卡片排满自动换行（FlowLayout），不再无限横向延伸。
6. 页面网格（第二级视图）内左键点击页面 = 进入该页全屏；按住左键拖动 =
   移动图章（拖动阈值 3px 区分点击与拖动）。
7. 页面预览右上角「全屏」角标同样进入单页全屏视图：交互与预览一致
   （右键切换、左键拖动），滚轮调节图章大小，左右悬浮按钮/方向键翻页。

坐标约定（所见即所得的关键）：
- PageState.dx/dy 与 image_offsets 一律以 PDF 点（pt，72dpi）为单位，
  相对页面中心（锚点居中）。
- 预览位图 = pt × PREVIEW_ZOOM；合成预览时布局经 page_layout(zoom) 换算成
  位图像素；导出时 zoom=1.0 直接用 pt（reportlab 坐标系）。
- 拖动量以「控件像素 → pt」换算后再写入 PageState，保证三处单位一致。

运行： python pdf_watermark_demo.py
"""
from __future__ import annotations

import copy
import os
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as _wintypes

from PySide6.QtCore import (QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                            QStandardPaths, QThread, QTimer)
from PySide6.QtGui import (QColor, QCursor, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLayout, QLineEdit, QListWidget, QMainWindow, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout,
    QWidget,
)
from shiboken6 import delete as delete_qt_object

from pdf_watermark_studio.core.engine import (
    Anchor, Layout, SizeMode, rect_for,
    render_page_bg, composite_watermark, list_pdf_pages,
    image_native_size,
)
from pdf_watermark_studio.core.export_jobs import export_unique_pdf
from pdf_watermark_studio.export_worker import (
    BatchResult, ExportFailure, ExportJob, ExportWorker,
)

PAGE_W, PAGE_H = 420, 297
CARD_W, CARD_H = 340, int(340 * 297 / 420)
WATERMARK_COLOR = QColor(231, 76, 60, 200)
ORIG_W, ORIG_H = 300, 186          # 模拟水印图片「原始尺寸」（页面坐标）
MODE_PERCENT = "percent"
MODE_ORIGINAL = "original"
MODE_CM = "cm"
MODE_FIXED = "fixed"
ANCHOR_KEEP = "keep"
CM_TO_PT = 28.3464567             # 1 cm = 72/2.54 pt


def _path_identity(path: str) -> str:
    """Return the platform-normalized identity for a user-selected path."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


# ---------------------------------------------------------------- 模拟数据
@dataclass
class PageState:
    """单页状态：水印开关 + 位置偏移 + 尺寸（均只影响本页）。"""
    wm: bool = False
    dx: float = 0.0
    dy: float = 0.0
    size_mode: str = MODE_PERCENT
    percent: float = 30.0
    fixed_width: float = 180.0     # FIXED：PDF 点（pt）
    cm_w: float = 5.0             # CM：宽（cm）
    cm_h: float = 5.0             # CM：高（cm）
    opacity: float = 1.0           # 0~1，仅预览；导出始终不透明
    # 每张水印图在本页的独立位置；未记录时使用 dx/dy 作为默认位置
    image_offsets: dict[int, tuple[float, float]] = field(default_factory=dict)


@dataclass
class MockPdf:
    """一个待处理文件：文件名 + 各页状态 + 真实 PDF 路径（可为空）。"""
    name: str
    pages: List[PageState] = field(default_factory=list)
    path: str = ""


def make_pages(n: int) -> List[PageState]:
    return [PageState(wm=False) for _ in range(n)]


def page_layout(st: PageState, zoom: float = 1.0,
                image_path: Optional[str] = None,
                native_size: Optional[tuple[int, int]] = None) -> "Layout":
    """把 PageState 映射为引擎 Layout（锚点居中 + 本页偏移/尺寸）。

    zoom=PREVIEW_ZOOM 时输出预览位图像素单位；zoom=1.0 时输出 pt（导出）。
    dx/dy、各尺寸模式都按 zoom 同比换算，保证预览与导出几何一致。
    原始尺寸模式需要知道图片原生像素（image_path 或 native_size 二选一）。
    """
    dx, dy = st.dx * zoom, st.dy * zoom
    common = {"dx": dx, "dy": dy, "opacity": st.opacity,
              "margin": 12.0 * zoom}
    if st.size_mode == MODE_ORIGINAL:
        if native_size is None and image_path is not None:
            native_size = image_native_size(Path(image_path))
        if native_size is None:
            return Layout(size_mode=SizeMode.DEFAULT, **common)
        iw, ih = native_size
        return Layout(size_mode=SizeMode.FIXED,
                      fixed_width=max(1, round(iw * zoom)),
                      fixed_height=max(1.0, ih * zoom), **common)
    if st.size_mode == MODE_FIXED:
        return Layout(size_mode=SizeMode.FIXED,
                      fixed_width=max(1.0, st.fixed_width * zoom),
                      **common)
    if st.size_mode == MODE_CM:
        return Layout(size_mode=SizeMode.FIXED,
                      fixed_width=max(1, round(st.cm_w * CM_TO_PT * zoom)),
                      fixed_height=max(1, round(st.cm_h * CM_TO_PT * zoom)),
                      **common)
    return Layout(size_mode=SizeMode.PERCENT, percent=st.percent, **common)


# 预览渲染 zoom（与 render_page_bg / _background 保持一致）
PREVIEW_ZOOM = 1.4


# ---------------------------------------------------------------- 共用渲染
def render_page(pixmap: QPixmap, pdf: MockPdf, page_index: int,
                image_paths: List[str], active_image_index: int = 0,
                active_offset: Optional[tuple[float, float]] = None) -> None:
    """卡片与弹窗共用的唯一渲染函数，保证位置/大小一致。

    - 有真实 PDF 路径：渲染真实页面；有真实水印图则合成，否则叠模拟色块。
    - 无路径：纯模拟绘制（离线演示）。
    - image_paths 全部绘制（多图预览），active_offset 仅拖动时用于定位当前图。
    """
    st = pdf.pages[page_index]
    if pdf.path and os.path.exists(pdf.path):
        try:
            w, h, pil = render_page_bg(Path(pdf.path), page_index, zoom=PREVIEW_ZOOM)
            if st.wm and image_paths:
                # 真实图片合成（PIL → QImage）：每张图用自己独立位置逐张叠放
                composed = _composite_multi(pil, st, image_paths)
                img = QImage(composed.tobytes(), w, h, w * 4,
                             QImage.Format_RGBA8888)
            else:
                img = QImage(pil.tobytes(), w, h, w * 4,
                             QImage.Format_RGBA8888)
            qpix = QPixmap.fromImage(img)
            pixmap.fill(QColor("#0f172a"))
            painter = QPainter(pixmap)
            scaled = qpix.scaled(pixmap.width(), pixmap.height(),
                                 Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap((pixmap.width() - scaled.width()) // 2,
                               (pixmap.height() - scaled.height()) // 2, scaled)
            painter.end()
            return
        except Exception:
            pass   # 渲染失败回退到模拟绘制
    # ---- 纯模拟绘制 ----
    pixmap.fill(QColor("white"))
    painter = QPainter(pixmap)
    scale = min(pixmap.width() / PAGE_W, pixmap.height() / PAGE_H)
    painter.translate((pixmap.width() - PAGE_W * scale) / 2,
                      (pixmap.height() - PAGE_H * scale) / 2)
    painter.scale(scale, scale)
    painter.fillRect(QRectF(0, 0, PAGE_W, PAGE_H), QColor("white"))
    painter.setPen(QColor("#e6e6e6"))
    for line in range(1, 12):
        painter.drawText(QPointF(14, line * 24), "模拟 PDF 文档内容，用于演示水印效果。")
    if st.wm:
        if st.size_mode == MODE_ORIGINAL:
            width, height = float(ORIG_W), float(ORIG_H)
        elif st.size_mode == MODE_CM:
            width, height = st.cm_w * CM_TO_PT, st.cm_h * CM_TO_PT
        else:
            width = PAGE_W * st.percent / 100
            height = width * 0.62
        # 多张水印图：依次绘制；位置用每张图的独立偏移（默认 st.dx/dy）
        painter.setOpacity(st.opacity)
        base_x, base_y = st.dx, st.dy
        for img_idx in range(len(image_paths)):
            if img_idx == active_image_index and active_offset is not None:
                ox, oy = active_offset
            else:
                ox, oy = st.image_offsets.get(img_idx, (base_x, base_y))
            rect = QRectF(PAGE_W / 2 + ox - width / 2,
                          PAGE_H / 2 + oy - height / 2,
                          width, height)
            painter.fillRect(rect, WATERMARK_COLOR)
            painter.setPen(Qt.white)
            painter.drawText(rect, Qt.AlignCenter, "WATERMARK")
    painter.end()


def _composite_multi(pil, st: PageState, image_paths: List[str]):
    """逐张合成水印图，每张图片使用本页自己的偏移。"""
    result = pil
    for idx, path in enumerate(image_paths):
        dx, dy = st.image_offsets.get(idx, (st.dx, st.dy))
        local = replace(st, wm=True, dx=dx, dy=dy, image_offsets={})
        result = composite_watermark(result,
                                     page_layout(local, zoom=PREVIEW_ZOOM,
                                                 image_path=path),
                                     [Path(path)])
    return result


# ---------------------------------------------------------------- 流式布局
class FlowLayout(QLayout):
    """卡片排满自动换行的流式布局。"""

    def __init__(self, parent=None, margin=4, h_spacing=14, v_spacing=14):
        super().__init__(parent)
        self._items: list = []
        self._h, self._v = h_spacing, v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = eff.x(), eff.y()
        line_h = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h
            if next_x - self._h > eff.right() + 1 and line_h > 0:
                x = eff.x()
                y += line_h + self._v
                next_x = x + hint.width() + self._h
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


# ---------------------------------------------------------------- PDF 卡片
class PdfCard(QWidget):
    """单 PDF 卡片：420:297 缩略区 + 文件名 + 页数 + 放大角标。

    单击卡片（无论单页/多页）都进入弹窗，保持交互一致。
    卡片本身不处理拖动/切换水印（都在弹窗内完成）。
    """

    def __init__(self, pdf: MockPdf, index: int, on_zoom, on_change):
        super().__init__()
        self.pdf, self.index = pdf, index
        self.on_zoom, self.on_change = on_zoom, on_change
        self.setFixedSize(CARD_W + 16, CARD_H + 70)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self._chip_hover = False
        self.refresh()

    def refresh(self, image_paths: Optional[List[str]] = None) -> None:
        self.thumb = QPixmap(CARD_W, CARD_H)
        images = image_paths if image_paths is not None else []
        render_page(self.thumb, self.pdf, 0, images)
        self.update()

    def _chip_rect(self) -> QRectF:
        return QRectF(self.width() - 64, self.height() - 34, 54, 25)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        hover = self.underMouse()
        # 圆角卡片底 + 边框（悬停微亮）
        body = QRectF(0.5, 0.5, w - 1, h - 1)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#16223a" if hover else "#121b2c"))
        painter.drawRoundedRect(body, 12, 12)
        painter.setPen(QPen(QColor("#35496e" if hover else "#24324e"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(body, 12, 12)
        # 缩略图（圆角裁剪）
        thumb = QRectF(10, 10, CARD_W, CARD_H)
        path = QPainterPath()
        path.addRoundedRect(thumb, 10, 10)
        painter.save()
        painter.setClipPath(path)
        painter.drawPixmap(10, 10, self.thumb)
        painter.restore()
        # 文件名 + 页数
        painter.setPen(QColor("#dbe4f0"))
        painter.drawText(QRectF(10, CARD_H + 16, CARD_W - 8, 20),
                         Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(
                             self.pdf.name, Qt.ElideMiddle, CARD_W - 8))
        painter.setPen(QColor("#6f839c"))
        small = painter.font()
        small.setPointSizeF(max(8.0, small.pointSizeF() - 1.0))
        painter.setFont(small)
        painter.drawText(QRectF(10, CARD_H + 36, CARD_W, 16),
                         Qt.AlignVCenter, f"{len(self.pdf.pages)} 页")
        painter.setFont(QApplication.font())
        # 放大角标（悬停变印章红）
        chip = self._chip_rect()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#b93a4e" if self._chip_hover else "#0d1524"))
        painter.drawRoundedRect(chip, 7, 7)
        painter.setPen(QPen(QColor("#b93a4e" if not self._chip_hover
                                  else "#d24356"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(chip, 7, 7)
        painter.setPen(QColor("white" if self._chip_hover else "#c7d2e0"))
        painter.drawText(chip, Qt.AlignCenter, "放大")

    # ---------- 交互 ----------
    def mousePressEvent(self, event) -> None:
        # 左键或右键按下都记录起点；单击（未拖动）时触发进弹窗
        if event.button() in (Qt.LeftButton, Qt.RightButton):
            self.press = event.position().toPoint()
            self._moved = False

    def mouseMoveEvent(self, event) -> None:
        hover = self._chip_rect().contains(event.position())
        if hover != self._chip_hover:
            self._chip_hover = hover
            self.update()
        if getattr(self, "press", None) is not None:
            delta = event.position().toPoint() - self.press
            if abs(delta.x()) > 4 or abs(delta.y()) > 4:
                self._moved = True

    def leaveEvent(self, _event) -> None:
        if self._chip_hover:
            self._chip_hover = False
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() not in (Qt.LeftButton, Qt.RightButton):
            return
        if not getattr(self, "_moved", False):
            # 单击（无论左/右键、单页/多页）都打开弹窗
            self.on_zoom(self.index)
        self.press = None
        self._moved = False


# ---------------------------------------------------------------- 弹窗瓦片
class PageTile(QWidget):
    """弹窗内单页瓦片：固定预览尺寸；选中只改边框不改尺寸。

    交互：左键单击进入该页全屏视图；按住左键拖动移动当前选中图片
    在本页的位置；右键切换水印；左键按下同时选中当前页。
    """

    TILE_W, TILE_H = 300, 210        # 更大，铺满窗口换排

    def __init__(self, overlay, page: int):
        super().__init__()
        self.overlay, self.page = overlay, page
        self.press: Optional[QPointF] = None
        self.base_dx = 0.0
        self.base_dy = 0.0
        self.dragging = False
        self.active = False
        self._pix = None
        self._chip_hover = False
        self.setFixedSize(PageTile.TILE_W + 24, PageTile.TILE_H + 48)
        self.setMouseTracking(True)

    def set_active(self, active: bool) -> None:
        self.active = active
        self.update()

    def set_preview(self, pix: QPixmap) -> None:
        if pix.size() != QSize(PageTile.TILE_W, PageTile.TILE_H):
            pix = pix.scaled(PageTile.TILE_W, PageTile.TILE_H,
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._pix = pix
        self.update()

    def _fullscreen_chip_rect(self) -> QRectF:
        """瓦片右上角「全屏」角标的命中区域（控件坐标）。"""
        return QRectF(self.width() - 58, 6, 50, 22)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # 卡片底 + 圆角边框（选中用印章红）
        body = QRectF(0.5, 0.5, w - 1, h - 1)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#121b2c" if not self.active else "#16223a"))
        painter.drawRoundedRect(body, 10, 10)
        painter.setPen(QPen(QColor("#b93a4e" if self.active else "#24324e"),
                            1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(body, 10, 10)
        if self._pix is not None and not self._pix.isNull():
            x = (w - self._pix.width()) // 2
            # 缩略图圆角裁剪
            thumb = QRectF(x, 8, self._pix.width(), self._pix.height())
            path = QPainterPath()
            path.addRoundedRect(thumb, 8, 8)
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(x, 8, self._pix)
            painter.restore()
        painter.setPen(QColor("#6f839c"))
        painter.drawText(QRectF(0, h - 26, w, 18), Qt.AlignCenter,
                         f"第 {self.page + 1} 页")
        # 「全屏」角标（悬停变印章红，与首页「放大」角标同款）
        chip = self._fullscreen_chip_rect()
        hovered = self._chip_hover
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#b93a4e" if hovered else "#0d1524"))
        painter.drawRoundedRect(chip, 7, 7)
        painter.setPen(QPen(QColor("#b93a4e" if hovered else "#35496e"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(chip, 7, 7)
        painter.setPen(QColor("white" if hovered else "#c7d2e0"))
        painter.drawText(chip, Qt.AlignCenter, "全屏")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            self.overlay.set_current(self.page)
            self.overlay.toggle_page(self.page)
            return
        if event.button() == Qt.LeftButton:
            if self._fullscreen_chip_rect().contains(event.position()):
                self.overlay.open_fullscreen(self.page)
                return
            self.overlay.set_current(self.page)
            self.press = QPointF(event.position())
            self.dragging = False

    def mouseMoveEvent(self, event) -> None:
        if self.press is None:
            hovered = self._fullscreen_chip_rect().contains(event.position())
            if hovered != self._chip_hover:
                self._chip_hover = hovered
                self.update()
            return
        dx = event.position().x() - self.press.x()
        dy = event.position().y() - self.press.y()
        if not self.dragging and (abs(dx) > 3 or abs(dy) > 3):
            self.dragging = True
            st = self.overlay.pdf.pages[self.page]
            img_idx = self.overlay.current_image_index
            # 以该图片在该页上次保存的位置为基准，避免再次拖动回到初始位置
            self.base_dx, self.base_dy = st.image_offsets.get(
                img_idx, (st.dx, st.dy))
            st.image_offsets[img_idx] = (self.base_dx, self.base_dy)
        if not self.dragging:
            return
        st = self.overlay.pdf.pages[self.page]
        img_idx = self.overlay.current_image_index
        # 控件像素 → pt：预览与导出共用 pt 单位，1:1 跟手
        k = self.overlay._pt_per_px(self.page)
        if k is None:
            k = 1.0
        st.image_offsets[img_idx] = (self.base_dx + dx * k,
                                     self.base_dy + dy * k)
        # 高频拖动：只重绘当前页（Qt 缓存合成，避免全量重渲染的滞涩）
        self.overlay.rerender_tile(self.page)

    def leaveEvent(self, _event) -> None:
        if self._chip_hover:
            self._chip_hover = False
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.press is not None:
            if self.dragging:
                self.overlay.on_change()
            else:
                # 左键单击（未拖动）：进入该页全屏视图
                self.overlay.open_fullscreen(self.page)
            self.press = None
            self.dragging = False


# ---------------------------------------------------------------- 全屏弹窗
def build_dialog_titlebar(title: str, subtitle: str, on_close) -> QWidget:
    """弹窗统一标题栏（深色 44px）：品牌图标 + 标题 + 副标题 + 关闭按钮。"""
    bar = QWidget()
    bar.setObjectName("dlgTitleBar")
    bar.setFixedHeight(44)
    lay = QHBoxLayout(bar)
    lay.setContentsMargins(16, 0, 4, 0)
    lay.setSpacing(10)
    icon_lbl = QLabel()
    icon_lbl.setFixedSize(20, 20)
    if Brand.icon() is not None:
        icon_lbl.setPixmap(Brand.icon().scaled(
            20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    lay.addWidget(icon_lbl)
    title_lbl = QLabel(title)
    title_lbl.setStyleSheet("color:#e8eef7;font-size:13px;font-weight:600;"
                            "background:transparent;")
    lay.addWidget(title_lbl)
    sub = QLabel(subtitle)
    sub.setObjectName("dlg_subtitle")
    sub.setStyleSheet("color:#5c7089;font-size:11px;background:transparent;")
    lay.addWidget(sub)
    lay.addStretch(1)
    close_btn = TitleButton(TitleButton.CLOSE, "关闭")
    close_btn.clicked.connect(on_close)
    lay.addWidget(close_btn)
    bar.setStyleSheet(
        "QWidget#dlgTitleBar{background:#0b1220;"
        "border-bottom:1px solid #18233a;}"
        "QWidget#dlgTitleBar QLabel{background:transparent;border:0;}"
        "QWidget#dlgTitleBar QPushButton{background:transparent;"
        "color:#8ba0b8;border:0;border-radius:6px;font-size:12px;}"
        "QWidget#dlgTitleBar QPushButton:hover{background:#1d2a42;"
        "color:#dbe4f0;}")
    return bar


class PageGridDialog(QDialog):
    """全屏页面查看：左侧水印图片选择 + 右侧页面网格（自动换排）。"""

    def __init__(self, pdf: MockPdf, on_change, image_paths=None,
                 selected_image_index: int = 0, parent=None):
        super().__init__(parent)
        self.pdf = pdf
        self.on_change = on_change
        self.image_paths = list(image_paths or [])
        self.current_image_index = selected_image_index
        self.current_page = 0
        self.tiles: List[PageTile] = []
        self._bg_cache: dict[int, object] = {}   # 页面背景位图缓存（PIL）
        self._tile_base: dict[int, tuple] = {}   # page → (底图QPixmap, k, ox, oy)
        self._stamp_src: dict[int, QImage] = {}  # 图片下标 → 原生 QImage
        self._stamp_scaled: dict[tuple, QPixmap] = {}  # (下标, w, h) → 缩放缓存
        self.setWindowTitle(pdf.name)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setGeometry(QApplication.primaryScreen().availableGeometry())
        # 样式：直接挂 GLOBAL_QSS（不依赖父窗口级联），仅补充滚动区透明
        self.setStyleSheet(
            GLOBAL_QSS
            + "QScrollArea{background:transparent;border:0;}"
              "QScrollArea > QWidget > QWidget {background:transparent;}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_dialog_titlebar())
        inner = QWidget()
        root = QHBoxLayout(inner)
        root.setContentsMargins(18, 12, 18, 14)
        root.setSpacing(12)
        outer.addWidget(inner, 1)

        # ---- 左侧：水印图片选择 + 布局控件 ----
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(QLabel("水印图片（拖动所选图移动位置）"))
        self.img_list = QListWidget()
        self.img_list.setMaximumHeight(140)
        for p in self.image_paths:
            self.img_list.addItem(Path(p).name)
        self.img_list.setCurrentRow(self.current_image_index)
        self.img_list.currentRowChanged.connect(self._on_image_selected)
        left.addWidget(self.img_list)

        left.addWidget(QLabel("水印大小"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("页面宽度百分比", MODE_PERCENT)
        self.mode_combo.addItem("原始尺寸", MODE_ORIGINAL)
        self.mode_combo.addItem("固定宽度 (pt)", MODE_FIXED)
        self.mode_combo.addItem("长宽尺寸 (cm)", MODE_CM)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        left.addWidget(self.mode_combo)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(1, 200)
        self.slider.valueChanged.connect(self._on_percent_changed)
        left.addWidget(self.slider)
        fixed_row = QHBoxLayout()
        fixed_row.addWidget(QLabel("宽"))
        self.fixed_width_spin = QDoubleSpinBox()
        self.fixed_width_spin.setRange(1.0, 2000.0)
        self.fixed_width_spin.setDecimals(1)
        self.fixed_width_spin.setSuffix(" pt")
        self.fixed_width_spin.valueChanged.connect(self._on_fixed_width_changed)
        fixed_row.addWidget(self.fixed_width_spin)
        self.fixed_row_widget = QWidget()
        self.fixed_row_widget.setLayout(fixed_row)
        left.addWidget(self.fixed_row_widget)
        # cm 模式：宽×高 输入
        cm_row = QHBoxLayout()
        self.cm_w_spin = QDoubleSpinBox()
        self.cm_w_spin.setRange(0.1, 60.0)
        self.cm_w_spin.setDecimals(1)
        self.cm_w_spin.setSuffix(" cm")
        self.cm_w_spin.setValue(5.0)
        self.cm_w_spin.valueChanged.connect(self._on_cm_changed)
        cm_row.addWidget(QLabel("宽"))
        cm_row.addWidget(self.cm_w_spin)
        self.cm_h_spin = QDoubleSpinBox()
        self.cm_h_spin.setRange(0.1, 60.0)
        self.cm_h_spin.setDecimals(1)
        self.cm_h_spin.setSuffix(" cm")
        self.cm_h_spin.setValue(5.0)
        self.cm_h_spin.valueChanged.connect(self._on_cm_changed)
        cm_row.addWidget(QLabel("高"))
        cm_row.addWidget(self.cm_h_spin)
        self.cm_row_widget = QWidget()
        self.cm_row_widget.setLayout(cm_row)
        left.addWidget(self.cm_row_widget)
        left.addWidget(QLabel(""))   # 留白

        op_row = QHBoxLayout()
        op_row.addWidget(QPushButton("全部添加", clicked=self.add_all))
        op_row.addWidget(QPushButton("全部取消", clicked=self.clear_all))
        left.addLayout(op_row)
        left.addWidget(QPushButton("缩小", clicked=self.close))
        left.addStretch(1)
        left_wrap = QWidget()
        left_wrap.setLayout(left)
        left_wrap.setFixedWidth(230)
        root.addWidget(left_wrap)

        # ---- 右侧：可滚动页面网格 ----
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.body)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        root.addWidget(self.scroll, 1)

        self.total_label = QLabel()
        self.total_label.setAlignment(Qt.AlignCenter)
        # 右键空白处返回首页（事件过滤器，拦截非瓦片区域的右键）
        self._blank_right_click_enabled = True
        self.installEventFilter(self)
        self.scroll.viewport().installEventFilter(self)
        self.body.installEventFilter(self)
        self.img_list.viewport().installEventFilter(self)
        # 布局稳定（弹窗显示后）再排布渲染，避免宽度未布局导致单列竖排/空白
        QTimer.singleShot(0, self._initial_layout)

    # ---- 弹窗标题栏（与首页同款，深色 44px） ----
    def _build_dialog_titlebar(self) -> QWidget:
        return build_dialog_titlebar(self.pdf.name, f"·  {len(self.pdf.pages)} 页",
                                     self.close)

    # ---- 右键空白处返回首页 ----
    def eventFilter(self, obj, event) -> bool:
        if self._blank_right_click_enabled and event.type() == QEvent.MouseButtonPress:
            if event.button() == Qt.RightButton:
                # 逐个判断是否落在任何瓦片上；不在瓦片上则视为空白 → 关闭返回首页
                pos = event.globalPosition().toPoint()
                on_tile = any(t.isVisible()
                              and t.rect().contains(t.mapFromGlobal(pos))
                              for t in self.tiles)
                if not on_tile:
                    event.accept()
                    self.close()
                    return True
        return super().eventFilter(obj, event)

    # ---- 动态换排：根据可用宽度计算列数 ----
    def _cols(self) -> int:
        avail_w = self.scroll.viewport().width()
        if avail_w <= 0:
            # 未布局时用弹窗宽度估算
            avail_w = self.width() - 230 - 40
        per = PageTile.TILE_W + 24 + 12
        return max(1, avail_w // per)

    def _initial_layout(self) -> None:
        self._reflow()
        self.render()

    def _reflow(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.tiles = []
        cols = self._cols()
        for p in range(len(self.pdf.pages)):
            tile = PageTile(self, p)
            self.tiles.append(tile)
            # 先横排填满一行，再换行（row-major）
            self.grid.addWidget(tile, p // cols, p % cols)
        for c in range(cols):
            self.grid.setColumnStretch(c, 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 宽度变化后重建瓦片并完整重渲染，保证始终横排铺满、无空白
        self._reflow()
        self.render()

    def paintEvent(self, event) -> None:
        """弹窗品牌背景与首页一致：戳个印图标 + slogan + powered by。"""
        super().paintEvent(event)   # 先画基类背景（#0f172a）
        painter = QPainter(self)
        paint_brand_background(painter, self.width(), self.height(),
                               content_left=260, wf_alpha=0.06)
        painter.end()

    # ---- 页面背景缓存 ----
    def _background(self, page: int):
        if page not in self._bg_cache:
            if self.pdf.path and os.path.exists(self.pdf.path):
                try:
                    _, _, pil = render_page_bg(Path(self.pdf.path), page,
                                               zoom=PREVIEW_ZOOM)
                    self._bg_cache[page] = pil
                except Exception:
                    self._bg_cache[page] = None
            else:
                self._bg_cache[page] = None
        return self._bg_cache[page]

    # ---- 渲染 ----
    def render(self) -> None:
        self.total_label.setText(f"总计 {len(self.pdf.pages)} 页")
        for i, tile in enumerate(self.tiles):
            tile.set_active(i == self.current_page)
            self.rerender_tile(i)
        self._sync_size_controls()

    def rerender_tile(self, page: int) -> None:
        """只重绘第 page 页（拖动时高频调用）。

        优先走 Qt 缓存合成（毫秒级）；无背景缓存时回退到整页重渲染。
        """
        if not (0 <= page < len(self.tiles)):
            return
        self._composite_quick(page, self.tiles[page])

    # ---- Qt 快速合成（拖动/常规渲染共用，不触碰 PDF 与 PIL） ----
    def _tile_geo(self, page: int):
        """返回 (底图QPixmap, k, ox, oy)；k=每 pt 对应的瓦片像素，
        (ox, oy)=页面位图在瓦片画布内的左上角位置。无背景返回 None。"""
        if page in self._tile_base:
            return self._tile_base[page]
        bg = self._background(page)
        if bg is None:
            return None
        bw, bh = bg.size
        img = QImage(bg.tobytes(), bw, bh, bw * 4, QImage.Format_RGBA8888).copy()
        base = QPixmap(PageTile.TILE_W, PageTile.TILE_H)
        base.fill(QColor("#0f172a"))
        painter = QPainter(base)
        scaled = QPixmap.fromImage(img).scaled(
            PageTile.TILE_W, PageTile.TILE_H,
            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        ox = (PageTile.TILE_W - scaled.width()) // 2
        oy = (PageTile.TILE_H - scaled.height()) // 2
        painter.drawPixmap(ox, oy, scaled)
        painter.end()
        k = scaled.width() / bw            # 瓦片像素 / 位图像素
        geo = (base, k, ox, oy)
        self._tile_base[page] = geo
        return geo

    def _pt_per_px(self, page: int) -> Optional[float]:
        """拖动换算系数：每个控件像素对应多少 pt。"""
        geo = self._tile_geo(page)
        if geo is None:
            return None
        return 1.0 / (PREVIEW_ZOOM * geo[1])

    def _stamp_rect_px(self, st: PageState, page: int, idx: int, path: str):
        """水印图 idx 在第 page 页瓦片上的矩形（瓦片像素）。

        位置取本图自己的偏移（image_offsets，缺省回落 dx/dy）——
        拖动写入 image_offsets，这里必须跟着读，否则拖动不生效。
        pt → 瓦片像素的换算系数是 PREVIEW_ZOOM×k（k 为瓦片像素/位图像素），
        与拖动换算 _pt_per_px 互为倒数，保证三处基准一致。
        """
        geo = self._tile_geo(page)
        if geo is None:
            return None
        _, k, ox, oy = geo
        bg = self._background(page)
        bw, bh = bg.size
        pt_w, pt_h = bw / PREVIEW_ZOOM, bh / PREVIEW_ZOOM
        iw, ih = image_native_size(Path(path))
        dx, dy = st.image_offsets.get(idx, (st.dx, st.dy))
        local = replace(st, wm=True, dx=dx, dy=dy, image_offsets={})
        lx, ly, lw, lh = rect_for(page_layout(local, 1.0, image_path=path,
                                              native_size=(iw, ih)),
                                  pt_w, pt_h, iw, ih)
        scale = PREVIEW_ZOOM * k
        return (lx * scale + ox, ly * scale + oy, lw * scale, lh * scale)

    def _stamp_pix(self, idx: int, path: str, w: int, h: int) -> QPixmap:
        """按目标尺寸缩放水印图（带缓存；w/h 相同即命中）。"""
        key = (idx, w, h)
        cached = self._stamp_scaled.get(key)
        if cached is not None:
            return cached
        src = self._stamp_src.get(idx)
        if src is None:
            from PIL import Image
            with Image.open(path) as im:
                rgba = im.convert("RGBA")
            src = QImage(rgba.tobytes(), rgba.width, rgba.height,
                         rgba.width * 4, QImage.Format_RGBA8888).copy()
            self._stamp_src[idx] = src
        if len(self._stamp_scaled) > 96:   # 防止尺寸反复调整时缓存膨胀
            self._stamp_scaled.clear()
        pix = QPixmap.fromImage(src).scaled(
            max(1, w), max(1, h), Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation)
        self._stamp_scaled[key] = pix
        return pix

    def _composite_quick(self, page: int, tile: PageTile) -> None:
        """把底图与全部水印图合成到瓦片预览（全部命中缓存，毫秒级）。"""
        geo = self._tile_geo(page)
        if geo is None:
            pix = QPixmap(PageTile.TILE_W, PageTile.TILE_H)
            render_page(pix, self.pdf, page, self.image_paths)
            tile.set_preview(pix)
            return
        base = geo[0].copy()
        st = self.pdf.pages[page]
        if st.wm and self.image_paths:
            painter = QPainter(base)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.setOpacity(st.opacity)
            for idx, path in enumerate(self.image_paths):
                try:
                    rect = self._stamp_rect_px(st, page, idx, path)
                    if rect is None or rect[2] < 1 or rect[3] < 1:
                        continue
                    stamp = self._stamp_pix(idx, path,
                                            max(1, round(rect[2])),
                                            max(1, round(rect[3])))
                    painter.drawPixmap(QPointF(rect[0], rect[1]), stamp)
                except Exception:
                    continue   # 单张图片缺失时跳过，不影响其他水印
            painter.end()
        tile.set_preview(base)

    # ---- 当前页 / 当前图片 ----
    def set_current(self, page: int) -> None:
        if not (0 <= page < len(self.pdf.pages)):
            return
        self.current_page = page
        for i, tile in enumerate(self.tiles):
            tile.set_active(i == self.current_page)
        self._sync_size_controls()

    def _on_image_selected(self, row: int) -> None:
        if 0 <= row < len(self.image_paths):
            self.current_image_index = row
            self.render()
            self.on_change()

    # ---- 尺寸控件 ----
    def _sync_size_controls(self) -> None:
        st = self.pdf.pages[self.current_page]
        mode = st.size_mode
        idx = self.mode_combo.findData(mode)
        if idx < 0:
            idx = 0
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(idx)
        self.mode_combo.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setValue(int(st.percent))
        self.slider.blockSignals(False)
        self.slider.setEnabled(mode == MODE_PERCENT)
        self.fixed_row_widget.setVisible(mode == MODE_FIXED)
        self.fixed_width_spin.blockSignals(True)
        self.fixed_width_spin.setValue(st.fixed_width)
        self.fixed_width_spin.blockSignals(False)
        self.cm_row_widget.setVisible(mode == MODE_CM)
        self.cm_w_spin.blockSignals(True)
        self.cm_w_spin.setValue(st.cm_w)
        self.cm_w_spin.blockSignals(False)
        self.cm_h_spin.blockSignals(True)
        self.cm_h_spin.setValue(st.cm_h)
        self.cm_h_spin.blockSignals(False)

    def _on_mode_changed(self, idx: int) -> None:
        mode = self.mode_combo.itemData(idx)
        st = self.pdf.pages[self.current_page]
        st.size_mode = mode
        self.slider.setEnabled(mode == MODE_PERCENT)
        self.fixed_row_widget.setVisible(mode == MODE_FIXED)
        self.cm_row_widget.setVisible(mode == MODE_CM)
        self.render()
        self.on_change()

    def _on_percent_changed(self, value: int) -> None:
        st = self.pdf.pages[self.current_page]
        st.percent = float(value)
        self.render()
        self.on_change()

    def _on_cm_changed(self, *_args) -> None:
        st = self.pdf.pages[self.current_page]
        st.cm_w = self.cm_w_spin.value()
        st.cm_h = self.cm_h_spin.value()
        self.render()
        self.on_change()

    def _on_fixed_width_changed(self, value: float) -> None:
        st = self.pdf.pages[self.current_page]
        st.fixed_width = value
        self.render()
        self.on_change()

    # ---- 页面水印操作 ----
    def open_fullscreen(self, page: int) -> None:
        """打开单页全屏视图；关闭后同步刷新本网格中该页瓦片。"""
        if not (0 <= page < len(self.pdf.pages)):
            return
        self.set_current(page)
        dialog = FullScreenPageDialog(
            self.pdf,
            self.on_change,
            image_paths=self.image_paths,
            selected_image_index=self.current_image_index,
            page=page,
            parent=self,
        )
        # 保住 Python 引用：构造里挂起的 QTimer.singleShot(0, ...) 需要
        # 包装器存活；exec 被替换/未跑事件循环时提前回收会访问违例。
        self._fs_dialog = dialog
        dialog.exec()
        self.rerender_tile(page)
        self.set_current(page)

    def toggle_page(self, page: int) -> None:
        self.pdf.pages[page].wm = not self.pdf.pages[page].wm
        self.render()
        self.on_change()

    def add_all(self) -> None:
        for p in self.pdf.pages:
            p.wm = True
        self.render()
        self.on_change()

    def clear_all(self) -> None:
        for p in self.pdf.pages:
            p.wm = False
        self.render()
        self.on_change()


# ---------------------------------------------------------------- 单页全屏视图
class FullScreenPageDialog(QDialog):
    """单页全屏查看与编辑。

    交互（与网格视图同一套坐标合同，所有位移/尺寸仍以 PDF pt 记录）：
    - 右键点击页面：切换本页 添加/不添加 图章；
    - 按住左键拖动：移动当前选中水印图；
    - 滚轮：调节图章大小（百分比/厘米/固定宽度模式生效）；
    - 左右悬浮按钮（或 ←/→ 方向键）：上一页、下一页；
    - 右键页面外空白：返回页面网格。
    """

    MARGIN = 56          # 页面与窗口边缘的最小留白
    SIDE_BUTTON_W = 96   # 悬浮翻页按钮宽度

    def __init__(self, pdf: MockPdf, on_change, image_paths=None,
                 selected_image_index: int = 0, page: int = 0, parent=None):
        super().__init__(parent)
        self.pdf = pdf
        self.on_change = on_change
        self.image_paths = list(image_paths or [])
        self.current_image_index = max(0, selected_image_index)
        self.current_page = max(0, min(page, len(pdf.pages) - 1))
        self._bg_cache: dict[int, object] = {}
        self._stamp_src: dict[int, QImage] = {}
        self._stamp_scaled: dict[tuple, QPixmap] = {}
        self._display_base: Optional[QPixmap] = None   # 底图缩放缓存
        self._display_geo: Optional[tuple] = None      # (page, w, h)
        self.press: Optional[QPointF] = None
        self.press_page = -1
        self.dragging = False
        self.base_dx = 0.0
        self.base_dy = 0.0

        self.setWindowTitle(pdf.name)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setGeometry(QApplication.primaryScreen().availableGeometry())
        self.setStyleSheet(GLOBAL_QSS
                           + "QDialog{background:#0b1220;}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._title_bar = build_dialog_titlebar(
            pdf.name, "", self.close)
        # 副标题换成页码指示
        self.page_label = self._title_bar.findChild(QLabel, "dlg_subtitle")
        outer.addWidget(self._title_bar)
        outer.addStretch(1)   # 标题栏置顶，其余空间全部留给页面绘制

        # ---- 左右悬浮翻页按钮 ----
        self.btn_prev = QPushButton("‹  上一页", self)
        self.btn_next = QPushButton("下一页  ›", self)
        self.btn_prev.setCursor(Qt.PointingHandCursor)
        self.btn_next.setCursor(Qt.PointingHandCursor)
        self.btn_prev.clicked.connect(self._go_prev)
        self.btn_next.clicked.connect(self._go_next)
        for btn in (self.btn_prev, self.btn_next):
            # 固定尺寸：默认 sizeHint 在无边框窗口下高度不足，文字被裁剪
            btn.setFixedSize(112, 40)
            btn.setStyleSheet(
                "QPushButton{background:rgba(18,27,44,0.72);"
                "color:#c7d2e0;border:1px solid #2a3a57;"
                "border-radius:20px;padding:0;font-size:13px;}"
                "QPushButton:hover{background:rgba(34,49,84,0.85);"
                "color:#ffffff;border-color:#3c5176;}"
                "QPushButton:disabled{color:#54627a;"
                "background:rgba(19,29,48,0.55);border-color:#1d2942;}")
        # 几何已由 setGeometry 定死，直接同步初始化即可；
        # 延迟 singleShot 在 exec 被替换/包装器提前回收时会访问违例。
        self._first_layout()

    # ---- 布局 ----
    def _first_layout(self) -> None:
        self._place_side_buttons()
        self.set_current(self.current_page)

    def _update_page_label(self) -> None:
        total = len(self.pdf.pages)
        if self.page_label is not None:
            self.page_label.setText(f"·  第 {self.current_page + 1} / {total} 页")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._display_base = None
        self._display_geo = None
        self._place_side_buttons()
        self._rerender()

    def _place_side_buttons(self) -> None:
        if not hasattr(self, "btn_prev"):
            return
        y = (self.height() - self.btn_prev.height()) // 2
        self.btn_prev.move(20, y)
        self.btn_next.move(self.width() - 20 - self.btn_next.width(), y)

    # ---- 页面显示矩形 / 换算 ----
    def _page_rect(self) -> QRectF:
        """当前页位图在窗口内的显示矩形（保持纵横比、居中）。"""
        bg = self._background(self.current_page)
        title_h = self._title_bar.height() if self._title_bar else 44
        if bg is not None:
            bw, bh = bg.size
        else:
            bw, bh = PAGE_W * PREVIEW_ZOOM, PAGE_H * PREVIEW_ZOOM
        avail_w = max(60.0, self.width() - 2 * self.MARGIN)
        avail_h = max(60.0, self.height() - title_h - 2 * self.MARGIN)
        scale = min(avail_w / bw, avail_h / bh)
        w, h = bw * scale, bh * scale
        x = (self.width() - w) / 2
        y = title_h + (self.height() - title_h - h) / 2
        return QRectF(x, y, w, h)

    def _pt_per_px(self) -> Optional[float]:
        """全屏视图中每个控件像素对应的 PDF 点（pt）。"""
        rect = self._page_rect()
        if rect.width() <= 0:
            return None
        bg = self._background(self.current_page)
        pt_w = (bg.size[0] / PREVIEW_ZOOM) if bg is not None else PAGE_W
        return pt_w / rect.width()

    # ---- 渲染 ----
    def _background(self, page: int):
        if page not in self._bg_cache:
            if self.pdf.path and os.path.exists(self.pdf.path):
                try:
                    _, _, pil = render_page_bg(Path(self.pdf.path), page,
                                               zoom=PREVIEW_ZOOM)
                    self._bg_cache[page] = pil
                except Exception:
                    self._bg_cache[page] = None
            else:
                self._bg_cache[page] = None
        return self._bg_cache[page]

    def _stamp_rect_view(self, st: PageState, page: int, idx: int, path: str):
        """水印图 idx 在全屏显示矩形中的矩形（窗口像素）。"""
        bg = self._background(page)
        if bg is None:
            return None
        rect = self._page_rect()
        bw, bh = bg.size
        pt_w, pt_h = bw / PREVIEW_ZOOM, bh / PREVIEW_ZOOM
        iw, ih = image_native_size(Path(path))
        dx, dy = st.image_offsets.get(idx, (st.dx, st.dy))
        local = replace(st, wm=True, dx=dx, dy=dy, image_offsets={})
        lx, ly, lw, lh = rect_for(page_layout(local, 1.0, image_path=path,
                                              native_size=(iw, ih)),
                                  pt_w, pt_h, iw, ih)
        k_disp = rect.width() / bw            # 显示像素 / 位图像素
        scale = PREVIEW_ZOOM * k_disp         # 显示像素 / pt
        return (rect.x() + lx * scale, rect.y() + ly * scale,
                lw * scale, lh * scale)

    def _stamp_pix(self, idx: int, path: str, w: int, h: int) -> QPixmap:
        """按目标尺寸缩放水印图（带缓存；w/h 相同即命中）。"""
        key = (idx, w, h)
        cached = self._stamp_scaled.get(key)
        if cached is not None:
            return cached
        src = self._stamp_src.get(idx)
        if src is None:
            from PIL import Image
            with Image.open(path) as im:
                rgba = im.convert("RGBA")
            src = QImage(rgba.tobytes(), rgba.width, rgba.height,
                         rgba.width * 4, QImage.Format_RGBA8888).copy()
            self._stamp_src[idx] = src
        if len(self._stamp_scaled) > 96:
            self._stamp_scaled.clear()
        pix = QPixmap.fromImage(src).scaled(
            max(1, w), max(1, h), Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation)
        self._stamp_scaled[key] = pix
        return pix

    def _display_base_pix(self) -> Optional[QPixmap]:
        """当前页底图缩放到显示矩形（缓存；拖动/滚轮时直接复用）。"""
        page, rect = self.current_page, self._page_rect()
        w, h = int(rect.width()), int(rect.height())
        if (self._display_base is not None
                and self._display_geo == (page, w, h)):
            return self._display_base
        bg = self._background(page)
        base = QPixmap(max(1, w), max(1, h))
        base.fill(QColor("#0f172a"))
        if bg is not None:
            img = QImage(bg.tobytes(), bg.size[0], bg.size[1],
                         bg.size[0] * 4, QImage.Format_RGBA8888).copy()
            painter = QPainter(base)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            scaled = QPixmap.fromImage(img).scaled(
                base.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap((base.width() - scaled.width()) // 2,
                                (base.height() - scaled.height()) // 2,
                                scaled)
            painter.end()
        else:
            # 无真实 PDF：复用模拟绘制（白色页面 + 模拟水印）
            render_page(base, self.pdf, page, self.image_paths)
        self._display_base = base
        self._display_geo = (page, w, h)
        return base

    def _rerender(self) -> None:
        """重绘当前页：底图缓存永远保持干净，水印只画在当帧副本上。

        _display_base_pix() 返回的是跨帧复用的缓存 QPixmap 本体；
        若直接在上面画水印，每次拖动/滚轮都会把水印叠加进缓存，
        形成拖影/花屏（网格视图 _composite_quick 用 geo[0].copy()，
        这里必须同样先拷贝再绘制）。
        """
        base = self._display_base_pix()
        self._pix = base.copy() if base is not None else None
        st = self.pdf.pages[self.current_page]
        if st.wm and self.image_paths and self._pix is not None:
            view_rect = self._page_rect()
            ox, oy = view_rect.x(), view_rect.y()
            painter = QPainter(self._pix)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.setOpacity(st.opacity)
            for idx, path in enumerate(self.image_paths):
                try:
                    rect = self._stamp_rect_view(st, self.current_page,
                                                 idx, path)
                    if rect is None or rect[2] < 1 or rect[3] < 1:
                        continue
                    # _stamp_rect_view 返回窗口坐标；_pix 是页面位图，
                    # 必须减去页面原点，否则图章被位移并遭位图边界裁剪。
                    stamp = self._stamp_pix(idx, path,
                                            max(1, round(rect[2])),
                                            max(1, round(rect[3])))
                    painter.drawPixmap(QPointF(rect[0] - ox, rect[1] - oy),
                                       stamp)
                except Exception:
                    continue   # 单张图片缺失时跳过，不影响其他水印
            painter.end()
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        paint_brand_background(painter, self.width(), self.height(),
                               content_left=0, wf_alpha=0.05)
        rect = self._page_rect()
        if self._pix is not None and not self._pix.isNull():
            # 页面投影 + 圆角裁剪
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 90))
            painter.drawRoundedRect(
                QRectF(rect.x() + 6, rect.y() + 8, rect.width(),
                       rect.height()), 8, 8)
            path = QPainterPath()
            path.addRoundedRect(rect, 8, 8)
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(rect.topLeft(), self._pix)
            painter.restore()
            painter.setPen(QPen(QColor("#24324e"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, 8, 8)
        painter.end()

    # ---- 当前页 ----
    def set_current(self, page: int) -> None:
        if not (0 <= page < len(self.pdf.pages)):
            return
        self.current_page = page
        self._display_base = None
        self._display_geo = None
        self._update_page_label()
        self.btn_prev.setEnabled(page > 0)
        self.btn_next.setEnabled(page < len(self.pdf.pages) - 1)
        self._rerender()

    def _go_prev(self) -> None:
        self.set_current(self.current_page - 1)

    def _go_next(self) -> None:
        self.set_current(self.current_page + 1)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Left:
            self._go_prev()
            return
        if event.key() == Qt.Key_Right:
            self._go_next()
            return
        super().keyPressEvent(event)

    # ---- 页面水印操作 ----
    def toggle_page(self, page: int) -> None:
        self.pdf.pages[page].wm = not self.pdf.pages[page].wm
        self._rerender()
        self.on_change()

    def adjust_stamp_size(self, steps: int) -> None:
        """滚轮调大小：以步进调节当前页图章尺寸（页面级参数）。"""
        if not steps or not (0 <= self.current_page < len(self.pdf.pages)):
            return
        st = self.pdf.pages[self.current_page]
        if not st.wm:
            return
        if st.size_mode == MODE_PERCENT:
            st.percent = min(200.0, max(1.0, st.percent + 2.0 * steps))
        elif st.size_mode == MODE_CM:
            delta = 0.2 * steps
            st.cm_w = min(60.0, max(0.1, st.cm_w + delta))
            st.cm_h = min(60.0, max(0.1, st.cm_h + delta))
        elif st.size_mode == MODE_FIXED:
            st.fixed_width = min(2000.0, max(1.0, st.fixed_width + 10.0 * steps))
        else:   # MODE_ORIGINAL：无尺寸参数
            return
        self._rerender()
        self.on_change()

    # ---- 鼠标 / 滚轮 ----
    def mousePressEvent(self, event) -> None:
        pos = QPointF(event.position())
        rect = self._page_rect()
        inside = rect.contains(pos)
        if event.button() == Qt.RightButton:
            if inside:
                self.toggle_page(self.current_page)
            else:
                self.close()          # 右键页面外空白：返回页面网格
            return
        if event.button() == Qt.LeftButton and inside:
            self.press = pos
            self.press_page = self.current_page
            self.dragging = False

    def mouseMoveEvent(self, event) -> None:
        if self.press is None:
            return
        dx = event.position().x() - self.press.x()
        dy = event.position().y() - self.press.y()
        if not self.dragging and (abs(dx) > 3 or abs(dy) > 3):
            self.dragging = True
            st = self.pdf.pages[self.press_page]
            img_idx = self.current_image_index
            # 以该图片在该页上次保存的位置为基准（与网格拖动一致）
            self.base_dx, self.base_dy = st.image_offsets.get(
                img_idx, (st.dx, st.dy))
            st.image_offsets[img_idx] = (self.base_dx, self.base_dy)
        if not self.dragging:
            return
        st = self.pdf.pages[self.press_page]
        img_idx = self.current_image_index
        k = self._pt_per_px()
        if k is None:
            k = 1.0
        st.image_offsets[img_idx] = (self.base_dx + dx * k,
                                     self.base_dy + dy * k)
        self._rerender()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.press is not None:
            if self.dragging:
                self.on_change()
            self.press = None
            self.dragging = False
            self.press_page = -1

    def wheelEvent(self, event) -> None:
        steps = int(round(event.angleDelta().y() / 120.0))
        if steps:
            self.adjust_stamp_size(steps)
        event.accept()


# ---------------------------------------------------------------- 主窗口
class CardsBody(QWidget):
    """首页卡片流容器；没有文档时绘制虚线「拖放导入」空状态。"""

    def __init__(self, is_empty, parent=None):
        super().__init__(parent)
        self._is_empty = is_empty
        self.setStyleSheet("background:transparent;")

    def paintEvent(self, event) -> None:
        if not self._is_empty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        zone_w, zone_h = min(560, int(w * 0.72)), min(360, int(h * 0.72))
        zone = QRectF((w - zone_w) / 2, (h - zone_h) / 2, zone_w, zone_h)
        pen = QPen(QColor(64, 84, 122, 200), 1.5, Qt.DashLine)
        pen.setDashPattern([6, 5])
        painter.setPen(pen)
        painter.setBrush(QColor(255, 255, 255, 4))
        painter.drawRoundedRect(zone, 16, 16)

        cx = zone.center().x()
        icon = Brand.icon()
        if icon is not None:
            scaled = icon.scaled(56, 56, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
            painter.setOpacity(0.85)
            painter.drawPixmap(int(cx - scaled.width() / 2),
                               int(zone.y() + zone_h * 0.20), scaled)
            painter.setOpacity(1.0)
        font = painter.font()
        font.setPointSizeF(13)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#aebdd2"))
        painter.drawText(QRectF(zone.x(), zone.y() + zone_h * 0.44, zone_w, 30),
                         Qt.AlignHCenter, "把 PDF 或水印图片拖到这里")
        font.setBold(False)
        font.setPointSizeF(10.5)
        painter.setFont(font)
        painter.setPen(QColor(124, 143, 168, 220))
        painter.drawText(
            QRectF(zone.x(), zone.y() + zone_h * 0.44 + 34, zone_w, 26),
            Qt.AlignHCenter,
            "或点击左侧「添加 PDF…」——支持 PDF / PNG / JPG")


def asset_base() -> Path:
    """返回资源根目录。

    Nuitka（standalone/onefile）：随包数据落在程序目录的相对路径下，
    直接用 __file__ 所在目录；兼容 PyInstaller 的 _MEIPASS。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def srcdir() -> Path:
    """返回品牌素材目录（pdf_watermark_studio/src）。"""
    return asset_base() / "pdf_watermark_studio" / "src"


# ---------------------------------------------------------------- 品牌视觉（首页与弹窗共用）
APP_SLOGAN = "戳一下，不加班"
POWERED_BY = "powered by 野草智能（Wild Field）"
CONTACT_PHONE = "15620126846"


class Brand:
    """懒加载并缓存品牌素材，供首页与弹窗共用同一套背景。"""
    _icon: Optional[QPixmap] = None   # 戳个印.png
    _wf: Optional[QPixmap] = None     # Wild Field 套件图（半透明底图）
    _loaded = False

    @classmethod
    def ensure(cls) -> None:
        if cls._loaded:
            return
        cls._loaded = True
        src = srcdir()
        icon_path = src / "戳个印.png"
        if icon_path.exists():
            pm = QPixmap(str(icon_path))
            if not pm.isNull():
                cls._icon = pm
        wf = src / "Wild_Field"
        candidates = [
            wf / "wild-field-logo-transparent-master.png",
            wf / "wild-field-header-2400x600.png",
            wf / "wild-field-logo-transparent-1024w.png",
        ]
        for c in candidates:
            if c.exists():
                pm = QPixmap(str(c))
                if not pm.isNull():
                    cls._wf = pm
                    break

    @classmethod
    def icon(cls) -> Optional[QPixmap]:
        cls.ensure()
        return cls._icon

    @classmethod
    def wf(cls) -> Optional[QPixmap]:
        cls.ensure()
        return cls._wf


def paint_brand_background(painter: QPainter, w: int, h: int,
                           *, content_left: int = 0,
                           wf_alpha: float = 0.10,
                           icon_alpha: float = 0.55) -> None:
    """统一品牌背景：深色基底 + Wild Field 水印 + powered by。

    首页与弹窗共用；content_left 为内容区起始 x，Wild Field Logo
    在内容区（卡片区）水平居中。
    """
    # 基底
    painter.fillRect(QRectF(0, 0, w, h), QColor("#0b1220"))

    # Wild Field 底图（半透明，卡片区中部偏下）
    wf = Brand.wf()
    if wf is not None:
        wf_w = int(w * 0.6)
        wf_h = int(h * 0.4)
        scaled_wf = wf.scaled(wf_w, wf_h, Qt.KeepAspectRatio,
                              Qt.SmoothTransformation)
        painter.setOpacity(wf_alpha)
        painter.drawPixmap(content_left + (w - content_left
                                           - scaled_wf.width()) // 2,
                           int(h * 0.62) - scaled_wf.height() // 2,
                           scaled_wf)
        painter.setOpacity(1.0)

    # 戳个印图标（居中偏上，醒目）
    # icon = Brand.icon()
    # if icon is not None:
    #     target = int(min(w, h) * icon_pct)
    #     scaled_icon = icon.scaled(target, target, Qt.KeepAspectRatio,
    #                               Qt.SmoothTransformation)
    #     painter.setOpacity(icon_alpha)
    #     ix = (w - scaled_icon.width()) // 2
    #     iy = int(h * 0.18)
    #     painter.drawPixmap(ix, iy, scaled_icon)
    #     painter.setOpacity(1.0)

    # slogan（图标下方，大而醒目）
    # painter.setPen(QColor("#e2e8f0"))
    # font = painter.font()
    # font.setPointSizeF(17)
    # font.setBold(True)
    # painter.setFont(font)
    # slogan_y = int(h * 0.30)
    # painter.drawText(QRectF(0, slogan_y, w, 40), Qt.AlignCenter, APP_SLOGAN)

    # powered by（右下角小字）
    painter.setPen(QColor(148, 163, 184, 220))
    small = painter.font()
    small.setPointSizeF(9)
    small.setBold(False)
    painter.setFont(small)
    painter.drawText(QRectF(0, h - 44, w - 18, 18), Qt.AlignRight,
                     POWERED_BY)


# ---------------------------------------------------------------- 全局样式
# 视觉基调：墨蓝深色 + 印章红点缀（戳个印品牌色）
GLOBAL_QSS = """
QMainWindow, QDialog { background: #0b1220; }
QLabel { color: #c7d2e0; background: transparent; }
QGroupBox {
    background: #121b2c; border: 1px solid #1d2942; border-radius: 12px;
    margin-top: 16px; padding: 8px 10px 8px 10px; font-weight: normal;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 14px; top: 3px; padding: 0 4px;
    color: #8ba0b8; font-size: 12px; letter-spacing: 1px;
}
QListWidget {
    background: #0d1524; border: 1px solid #21304b; border-radius: 8px;
    color: #c7d2e0; padding: 4px; outline: 0; font-size: 12px;
    alternate-background-color: #0d1524;
}
QListWidget::item { padding: 6px 8px; border-radius: 6px; margin: 1px 2px; }
QListWidget::item:hover { background: #16233a; }
QListWidget::item:selected {
    background: rgba(185, 58, 78, 0.30); color: #ffffff;
}
QPushButton {
    background: #1a2740; color: #dbe4f0; border: 1px solid #2a3a57;
    border-radius: 8px; padding: 6px 14px; font-size: 12px;
}
QPushButton:hover { background: #223154; border-color: #3c5176; }
QPushButton:pressed { background: #141f34; }
QPushButton:disabled { color: #54627a; background: #131d30; border-color: #1d2942; }
QPushButton#btn_primary {
    background: #b93a4e; border-color: #b93a4e; color: white; font-weight: 600;
}
QPushButton#btn_primary:hover { background: #cf4258; border-color: #cf4258; }
QPushButton#btn_primary:pressed { background: #9c2f41; }
QSlider { min-height: 20px; }
QSlider::groove:horizontal { height: 4px; background: #223049; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
    background: #d24356; border: 0;
}
QSlider::handle:horizontal:hover { background: #e35a6d; }
QSlider::sub-page:horizontal { background: rgba(210, 67, 86, 0.45); border-radius: 2px; }
QSpinBox, QDoubleSpinBox {
    background: #0d1524; color: #dbe4f0; border: 1px solid #21304b;
    border-radius: 8px; padding: 4px 8px; selection-background-color: #2a4a6e;
}
QComboBox {
    background: #1a2740; color: #dbe4f0; border: 1px solid #2a3a57;
    border-radius: 8px; padding: 4px 10px;
}
QComboBox:hover { background: #223154; }
QComboBox::drop-down { border: 0; width: 22px; }
QComboBox::down-arrow {
    image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-top: 5px solid #8ba0b8;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background: #121b2c; color: #dbe4f0; border: 1px solid #2a3a57;
    border-radius: 8px; outline: 0;
    selection-background-color: rgba(185, 58, 78, 0.35);
}
QProgressBar {
    background: #0d1524; border: 1px solid #21304b; border-radius: 7px;
    height: 14px; text-align: center; color: #9fb1c7; font-size: 10px;
}
QProgressBar::chunk { background: #b93a4e; border-radius: 6px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #26354e; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #34486a; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #26354e; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #34486a; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QToolTip {
    background: #1a2740; color: #dbe4f0; border: 1px solid #2a3a57;
    padding: 4px 8px; border-radius: 6px; font-size: 11px;
}
QMessageBox { background: #121b2c; }
QMessageBox QLabel { color: #dbe4f0; font-size: 12px; }
QTextEdit, QPlainTextEdit {
    background: #0d1524; color: #dbe4f0; border: 1px solid #21304b;
    border-radius: 8px; selection-background-color: #2a4a6e;
}
"""


# ---------------------------------------------------------------- 标题栏
HTCLIENT, HTCAPTION = 1, 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
WM_NCHITTEST = 0x0084


class TitleButton(QPushButton):
    """矢量自绘窗口按钮：最小化/最大化/还原/关闭，悬停底色、图标变白。"""

    MIN, MAX, RESTORE, CLOSE = range(4)

    def __init__(self, kind: int, tip: str):
        super().__init__()
        self.kind = kind
        self.setFixedSize(44, 30)
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)

    def set_kind(self, kind: int) -> None:
        self.kind = kind
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hover = self.underMouse()
        down = self.isDown()
        close_red = self.kind == TitleButton.CLOSE
        if close_red:
            bg = QColor("#a92715" if down else "#c42b1c") if hover or down \
                else None
        else:
            bg = QColor("#2a3a57" if down else "#22304a") if hover or down \
                else None
        if bg is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(1, 1, self.width() - 2,
                                     self.height() - 2), 7, 7)
        p.setPen(QPen(QColor("white") if (hover or down) else QColor("#c3cede"),
                      1.3))
        cx, cy = self.rect().center().x(), self.rect().center().y()
        if self.kind == TitleButton.MIN:
            p.drawLine(int(cx - 5), int(cy + 2), int(cx + 5), int(cy + 2))
        elif self.kind == TitleButton.MAX:
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(cx - 4.5, cy - 4.5, 9, 9))
        elif self.kind == TitleButton.RESTORE:
            p.setBrush(QColor("#0b1220"))
            p.drawRect(QRectF(cx - 5.5, cy - 2.5, 8, 8))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(cx - 1.5, cy - 6.5, 8, 8))
        else:   # CLOSE
            p.drawLine(int(cx - 4), int(cy - 4), int(cx + 4), int(cy + 4))
            p.drawLine(int(cx - 4), int(cy + 4), int(cx + 4), int(cy - 4))
        p.end()


class TitleBar(QWidget):
    """自绘深色标题栏（高约56px）：品牌图标+标题 + 帮助/反馈 + 窗口按钮。

    拖动/贴边/双击最大化由 WM_NCHITTEST→HTCAPTION 原生处理；
    按钮区域保持 HTCLIENT，点击不受影响。
    """

    HEIGHT = 44

    def __init__(self, window: "DemoWindow"):
        super().__init__(window)
        self.win = window
        self.setFixedHeight(TitleBar.HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 4, 0)
        lay.setSpacing(10)
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(22, 22)
        if Brand.icon() is not None:
            icon_lbl.setPixmap(Brand.icon().scaled(
                22, 22, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lay.addWidget(icon_lbl)
        title = QLabel("戳个印")
        title.setObjectName("title_app")
        sub = QLabel(APP_SLOGAN)
        sub.setObjectName("title_sub")
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addStretch(1)

        def ghost(text: str, tip: str) -> QPushButton:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setObjectName("btn_ghost")
            return b

        btn_help = ghost("帮助", "使用说明")
        btn_help.clicked.connect(window._show_help)
        btn_feedback = ghost("反馈", "问题与建议")
        btn_feedback.clicked.connect(window._send_feedback)
        lay.addWidget(btn_help)
        lay.addWidget(btn_feedback)
        spacer = QWidget()
        spacer.setFixedWidth(8)
        lay.addWidget(spacer)

        self.btn_min = TitleButton(TitleButton.MIN, "最小化")
        self.btn_max = TitleButton(TitleButton.MAX, "最大化")
        self.btn_close = TitleButton(TitleButton.CLOSE, "关闭")
        self.btn_min.clicked.connect(window.showMinimized)
        self.btn_max.clicked.connect(window.toggle_maximized)
        self.btn_close.clicked.connect(window.close)
        for b in (self.btn_min, self.btn_max, self.btn_close):
            lay.addWidget(b)
        self.setStyleSheet(
            "TitleBar{background:#0b1220;border-bottom:1px solid #18233a;}"
            "TitleBar QLabel{background:transparent;}"
            "TitleBar QLabel#title_app{color:#e8eef7;font-size:13px;"
            "font-weight:600;}"
            "TitleBar QLabel#title_sub{color:#5c7089;font-size:11px;}"
            "TitleBar QPushButton{background:transparent;color:#8ba0b8;"
            "border:0;border-radius:6px;font-size:12px;}"
            "TitleBar QPushButton#btn_ghost{padding:2px 9px;}"
            "TitleBar QPushButton:hover{background:#1d2a42;color:#dbe4f0;}")


class DemoWindow(QMainWindow):
    APP_NAME = "戳个印  戳一下，不加班"

    def __init__(self):
        super().__init__()
        self.setWindowTitle(self.APP_NAME)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.resize(1180, 720)
        # 品牌设置：图标 + 统一品牌背景
        Brand.ensure()
        icon_path = srcdir() / "戳个印.png"
        if Brand.icon() is not None and icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        # 默认空白：文档与水印图都由用户自行添加
        self.files: List[MockPdf] = []
        self._images: List[str] = []
        self._output_dir: Optional[str] = ""
        # 某些机器上原生目录选择框「确定」后拿不到路径，本次运行内改用内建对话框
        self._native_dir_dialog_broken = False
        self.cards: List[PdfCard] = []
        self._export_active = False
        self._export_thread: Optional[QThread] = None
        self._export_worker: Optional[ExportWorker] = None
        self._export_cancel_event: Optional[threading.Event] = None
        self._close_after_export = False
        self._last_export_result: Optional[BatchResult] = None
        self._export_details_dialog: Optional[QDialog] = None
        self._export_details_text: Optional[QPlainTextEdit] = None
        self._exporter = export_unique_pdf

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.title_bar = TitleBar(self)
        root.addWidget(self.title_bar)

        content = QWidget()
        body = QHBoxLayout(content)
        body.setContentsMargins(10, 10, 10, 10)
        body.setSpacing(10)
        body.addWidget(self._build_left_panel())

        # 右侧卡片区（流式布局，排满自动换行；品牌背景）
        self.card_scroll = QScrollArea()
        self.card_scroll.setWidgetResizable(True)
        self.card_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:0;}")
        self._export_edit_controls.append(self.card_scroll)
        self.cards_body = CardsBody(lambda: not self.files)
        self.flow = FlowLayout(self.cards_body, margin=4)
        self.card_scroll.setWidget(self.cards_body)
        body.addWidget(self.card_scroll, 1)
        root.addWidget(content, 1)
        self.setCentralWidget(central)
        self.setStyleSheet(GLOBAL_QSS)
        self.setMinimumSize(860, 600)
        self.setAcceptDrops(True)
        # 卡片缩略图刷新防抖：滑杆/拖动等高频回调只触发一次重绘
        self._card_refresh_timer = QTimer(self)
        self._card_refresh_timer.setSingleShot(True)
        self._card_refresh_timer.setInterval(250)
        self._card_refresh_timer.timeout.connect(self._do_refresh_cards)
        self._rebuild_cards()

    def paintEvent(self, event) -> None:
        """首页品牌背景：Wild Field 水印 + powered by。"""
        super().paintEvent(event)
        painter = QPainter(self)
        paint_brand_background(painter, self.width(), self.height(),
                               content_left=350)
        painter.end()

    # ---- 拖放导入：PDF / 图片直接拖进窗口 ----
    def dragEnterEvent(self, event) -> None:
        if self._export_active:
            event.ignore()
            return
        if any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if self._export_active:
            event.ignore()
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.isLocalFile()]
        if not paths:
            return
        pdfs = [p for p in paths if p.lower().endswith(".pdf")]
        imgs = [p for p in paths if p.lower().endswith(
            (".png", ".jpg", ".jpeg", ".bmp", ".webp"))]
        self._import_paths(pdfs, imgs)
        event.acceptProposedAction()

    def _import_paths(self, pdfs: List[str], imgs: List[str]) -> None:
        if self._export_active:
            return
        added_pdf = added_img = 0
        if pdfs:
            existing = {f.path for f in self.files if f.path}
            for p in pdfs:
                norm = os.path.normpath(p)
                if norm in existing:
                    continue
                try:
                    n = list_pdf_pages(norm)
                except Exception:
                    self.status_label.setText(f"无法读取：{Path(norm).name}")
                    continue
                self.files.append(MockPdf(name=Path(norm).name,
                                          pages=make_pages(n), path=norm))
                existing.add(norm)
                added_pdf += 1
        if imgs:
            existing = {_path_identity(p) for p in self._images}
            for p in imgs:
                norm = os.path.normpath(p)
                identity = _path_identity(norm)
                if identity in existing:
                    continue
                self._images.append(norm)
                existing.add(identity)
                self.img_list.addItem(Path(norm).name)
                self.img_list.item(self.img_list.count() - 1).setData(
                    Qt.UserRole, norm)
                added_img += 1
        if added_pdf or added_img:
            if added_pdf:
                self._rebuild_cards()
            else:
                self.refresh_cards()

    # ---- 帮助 / 反馈 ----
    def _dialog_head(self, dlg: QDialog, title: str, subtitle: str) -> QVBoxLayout:
        """弹窗统一头部：品牌图标 + 标题 + 副标题，返回内容区布局。"""
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(8)
        head = QHBoxLayout()
        icon_lbl = QLabel()
        if Brand.icon() is not None:
            icon_lbl.setPixmap(Brand.icon().scaled(
                42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        head.addWidget(icon_lbl)
        head.addSpacing(12)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet("font-size:15px;font-weight:600;color:#ffffff;")
        s = QLabel(subtitle)
        s.setStyleSheet("font-size:11px;color:#5c7089;")
        col.addWidget(t)
        col.addWidget(s)
        head.addLayout(col)
        head.addStretch(1)
        lay.addLayout(head)
        return lay

    def _build_help_dialog(self) -> QDialog:
        dlg = QDialog(self)
        dlg.setWindowTitle("帮助 戳个印")
        dlg.setFixedWidth(420)
        lay = self._dialog_head(dlg, "使用帮助", APP_SLOGAN)
        steps = [
            ("首页", "鼠标左键点击文档缩略图，进入文档详情。"),
            ("文档详情", "左键点击页面预览，进入该页全屏视图。"),
            ("文档详情", "在页面预览上点击鼠标右键，切换「添加本页图章 / "
                         "删除本页图章」。"),
            ("文档详情", "在页面预览上按住鼠标左键拖动，即可移动图章。"),
            ("全屏视图", "与页面预览相同：右键切换图章、按住左键拖动图章；"
                         "滚轮调节图章大小；左右悬浮按钮或方向键翻页；"
                         "右键页面外空白返回。"),
            ("文档详情", "在空白位置点击鼠标右键，返回首页。"),
        ]
        for i, (where, text) in enumerate(steps, 1):
            row = QHBoxLayout()
            row.setSpacing(10)
            no = QLabel(str(i))
            no.setFixedSize(20, 20)
            no.setAlignment(Qt.AlignCenter)
            no.setStyleSheet("background:#1d2942;border-radius:10px;"
                             "color:#8ba0b8;font-size:11px;font-weight:600;")
            row.addWidget(no, 0, Qt.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(1)
            tag = QLabel(where)
            tag.setStyleSheet("font-size:11px;color:#8ba0b8;")
            body = QLabel(text)
            body.setWordWrap(True)
            body.setStyleSheet("font-size:12px;color:#c7d2e0;")
            col.addWidget(tag)
            col.addWidget(body)
            row.addLayout(col, 1)
            lay.addLayout(row)
        btn = QPushButton("知道了")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, 0, Qt.AlignRight)
        return dlg

    def _show_help(self) -> None:
        self._build_help_dialog().exec()

    def _build_feedback_dialog(self) -> QDialog:
        dlg = QDialog(self)
        dlg.setWindowTitle("反馈")
        dlg.setFixedWidth(400)
        lay = self._dialog_head(dlg, "遇到问题或有建议？", "欢迎随时联系我们")
        body = QLabel("软件由野草智能开发，如有问题请联系：")
        body.setWordWrap(True)
        body.setStyleSheet("font-size:12px;color:#aebdd2;")
        lay.addWidget(body)
        phone = QLabel(CONTACT_PHONE)
        phone.setTextInteractionFlags(Qt.TextSelectableByMouse)
        phone.setStyleSheet("font-size:19px;font-weight:600;color:#38bdf8;"
                            "letter-spacing:1px;")
        lay.addWidget(phone)
        copied = QLabel("")
        copied.setStyleSheet("color:#38bdf8;font-size:11px;")
        row = QHBoxLayout()
        row.addWidget(copied, 1)
        btn_copy = QPushButton("复制手机号")

        def do_copy() -> None:
            QApplication.clipboard().setText(CONTACT_PHONE)
            copied.setText("已复制到剪贴板")

        btn_copy.clicked.connect(do_copy)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dlg.accept)
        row.addWidget(btn_copy)
        row.addWidget(btn_close)
        lay.addLayout(row)
        return dlg

    def _send_feedback(self) -> None:
        self._build_feedback_dialog().exec()

    # ---- 无边框窗口：原生拖动/缩放/贴边（Windows） ----
    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.btn_max.set_kind(
                TitleButton.RESTORE if self.isMaximized()
                else TitleButton.MAX)
            self.title_bar.btn_max.setToolTip(
                "还原" if self.isMaximized() else "最大化")

    def nativeEvent(self, eventType, message):
        if (sys.platform == "win32" and ctypes is not None
                and bytes(eventType) == b"windows_generic_MSG"):
            msg = _wintypes.MSG.from_address(int(message))
            if msg.message == WM_NCHITTEST:
                x = ctypes.c_short(msg.lParam & 0xFFFF).value
                y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                dpr = self.devicePixelRatioF() or 1.0
                gp = QPointF(x / dpr, y / dpr).toPoint()
                code = self._hit_test(self.mapFromGlobal(gp))
                if code is not None:
                    return True, code
        return super().nativeEvent(eventType, message)

    def _hit_test(self, pos) -> Optional[int]:
        m = 6
        w, h = self.width(), self.height()
        if not self.isMaximized():
            l, r = pos.x() <= m, pos.x() >= w - m - 1
            t, b = pos.y() <= m, pos.y() >= h - m - 1
            if t and l:
                return HTTOPLEFT
            if t and r:
                return HTTOPRIGHT
            if b and l:
                return HTBOTTOMLEFT
            if b and r:
                return HTBOTTOMRIGHT
            if l:
                return HTLEFT
            if r:
                return HTRIGHT
            if t:
                return HTTOP
            if b:
                return HTBOTTOM
        if self.title_bar.geometry().contains(pos):
            # 标题栏空白区返回 HTCAPTION 交给系统拖动/双击最大化；
            # 标题栏内的按钮必须留在 HTCLIENT，否则物理点击会被系统
            # 当成拖动标题栏吞掉，Qt 收不到 press，按钮点了没反应。
            if not isinstance(self.childAt(pos), QAbstractButton):
                return HTCAPTION
        return None

    # ---- 左侧面板（保留第一版内容） ----
    def _build_left_panel(self) -> QWidget:
        wrap = QWidget()
        wrap.setFixedWidth(330)
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # 竖向空间不足时整个面板可滚动，避免分组框被挤压导致文字重叠
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:0;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}")
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 4, 4, 4)
        lay.setSpacing(8)

        # PDF 文档
        pdf_box = QGroupBox("PDF 文档")
        self.pdf_box = pdf_box
        pb = QVBoxLayout(pdf_box)
        self.pdf_list = QListWidget()
        pb.addWidget(self.pdf_list)
        row1 = QHBoxLayout()
        add_pdf = QPushButton("添加 PDF…")
        add_pdf.clicked.connect(self._add_pdfs)
        remove_pdf = QPushButton("移除")
        remove_pdf.clicked.connect(self._remove_pdf_mock)
        clear_pdf = QPushButton("清空")
        clear_pdf.clicked.connect(self._clear_pdf_mock)
        for b in (add_pdf, remove_pdf, clear_pdf):
            row1.addWidget(b)
        pb.addLayout(row1)
        lay.addWidget(pdf_box)

        # 水印图片
        img_box = QGroupBox("水印图片（可选多张）")
        self.img_box = img_box
        ib = QVBoxLayout(img_box)
        self.img_list = QListWidget()
        for p in self._images:
            self.img_list.addItem(Path(p).name)
            self.img_list.item(self.img_list.count() - 1).setData(
                Qt.UserRole, p)
        ib.addWidget(self.img_list)
        row2 = QHBoxLayout()
        add_img = QPushButton("添加图片…")
        add_img.clicked.connect(self._add_images)
        remove_img = QPushButton("移除选中")
        remove_img.clicked.connect(self._remove_img_mock)
        row2.addWidget(add_img)
        row2.addWidget(remove_img)
        ib.addLayout(row2)
        lay.addWidget(img_box)

        # 图片摆放布局
        lay_box = QGroupBox("图片摆放布局")
        self.layout_box = lay_box
        lg = QGridLayout(lay_box)
        lg.addWidget(QLabel("对齐方式"), 0, 0)
        self.anchor_combo = QComboBox()
        self.anchor_combo.addItem("保留当前位置", ANCHOR_KEEP)
        for label, anchor in (
            ("居中", Anchor.CENTER),
            ("顶部居中", Anchor.TOP_CENTER),
            ("底部居中", Anchor.BOTTOM_CENTER),
            ("左上", Anchor.TOP_LEFT),
            ("右上", Anchor.TOP_RIGHT),
            ("左下", Anchor.BOTTOM_LEFT),
            ("右下", Anchor.BOTTOM_RIGHT),
        ):
            self.anchor_combo.addItem(label, anchor.value)
        lg.addWidget(self.anchor_combo, 0, 1, 1, 2)
        lg.addWidget(QLabel("尺寸模式"), 1, 0)
        self.size_mode_combo = QComboBox()
        self.size_mode_combo.addItem("页面宽度百分比", MODE_PERCENT)
        self.size_mode_combo.addItem("原始尺寸", MODE_ORIGINAL)
        self.size_mode_combo.addItem("固定宽度 (pt)", MODE_FIXED)
        self.size_mode_combo.currentIndexChanged.connect(
            self._home_size_mode_changed)
        lg.addWidget(self.size_mode_combo, 1, 1, 1, 2)
        self.percent_label = QLabel("页面宽度占比")
        lg.addWidget(self.percent_label, 2, 0)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 200)
        self.size_spin.setValue(30)
        self.size_spin.setSuffix(" %")
        lg.addWidget(self.size_spin, 2, 1, 1, 2)
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(1, 200)
        self.size_slider.setValue(30)
        self.size_spin.valueChanged.connect(self.size_slider.setValue)
        self.size_slider.valueChanged.connect(self.size_spin.setValue)
        lg.addWidget(self.size_slider, 3, 0, 1, 3)
        self.fixed_width_label = QLabel("固定宽度")
        lg.addWidget(self.fixed_width_label, 4, 0)
        self.fixed_width_spin = QDoubleSpinBox()
        self.fixed_width_spin.setRange(1.0, 2000.0)
        self.fixed_width_spin.setDecimals(1)
        self.fixed_width_spin.setValue(180.0)
        self.fixed_width_spin.setSuffix(" pt")
        lg.addWidget(self.fixed_width_spin, 4, 1, 1, 2)
        lg.addWidget(QLabel("预览透明度"), 5, 0)
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(1, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.valueChanged.connect(
            lambda value: self.opacity_value.setText(f"{value}%（仅预览）"))
        lg.addWidget(self.opacity_slider, 5, 1, 1, 2)
        self.opacity_value = QLabel("100%（仅预览）")
        lg.addWidget(self.opacity_value, 6, 1, 1, 2)
        self.layout_scope_label = QLabel(
            "作用范围：全部文档页面。调整参数后点击应用。")
        self.layout_scope_label.setWordWrap(True)
        lg.addWidget(self.layout_scope_label, 7, 0, 1, 3)
        self.anchor_hint_label = QLabel(
            "选择对齐方式会重设每页每张图片的位置。")
        self.anchor_hint_label.setWordWrap(True)
        lg.addWidget(self.anchor_hint_label, 8, 0, 1, 3)
        self.apply_layout_button = QPushButton("应用到全部页面")
        self.apply_layout_button.clicked.connect(self._apply_layout_to_all_pages)
        lg.addWidget(self.apply_layout_button, 9, 0, 1, 3)
        self._home_size_mode_changed()
        lay.addWidget(lay_box)

        # 导出
        exp_box = QGroupBox("导出")
        eb = QVBoxLayout(exp_box)
        dir_row = QVBoxLayout()
        self.out_dir_label = QLabel("未选择输出目录")
        self.out_dir_label.setWordWrap(True)
        self.out_dir_label.setToolTip("双击这里可以手动填写输出目录")
        self.out_dir_label.installEventFilter(self)
        dir_row.addWidget(self.out_dir_label)
        dir_buttons = QHBoxLayout()
        btn_dir = QPushButton("选择目录")
        self.btn_dir = btn_dir
        btn_dir.setToolTip("打开目录选择框")
        btn_dir.clicked.connect(self._pick_dir)
        dir_buttons.addWidget(btn_dir, 1)
        self.btn_manual_dir = QPushButton("手动输入")
        self.btn_manual_dir.setToolTip("选择框不可用时，直接填写输出目录路径")
        self.btn_manual_dir.clicked.connect(self._type_dir)
        dir_buttons.addWidget(self.btn_manual_dir)
        dir_row.addLayout(dir_buttons)
        eb.addLayout(dir_row)
        self.btn_export = QPushButton("导出全部")
        self.btn_export.setObjectName("btn_primary")
        self.btn_export.setCursor(Qt.PointingHandCursor)
        self.btn_export.clicked.connect(self._on_export_button_clicked)
        eb.addWidget(self.btn_export)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        eb.addWidget(self.progress)
        self.status_label = QLabel("就绪")
        self.status_label.setWordWrap(True)
        eb.addWidget(self.status_label)
        self.btn_open_dir = QPushButton("打开输出文件夹")
        self.btn_open_dir.setToolTip("在系统文件管理器中打开输出目录")
        self.btn_open_dir.setEnabled(False)
        self.btn_open_dir.clicked.connect(self._open_output_dir)
        eb.addWidget(self.btn_open_dir)
        self.btn_export_details = QPushButton("导出详情")
        self.btn_export_details.setObjectName("btn_export_details")
        self.btn_export_details.setEnabled(False)
        self.btn_export_details.clicked.connect(self._show_export_details)
        eb.addWidget(self.btn_export_details)
        lay.addWidget(exp_box)

        self._export_edit_controls = [
            self.pdf_box,
            self.img_box,
            self.layout_box,
            self.btn_dir,
        ]

        # 列表等关键控件给出最小高度，空间不足走滚动而不是挤压
        self.pdf_list.setMinimumHeight(110)
        self.img_list.setMinimumHeight(90)
        lay.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return wrap

    def _home_size_mode_changed(self, *_args) -> None:
        mode = self.size_mode_combo.currentData()
        percent = mode == MODE_PERCENT
        fixed = mode == MODE_FIXED
        self.percent_label.setVisible(percent)
        self.size_spin.setVisible(percent)
        self.size_slider.setVisible(percent)
        self.fixed_width_label.setVisible(fixed)
        self.fixed_width_spin.setVisible(fixed)

    @staticmethod
    def _display_page_sizes(pdf: MockPdf) -> list[tuple[float, float]]:
        """Return page sizes in the displayed orientation, in PDF points."""
        if pdf.path and os.path.exists(pdf.path):
            try:
                import fitz
                with fitz.open(pdf.path) as doc:
                    return [(float(page.rect.width), float(page.rect.height))
                            for page in doc]
            except Exception:
                pass
        return [(float(PAGE_W), float(PAGE_H)) for _ in pdf.pages]

    def _apply_layout_to_all_pages(self) -> None:
        """Apply pending home controls to every document page explicitly."""
        if self._export_active:
            return
        mode = self.size_mode_combo.currentData()
        anchor_value = self.anchor_combo.currentData()
        opacity = self.opacity_slider.value() / 100.0
        for pdf in self.files:
            page_sizes = self._display_page_sizes(pdf)
            for page_index, st in enumerate(pdf.pages):
                st.size_mode = mode
                st.percent = float(self.size_spin.value())
                st.fixed_width = self.fixed_width_spin.value()
                st.opacity = opacity
                if anchor_value == ANCHOR_KEEP:
                    continue
                page_w, page_h = page_sizes[page_index]
                offsets: dict[int, tuple[float, float]] = {}
                for image_index, image_path in enumerate(self._images):
                    try:
                        iw, ih = image_native_size(Path(image_path))
                        centered = replace(
                            st, dx=0.0, dy=0.0, image_offsets={})
                        layout = replace(
                            page_layout(centered, image_path=image_path,
                                        native_size=(iw, ih)),
                            anchor=Anchor(anchor_value),
                        )
                        x, y, width, height = rect_for(
                            layout, page_w, page_h, iw, ih)
                        offsets[image_index] = (
                            x + width / 2.0 - page_w / 2.0,
                            y + height / 2.0 - page_h / 2.0,
                        )
                    except Exception:
                        continue
                st.image_offsets = offsets
        self.status_label.setText("已将布局应用到全部文档页面")
        self.refresh_cards()

    # ---- 卡片区 ----
    def _rebuild_cards(self) -> None:
        while self.flow.count():
            item = self.flow.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.cards = []
        for idx, pdf in enumerate(self.files):
            card = PdfCard(pdf, idx,
                           on_zoom=self.open_grid,
                           on_change=self.refresh_cards)
            card.refresh(self._images)
            self.cards.append(card)
            self.flow.addWidget(card)
        self.pdf_list.clear()
        for pdf in self.files:
            self.pdf_list.addItem(f"{pdf.name}  ·  {len(pdf.pages)} 页")
        self.cards_body.update()

    def refresh_cards(self, *_args) -> None:
        """防抖入口：高频回调（滑杆、拖动结束）只在静默后刷新一次缩略图。"""
        self._card_refresh_timer.start()

    def _do_refresh_cards(self) -> None:
        for card in self.cards:
            card.refresh(self._images)
        self.cards_body.update()

    def open_grid(self, idx: int) -> None:
        if self._export_active:
            return
        if 0 <= idx < len(self.files):
            sel = self.img_list.currentRow() if self.img_list.count() else 0
            PageGridDialog(self.files[idx], self.refresh_cards,
                           image_paths=self._images,
                           selected_image_index=max(0, sel),
                           parent=self).exec()

    # ---- 左侧面板动作 ----
    def _add_pdfs(self) -> None:
        if self._export_active:
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择 PDF 文件", "", "PDF 文件 (*.pdf)")
        if not files:
            return
        existing = {f.path for f in self.files if f.path}
        for f in files:
            norm = os.path.normpath(f)
            if norm in existing:
                continue
            try:
                n = list_pdf_pages(norm)
            except Exception:
                self.status_label.setText(f"无法读取：{Path(f).name}")
                continue
            self.files.append(MockPdf(name=Path(norm).name,
                                      pages=make_pages(n), path=norm))
            existing.add(norm)
        self._rebuild_cards()

    def _remove_pdf_mock(self) -> None:
        if self._export_active:
            return
        rows = {i.row() for i in self.pdf_list.selectedIndexes()}
        if not rows:
            return
        for row in sorted(rows, reverse=True):
            if 0 <= row < len(self.files):
                self.files.pop(row)
        self._rebuild_cards()

    def _clear_pdf_mock(self) -> None:
        if self._export_active:
            return
        self.files.clear()
        self._rebuild_cards()

    def _add_images(self) -> None:
        if self._export_active:
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择水印图片", "",
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not files:
            return
        existing = {_path_identity(p) for p in self._images}
        for f in files:
            norm = os.path.normpath(f)
            identity = _path_identity(norm)
            if identity in existing:
                continue
            self._images.append(norm)
            existing.add(identity)
            self.img_list.addItem(Path(norm).name)
            self.img_list.item(self.img_list.count() - 1).setData(
                Qt.UserRole, norm)
        self.refresh_cards()

    def _remove_img_mock(self) -> None:
        if self._export_active:
            return
        removed_rows = {index.row() for index in self.img_list.selectedIndexes()}
        if not removed_rows:
            return

        old_to_new = {
            old_index: new_index
            for new_index, old_index in enumerate(
                index for index in range(len(self._images))
                if index not in removed_rows
            )
        }
        for index in sorted(removed_rows, reverse=True):
            self.img_list.takeItem(index)
        self._images = [
            image_path
            for old_index, image_path in enumerate(self._images)
            if old_index in old_to_new
        ]
        for pdf in self.files:
            for page in pdf.pages:
                page.image_offsets = {
                    old_to_new[old_index]: offset
                    for old_index, offset in page.image_offsets.items()
                    if old_index in old_to_new
                }
        self.refresh_cards()

    def _default_output_dir(self) -> str:
        """给选择框一个确定存在的起始目录，避免落到 Qt 记住的陈旧位置。"""
        candidates = (
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DocumentsLocation),
            str(Path.home()),
            os.getcwd(),
        )
        for candidate in candidates:
            if candidate and os.path.isdir(candidate):
                return candidate
        return ""

    def _ask_directory(self, title: str) -> tuple[str, bool]:
        """选目录，返回 (目录, 是否「点了确定却拿不到路径」)。

        静态方法 QFileDialog.getExistingDirectory() 把「取消」和「确定但取不到
        路径」都变成空串：在 Windows 上选中「此电脑」「网络」、库或手机这类没有
        文件系统路径的虚拟位置时，原生对话框会确定后返回空。这里用实例对话框
        区分两者，遇到后者自动换 Qt 内建对话框再选一次，并在本次运行内记住
        原生对话框不可用，免得每次都要点两遍。
        """
        start = self._output_dir if self._output_dir_ok() else \
            self._default_output_dir()
        attempts = (False,) if self._native_dir_dialog_broken else (True, False)
        for native in attempts:
            dialog = QFileDialog(self, title)
            dialog.setFileMode(QFileDialog.FileMode.Directory)
            dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, not native)
            if start:
                dialog.setDirectory(start)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            selected = dialog.selectedFiles()
            dialog.deleteLater()
            if accepted and selected and os.path.isdir(selected[0]):
                return os.path.normpath(selected[0]), False
            if not accepted:
                return "", False          # 用户取消，不再弹第二个
            if native:
                self._native_dir_dialog_broken = True
        return "", True

    def _open_output_dir(self) -> None:
        """用系统文件管理器打开当前输出目录。"""
        if not self._output_dir_ok():
            self.status_label.setText("请先选择有效的输出目录")
            return
        try:
            os.startfile(self._output_dir)
        except OSError as exc:
            self.status_label.setText(f"无法打开输出目录：{exc}")

    def _pick_dir(self) -> None:
        if self._export_active:
            return
        d, empty_result = self._ask_directory("选择输出目录")
        if d:
            self._output_dir = d
            self.out_dir_label.setText(d)
            self.status_label.setText(f"输出目录：{d}")
        elif empty_result:
            self.status_label.setText(
                "没有取到目录路径（「此电脑」「网络」等位置没有磁盘路径），"
                "请重新选择磁盘上的文件夹，或点「手动输入」直接填写")
        self.btn_open_dir.setEnabled(self._output_dir_ok())

    def _apply_output_dir(self, raw: str) -> bool:
        """写入输出目录；非法路径给出提示而不改动已有选择。"""
        d = os.path.normpath(raw.strip().strip('"'))
        if not os.path.isdir(d):
            self.status_label.setText(f"目录不存在：{d}")
            self.btn_open_dir.setEnabled(self._output_dir_ok())
            return False
        self._output_dir = d
        self.out_dir_label.setText(d)
        self.status_label.setText(f"输出目录：{d}")
        self.btn_open_dir.setEnabled(True)
        return True

    def _type_dir(self) -> None:
        """手动填写输出目录：选择框在部分机器上不可用（或选不中磁盘路径）时的兜底。"""
        if self._export_active:
            return
        current = self._output_dir if self._output_dir_ok() else \
            self._default_output_dir()
        text, ok = QInputDialog.getText(
            self, "输入输出目录", "目录路径（可直接粘贴，如 D:\\导出）：",
            QLineEdit.Normal, current)
        if ok:
            self._apply_output_dir(text)

    def eventFilter(self, obj, event) -> bool:
        if (obj is self.out_dir_label
                and event.type() == QEvent.Type.MouseButtonDblClick):
            self._type_dir()
            return True
        return super().eventFilter(obj, event)

    def _on_export_button_clicked(self) -> None:
        if self._export_active:
            self._cancel_export()
        else:
            self._export_real()

    def _build_export_jobs(self) -> tuple[ExportJob, ...]:
        """Freeze all model input before any work enters the background thread."""
        files_snapshot = copy.deepcopy([f for f in self.files if f.path])
        images_snapshot = copy.deepcopy(list(self._images))
        jobs: list[ExportJob] = []

        for pdf in files_snapshot:
            page_image_layouts = {}
            preparation_error = ""
            try:
                for page_index, state in enumerate(pdf.pages):
                    if not state.wm:
                        continue
                    per_image = {}
                    for image_index, image_path in enumerate(images_snapshot):
                        ox, oy = state.image_offsets.get(
                            image_index, (state.dx, state.dy)
                        )
                        local = replace(
                            state,
                            wm=True,
                            dx=ox,
                            dy=oy,
                            opacity=1.0,
                            image_offsets={},
                        )
                        per_image[image_index] = page_layout(
                            local, image_path=image_path
                        )
                    page_image_layouts[page_index] = per_image
            except Exception as exc:
                page_image_layouts = {}
                preparation_error = str(exc) or type(exc).__name__

            jobs.append(ExportJob(
                name=pdf.name,
                path=pdf.path,
                images=tuple(images_snapshot),
                page_image_layouts=page_image_layouts,
                page_count=len(pdf.pages),
                preparation_error=preparation_error,
            ))
        return tuple(jobs)

    def _export_real(self) -> None:
        """Start a snapshot-based batch export on a dedicated Qt worker thread."""
        if self._export_active:
            return
        if not any(pdf.path for pdf in self.files):
            self.status_label.setText(
                "没有可导出的真实 PDF（请先用“添加 PDF…”选择文件）"
            )
            return
        if not self._images:
            self.status_label.setText("请先添加至少一张水印图片")
            return
        if not self._output_dir_ok():
            self.status_label.setText("请先选择输出目录")
            return

        jobs = self._build_export_jobs()
        self._clear_export_result_ui()
        self._export_cancel_event = threading.Event()
        self._export_thread = QThread(self)
        self._export_worker = ExportWorker(
            jobs,
            str(self._output_dir),
            self._export_cancel_event,
            exporter=self._exporter,
        )
        self._export_worker.moveToThread(self._export_thread)
        self._export_thread.started.connect(self._export_worker.run)
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.finished.connect(self._on_export_worker_finished)
        self._export_worker.finished.connect(self._export_thread.quit)
        self._export_worker.finished.connect(self._export_worker.deleteLater)
        self._export_thread.finished.connect(self._on_export_thread_finished)
        self._export_thread.finished.connect(self._export_thread.deleteLater)

        total_pages = sum(max(1, job.page_count) for job in jobs)
        self.progress.setRange(0, total_pages)
        self.progress.setValue(0)
        self.status_label.setText(f"正在导出：0/{len(jobs)}")
        self._export_active = True
        self._set_export_ui_active(True)
        try:
            self._export_thread.start()
        except Exception as exc:
            result = BatchResult(
                total=len(jobs),
                successes=(),
                failures=(ExportFailure(
                    "导出任务", str(exc) or type(exc).__name__
                ),),
                cancelled=False,
            )
            self._on_export_worker_finished(result)
            if not self._export_thread.isRunning():
                self._export_active = False
                self._set_export_ui_active(False)
                delete_qt_object(self._export_worker)
                self._export_thread.deleteLater()
                self._export_worker = None
                self._export_thread = None
                self._export_cancel_event = None

    def _set_export_ui_active(self, active: bool) -> None:
        for control in self._export_edit_controls:
            control.setEnabled(not active)
        self.setAcceptDrops(not active)
        self.btn_export.setEnabled(True)
        self.btn_export.setText("取消导出" if active else "导出全部")
        self.btn_export_details.setEnabled(
            not active and self._last_export_result is not None
        )

    def _cancel_export(self) -> None:
        if not self._export_active or self._export_cancel_event is None:
            return
        self._export_cancel_event.set()
        self.btn_export.setEnabled(False)
        self.btn_export.setText("正在取消…")
        self.status_label.setText("正在取消：当前文件完成后停止")

    def _on_export_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(min(done, max(1, total)))
        if not (self._export_cancel_event and self._export_cancel_event.is_set()):
            self.status_label.setText(f"正在导出：{name}")

    def _on_export_worker_finished(self, result: BatchResult) -> None:
        self._last_export_result = result
        successes = len(result.successes)
        failures = len(result.failures)
        unprocessed = max(0, result.total - successes - failures)
        if result.cancelled:
            self.status_label.setText(
                f"已取消：成功 {successes}/{result.total}，失败 {failures}，"
                f"未处理 {unprocessed}"
            )
        elif failures == 0 and successes == result.total:
            self.status_label.setText(
                f"导出完成：全部成功 {successes}/{result.total}，未处理 {unprocessed}"
            )
        elif successes == 0 and failures > 0:
            self.status_label.setText(
                f"导出失败：成功 0/{result.total}，失败 {failures}，"
                f"未处理 {unprocessed}"
            )
        else:
            self.status_label.setText(
                f"导出完成：成功 {successes}/{result.total}，失败 {failures}，"
                f"未处理 {unprocessed}"
            )
        self.btn_export_details.setEnabled(not self._export_active)

    def _clear_export_result_ui(self) -> None:
        self._last_export_result = None
        self.btn_export_details.setEnabled(False)
        dialog = self._export_details_dialog
        self._export_details_dialog = None
        self._export_details_text = None
        if dialog is not None:
            dialog.close()

    @staticmethod
    def _format_export_details(result: BatchResult) -> str:
        successes = len(result.successes)
        failures = len(result.failures)
        unprocessed = max(0, result.total - successes - failures)
        if result.cancelled:
            state = "已取消"
        elif failures == 0 and successes == result.total:
            state = "全部成功"
        elif successes == 0 and failures > 0:
            state = "全部失败"
        else:
            state = "部分失败"

        lines = [
            f"状态：{state}",
            f"总计：{result.total}",
            f"成功：{successes}",
            f"失败：{failures}",
            f"未处理：{unprocessed}",
        ]
        if result.successes:
            lines.extend(("", "成功文件："))
            for success in result.successes:
                lines.extend((success.name, f"  {success.output_path}"))
        if result.failures:
            lines.extend(("", "失败文件："))
            for failure in result.failures:
                lines.extend((failure.name, f"  {failure.error_text}"))
        return "\n".join(lines)

    def _on_export_details_destroyed(self, dialog: QDialog) -> None:
        if self._export_details_dialog is dialog:
            self._export_details_dialog = None
            self._export_details_text = None

    def _show_export_details(self) -> None:
        result = self._last_export_result
        if result is None or self._export_active:
            return
        if self._export_details_dialog is not None:
            self._export_details_dialog.show()
            self._export_details_dialog.raise_()
            self._export_details_dialog.activateWindow()
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("导出详情")
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        dialog.resize(660, 420)
        dialog.setMinimumSize(480, 300)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 12)
        details = QPlainTextEdit(dialog)
        details.setObjectName("export_details_text")
        details.setReadOnly(True)
        details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        details.setPlainText(self._format_export_details(result))
        layout.addWidget(details, 1)
        close_button = QPushButton("关闭", dialog)
        close_button.clicked.connect(dialog.close)
        layout.addWidget(close_button, 0, Qt.AlignRight)

        self._export_details_dialog = dialog
        self._export_details_text = details
        dialog.destroyed.connect(
            lambda *_args, current=dialog:
            self._on_export_details_destroyed(current)
        )
        dialog.show()

    def _on_export_thread_finished(self) -> None:
        self._export_active = False
        self._set_export_ui_active(False)
        self._export_worker = None
        self._export_thread = None
        self._export_cancel_event = None
        if self._close_after_export:
            self._close_after_export = False
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event) -> None:
        if self._export_active:
            self._close_after_export = True
            self._cancel_export()
            event.ignore()
            return
        super().closeEvent(event)

    def _output_dir_ok(self) -> bool:
        d = getattr(self, "_output_dir", None)
        return bool(d and os.path.isdir(d))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DemoWindow()
    window.show()
    sys.exit(app.exec())
