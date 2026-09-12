#!/usr/bin/env python3
"""纯前端 PDF 水印交互 Demo（无后端，模拟数据）。

交互约定：
1. 只有鼠标右键切换该页 添加/不添加水印；左键不影响水印有无。
2. 左侧保留第一版面板：PDF 列表、图片列表、布局控件、导出控件。
3. 左键拖动只移动「被拖那一页」的水印位置（文件内其他页、其他文件均不动）。
4. 弹出窗口：所有页面按 3 列连续平铺，页面多于 9 页时靠右侧垂直滚动条查看；
   水印大小含「页面宽度百分比」与「原始尺寸」两种模式，放大/缩小只影响当前页。
5. 首页卡片排满自动换行（FlowLayout），不再无限横向延伸。
6. 点击页面只改变选中高亮，不改变页面预览图尺寸（无跳变）。

坐标约定（所见即所得的关键）：
- PageState.dx/dy 与 image_offsets 一律以 PDF 点（pt，72dpi）为单位，
  相对页面中心（锚点居中）。
- 预览位图 = pt × PREVIEW_ZOOM；合成预览时布局经 page_layout(zoom) 换算成
  位图像素；导出时 zoom=1.0 直接用 pt（reportlab 坐标系）。
- 拖动量以「控件像素 → pt」换算后再写入 PageState，保证三处单位一致。

运行： python pdf_watermark_demo.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as _wintypes

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QIcon, QImage, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLayout, QListWidget,
    QMainWindow, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from pdf_watermark_studio.core.engine import (
    Layout, SizeMode, rect_for,
    render_page_bg, composite_watermark, export_pdf, list_pdf_pages,
    image_native_size,
)

PAGE_W, PAGE_H = 420, 297
CARD_W, CARD_H = 340, int(340 * 297 / 420)
WATERMARK_COLOR = QColor(231, 76, 60, 200)
ORIG_W, ORIG_H = 300, 186          # 模拟水印图片「原始尺寸」（页面坐标）
MODE_PERCENT = "percent"
MODE_ORIGINAL = "original"
MODE_CM = "cm"
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
    cm_w: float = 5.0             # CM：宽（cm）
    cm_h: float = 5.0             # CM：高（cm）
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
    if st.size_mode == MODE_ORIGINAL:
        if native_size is None and image_path is not None:
            native_size = image_native_size(Path(image_path))
        if native_size is None:
            return Layout(size_mode=SizeMode.DEFAULT, dx=dx, dy=dy)
        iw, ih = native_size
        return Layout(size_mode=SizeMode.FIXED,
                      fixed_width=max(1, round(iw * zoom)),
                      fixed_height=max(1.0, ih * zoom),
                      dx=dx, dy=dy)
    if st.size_mode == MODE_CM:
        return Layout(size_mode=SizeMode.FIXED,
                      fixed_width=max(1, round(st.cm_w * CM_TO_PT * zoom)),
                      fixed_height=max(1, round(st.cm_h * CM_TO_PT * zoom)),
                      dx=dx, dy=dy)
    return Layout(size_mode=SizeMode.PERCENT, percent=st.percent, dx=dx, dy=dy)


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
        local = PageState(wm=True, dx=dx, dy=dy,
                          size_mode=st.size_mode, percent=st.percent,
                          cm_w=st.cm_w, cm_h=st.cm_h)
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

    交互：右键切换水印；左键拖动移动当前选中图片在本页的位置；
    左键按下选中当前页。
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

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            self.overlay.set_current(self.page)
            self.overlay.toggle_page(self.page)
            return
        if event.button() == Qt.LeftButton:
            self.overlay.set_current(self.page)
            self.press = QPointF(event.position())
            self.dragging = False

    def mouseMoveEvent(self, event) -> None:
        if self.press is None:
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

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.press is not None:
            if self.dragging:
                self.overlay.on_change()
            self.press = None
            self.dragging = False


# ---------------------------------------------------------------- 全屏弹窗
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
        self.mode_combo.addItem("长宽尺寸 (cm)", MODE_CM)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        left.addWidget(self.mode_combo)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(1, 200)
        self.slider.valueChanged.connect(self._on_percent_changed)
        left.addWidget(self.slider)
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
        title = QLabel(self.pdf.name)
        title.setStyleSheet("color:#e8eef7;font-size:13px;font-weight:600;"
                            "background:transparent;")
        lay.addWidget(title)
        sub = QLabel(f"·  {len(self.pdf.pages)} 页")
        sub.setStyleSheet("color:#5c7089;font-size:11px;background:transparent;")
        lay.addWidget(sub)
        lay.addStretch(1)
        close_btn = TitleButton(TitleButton.CLOSE, "关闭")
        close_btn.clicked.connect(self.close)
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
        local = PageState(wm=True, dx=dx, dy=dy,
                          size_mode=st.size_mode, percent=st.percent,
                          cm_w=st.cm_w, cm_h=st.cm_h)
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
        idx = {MODE_PERCENT: 0, MODE_ORIGINAL: 1, MODE_CM: 2}.get(mode, 0)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(idx)
        self.mode_combo.blockSignals(False)
        self.slider.blockSignals(True)
        self.slider.setValue(int(st.percent))
        self.slider.blockSignals(False)
        self.slider.setEnabled(mode == MODE_PERCENT)
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

    # ---- 页面水印操作 ----
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
QTextEdit {
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
    """自绘深色标题栏（高约56px）：品牌图标+标题 + 关于/反馈 + 窗口按钮。

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

        btn_about = ghost("关于", "版本信息")
        btn_about.clicked.connect(window._show_about)
        btn_feedback = ghost("反馈", "问题与建议")
        btn_feedback.clicked.connect(window._send_feedback)
        lay.addWidget(btn_about)
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
        self.cards: List[PdfCard] = []

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
        if any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
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

    # ---- 关于 / 反馈 ----
    def _show_about(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("关于 戳个印")
        dlg.setFixedWidth(360)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(6)
        head = QHBoxLayout()
        icon_lbl = QLabel()
        if Brand.icon() is not None:
            icon_lbl.setPixmap(Brand.icon().scaled(
                52, 52, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        head.addWidget(icon_lbl)
        title_col = QVBoxLayout()
        t = QLabel("戳个印")
        t.setStyleSheet("font-size:17px;font-weight:600;color:#ffffff;")
        s = QLabel(APP_SLOGAN)
        s.setStyleSheet("font-size:11px;color:#5c7089;")
        title_col.addSpacing(2)
        title_col.addWidget(t)
        title_col.addWidget(s)
        head.addSpacing(14)
        head.addLayout(title_col)
        head.addStretch(1)
        lay.addLayout(head)
        desc = QLabel("桌面版 PDF 图片水印工具\n预览与导出所见即所得，支持多文档、"
                      "多水印图批量盖章。\n\n由野草智能（Wild Field）出品"
                      f"\n{APP_SLOGAN}，向上生长，长出不凡")
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size:12px;color:#aebdd2;line-height:150%;")
        lay.addWidget(desc)
        lay.addSpacing(8)
        btn = QPushButton("知道了")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, 0, Qt.AlignRight)
        dlg.exec()

    def _send_feedback(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("反馈")
        dlg.setFixedWidth(360)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        icon_lbl = QLabel()
        if Brand.icon() is not None:
            icon_lbl.setPixmap(Brand.icon().scaled(
                44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        head.addWidget(icon_lbl)
        t = QLabel("遇到问题或有建议？")
        t.setStyleSheet("font-size:14px;font-weight:600;color:#ffffff;")
        head.addSpacing(10)
        head.addWidget(t)
        head.addStretch(1)
        lay.addLayout(head)
        body = QLabel("欢迎联系野草智能（Wild Field）反馈：\n"
                      "QQ：346869673")
        body.setWordWrap(True)
        body.setStyleSheet("font-size:12px;color:#aebdd2;")
        lay.addWidget(body)
        copied = QLabel("")
        copied.setStyleSheet("color:#38bdf8;font-size:11px;")
        row = QHBoxLayout()
        row.addWidget(copied, 1)
        btn_copy = QPushButton("复制 QQ 号")

        def do_copy() -> None:
            QApplication.clipboard().setText("346869673")
            copied.setText("已复制到剪贴板")

        btn_copy.clicked.connect(do_copy)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dlg.accept)
        row.addWidget(btn_copy)
        row.addWidget(btn_close)
        lay.addLayout(row)
        dlg.exec()

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
            child = self.childAt(pos)
            if child not in (self.title_bar.btn_min,
                             self.title_bar.btn_max,
                             self.title_bar.btn_close):
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
        lg = QGridLayout(lay_box)
        lg.addWidget(QLabel("对齐方式"), 0, 0)
        self.anchor_combo = QComboBox()
        self.anchor_combo.addItems(["居中", "顶部居中", "底部居中",
                                    "左上", "右上", "左下", "右下"])
        lg.addWidget(self.anchor_combo, 0, 1, 1, 2)
        lg.addWidget(QLabel("尺寸模式"), 1, 0)
        self.size_mode_combo = QComboBox()
        self.size_mode_combo.addItems(["页面宽度百分比", "原始尺寸", "固定像素宽度"])
        lg.addWidget(self.size_mode_combo, 1, 1, 1, 2)
        lg.addWidget(QLabel("页面宽度占比"), 2, 0)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 200)
        self.size_spin.setValue(30)
        self.size_spin.setSuffix(" %")
        lg.addWidget(self.size_spin, 2, 1, 1, 2)
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(1, 100)
        self.size_slider.setValue(30)
        lg.addWidget(self.size_slider, 3, 0, 1, 3)
        lg.addWidget(QLabel("预览透明度"), 4, 0)
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(1, 100)
        self.opacity_slider.setValue(100)
        lg.addWidget(self.opacity_slider, 4, 1, 1, 2)
        self.opacity_value = QLabel("100%")
        lg.addWidget(self.opacity_value, 5, 1, 1, 2)
        lay.addWidget(lay_box)

        # 导出
        exp_box = QGroupBox("导出")
        eb = QVBoxLayout(exp_box)
        dir_row = QHBoxLayout()
        self.out_dir_label = QLabel("未选择输出目录")
        dir_row.addWidget(self.out_dir_label, 1)
        btn_dir = QPushButton("选择目录")
        btn_dir.clicked.connect(self._pick_dir)
        dir_row.addWidget(btn_dir)
        eb.addLayout(dir_row)
        self.btn_export = QPushButton("导出全部")
        self.btn_export.setObjectName("btn_primary")
        self.btn_export.setCursor(Qt.PointingHandCursor)
        self.btn_export.clicked.connect(self._export_real)
        eb.addWidget(self.btn_export)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        eb.addWidget(self.progress)
        self.status_label = QLabel("就绪")
        eb.addWidget(self.status_label)
        lay.addWidget(exp_box)

        # 列表等关键控件给出最小高度，空间不足走滚动而不是挤压
        self.pdf_list.setMinimumHeight(110)
        self.img_list.setMinimumHeight(90)
        lay.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return wrap

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
        if 0 <= idx < len(self.files):
            sel = self.img_list.currentRow() if self.img_list.count() else 0
            PageGridDialog(self.files[idx], self.refresh_cards,
                           image_paths=self._images,
                           selected_image_index=max(0, sel),
                           parent=self).exec()

    # ---- 左侧面板动作 ----
    def _add_pdfs(self) -> None:
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
        rows = {i.row() for i in self.pdf_list.selectedIndexes()}
        if not rows:
            return
        for row in sorted(rows, reverse=True):
            if 0 <= row < len(self.files):
                self.files.pop(row)
        self._rebuild_cards()

    def _clear_pdf_mock(self) -> None:
        self.files.clear()
        self._rebuild_cards()

    def _add_images(self) -> None:
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
        removed = []
        for i in sorted({r.row() for r in self.img_list.selectedIndexes()},
                        reverse=True):
            item = self.img_list.takeItem(i)
            if item is not None:
                removed.append(item.data(Qt.UserRole))
        self._images = [p for p in self._images
                        if p not in {r for r in removed}]
        self.refresh_cards()

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self._output_dir = os.path.normpath(d)
            self.out_dir_label.setText(self._output_dir)

    def _export_real(self) -> None:
        """真实导出：把每页的 wm/偏移/尺寸按 page_layouts 应用到真实 PDF。"""
        real = [f for f in self.files if f.path and os.path.exists(f.path)]
        if not real:
            self.status_label.setText("没有可导出的真实 PDF（请先用“添加 PDF…”选择文件）")
            return
        if not self._images:
            self.status_label.setText("请先添加至少一张水印图片")
            return
        if not self._output_dir_ok():
            self.status_label.setText("请先选择输出目录")
            return
        total = len(real)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        ok = 0
        for i, f in enumerate(real):
            out = os.path.join(self._output_dir, "watermarked_" + f.name)
            # 每页每图独立布局：只导出 wm=True 的页；每张图使用自己的偏移
            page_image_layouts = {}
            for idx, st in enumerate(f.pages):
                if not st.wm:
                    continue
                per_img = {}
                for img_idx in range(len(self._images)):
                    if img_idx not in st.image_offsets:
                        # 未单独调整过的图片用该页默认位置
                        ox, oy = st.dx, st.dy
                    else:
                        ox, oy = st.image_offsets[img_idx]
                    local = PageState(wm=True, dx=ox, dy=oy,
                                      size_mode=st.size_mode,
                                      percent=st.percent,
                                      cm_w=st.cm_w, cm_h=st.cm_h)
                    per_img[img_idx] = page_layout(
                        local, image_path=self._images[img_idx])
                page_image_layouts[idx] = per_img
            try:
                export_pdf(f.path, out, images=[Path(p) for p in self._images],
                           page_image_layouts=page_image_layouts)
                ok += 1
            except Exception as e:
                self.status_label.setText(f"导出失败：{f.name}（{e}）")
            self.progress.setValue(i + 1)
        self.status_label.setText(f"导出完成：{ok}/{total} → {self._output_dir}")

    def _output_dir_ok(self) -> bool:
        d = getattr(self, "_output_dir", None)
        return bool(d and os.path.isdir(d))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DemoWindow()
    window.show()
    sys.exit(app.exec())
