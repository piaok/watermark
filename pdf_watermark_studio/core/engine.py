"""PDF 批量图片水印引擎。

预览渲染与正式导出共用同一套几何换算（rect_for），两者逐页逐图地
按同一个 Layout 计算放置位置，保证「所见即所得」。

坐标约定：
- Preview：页面像素坐标，左上角原点，y 向下（与 Qt/PIL 一致）。
- Export：同一矩形换算成 reportlab 自下而上的坐标再绘制。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PyPDF2 import PdfReader
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


class Anchor(Enum):
    """水印图在页面上的对齐锚点。"""
    TOP_LEFT = "top_left"
    TOP_CENTER = "top_center"
    TOP_RIGHT = "top_right"
    CENTER = "center"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_CENTER = "bottom_center"
    BOTTOM_RIGHT = "bottom_right"

    @property
    def label(self) -> str:
        return {
            "top_left": "左上",
            "top_center": "顶部居中",
            "top_right": "右上",
            "center": "正中",
            "bottom_left": "左下",
            "bottom_center": "底部居中",
            "bottom_right": "右下",
        }[self.value]


class SizeMode(Enum):
    """图片尺寸策略。"""
    DEFAULT = "default"    # 保持图片原始像素尺寸；超过页面则等比收缩
    PERCENT = "percent"    # 相对页面宽度的百分比
    FIXED = "fixed"        # 固定像素宽度，高度按图比等比

    @property
    def label(self) -> str:
        return {
            "default": "原始尺寸",
            "percent": "页面宽度百分比",
            "fixed": "固定像素宽度",
        }[self.value]


@dataclass
class Layout:
    """水印放置参数（界面与导出共用）。"""
    anchor: Anchor = Anchor.CENTER
    size_mode: SizeMode = SizeMode.PERCENT
    percent: float = 40.0          # PERCENT：相对页面宽度百分比
    fixed_width: int = 260         # FIXED：像素宽度
    fixed_height: Optional[float] = None  # FIXED：可选固定高度（不提供则按图比等比）
    opacity: float = 1.0           # 0~1，仅预览时的透明度；导出始终不透明
    margin: float = 12.0           # 页面边距（像素）
    rotate_per_page: bool = False  # 多图时：True=每页轮换一张；False=每页叠放全部图
    dx: float = 0.0                # 手动偏移 X（像素，在锚点基础上再平移）
    dy: float = 0.0                # 手动偏移 Y（像素）
    pages_filter: Optional[List[int]] = None  # 仅这些页码应用；None=全部页码（页码从1开始）


def image_native_size(path: Path) -> tuple[int, int]:
    """返回图片原始像素宽高。"""
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def rect_for(layout: Layout, page_w: float, page_h: float,
             img_w: float, img_h: float) -> tuple[float, float, float, float]:
    """返回水印图矩形的左上角坐标与宽高 (x, y, w, h)，左上角原点。

    DEFAULT：像素 1:1，超出页面则等比收缩到页内；
    其它模式：按目标宽度等比缩放（高度因此保持图片比例）。
    """
    ratio = img_h / img_w if img_w else 1.0
    target_w = target_width_for(layout, page_w)

    if target_w is None:
        w, h = img_w, img_h
        if w > page_w - 2 * layout.margin:
            w = page_w - 2 * layout.margin
            h = w * ratio
        if h > page_h - 2 * layout.margin:
            h = page_h - 2 * layout.margin
            w = h / ratio
    else:
        w = float(target_w)
        if layout.fixed_height is not None:
            h = float(layout.fixed_height)
            # 超出页面时整体等比收缩
            if w > page_w - 2 * layout.margin:
                k = (page_w - 2 * layout.margin) / w
                w, h = w * k, h * k
            if h > page_h - 2 * layout.margin:
                k = (page_h - 2 * layout.margin) / h
                w, h = w * k, h * k
        else:
            h = w * ratio
            if h > page_h - 2 * layout.margin:
                h = page_h - 2 * layout.margin
                w = h / ratio

    m = layout.margin
    a = layout.anchor
    if a is Anchor.TOP_LEFT:
        x_top, y_top = m, m
    elif a is Anchor.TOP_CENTER:
        x_top, y_top = (page_w - w) / 2, m
    elif a is Anchor.TOP_RIGHT:
        x_top, y_top = page_w - w - m, m
    elif a is Anchor.CENTER:
        x_top, y_top = (page_w - w) / 2, (page_h - h) / 2
    elif a is Anchor.BOTTOM_LEFT:
        x_top, y_top = m, page_h - h - m
    elif a is Anchor.BOTTOM_CENTER:
        x_top, y_top = (page_w - w) / 2, page_h - h - m
    else:  # BOTTOM_RIGHT
        x_top, y_top = page_w - w - m, page_h - h - m

    return x_top + layout.dx, y_top + layout.dy, w, h


def target_width_for(layout: Layout, page_w: float) -> Optional[float]:
    """返回目标像素宽度；DEFAULT 返回 None 表示用图片原生尺寸。"""
    if layout.size_mode is SizeMode.PERCENT:
        return page_w * layout.percent / 100.0
    if layout.size_mode is SizeMode.FIXED:
        return float(layout.fixed_width)
    return None


def render_page_bg(pdf_path: Path, page_index: int = 0,
                   zoom: float = 1.0) -> "tuple[int, int, Image.Image]":
    """渲染 PDF 第 page_index 页为 RGBA 位图（不含水印）。

    返回 (width_px, height_px, pil_image)。页面位图可被缓存复用，
    拖动水印时只需重新叠加而不必重新渲染 PDF。
    """
    import fitz
    from PIL import Image

    with fitz.open(str(pdf_path)) as doc:
        page = doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGBA")
    return pix.width, pix.height, img


def composite_watermark(bg: "Image.Image", layout: Layout,
                        images: Optional[list[Path]] = None) -> "Image.Image":
    """把水印图按 layout 合成到页面背景 bg 上，返回新位图。

    纯内存合成（不含 PDF 渲染），适合高频调用（如鼠标拖动预览）。
    """
    from PIL import Image

    page_w, page_h = bg.size
    result = bg.copy()
    for path in images or []:
        iw, ih = image_native_size(path)
        x, y, w, h = rect_for(layout, page_w, page_h, iw, ih)
        with Image.open(path) as stamp:
            stamp_img = stamp.convert("RGBA").resize(
                (max(1, int(round(w))), max(1, int(round(h)))), Image.LANCZOS)
            if layout.opacity < 1.0:
                alpha = stamp_img.getchannel("A").point(
                    lambda a: int(a * layout.opacity))
                stamp_img.putalpha(alpha)
            result.paste(stamp_img, (int(round(x)), int(round(y))), stamp_img)
    return result


def render_page_preview(pdf_path: Path, layout: Layout,
                        images: Optional[list[Path]] = None,
                        page_index: int = 0, zoom: float = 1.0,
                        bg: Optional["Image.Image"] = None):
    """渲染 PDF 第 page_index 页并叠加水印，返回像素数据。

    返回 (width_px, height_px, rgba_bytes)。若提供缓存的 bg，
    则跳过 PDF 重新渲染，仅做水印合成（更快）。
    """
    if bg is None:
        page_w, page_h, bg = render_page_bg(pdf_path, page_index, zoom)
    else:
        page_w, page_h = bg.size
    composed = composite_watermark(bg, layout, images)
    return page_w, page_h, composed.tobytes(), composed


def export_pdf(pdf_path: str, output_path: str, layout: Optional[Layout] = None,
               images: Optional[list[Path]] = None,
               progress_cb=None,
               page_layouts: Optional[dict[int, Layout]] = None,
               page_image_layouts: Optional[dict[int, dict[int, Layout]]] = None) -> None:
    """按布局将 images 叠加到每一页并写出 output_path。

    三种布局来源（从前到后优先）：
    - page_image_layouts：最精细 —— 某页内每张图片各自独立布局。
      {page_index: {image_index: Layout}}；页不在 dict 则整页不加。
    - page_layouts：按页取布局；某页不在 dict 中则该页不加水印。
    - 否则使用全局 layout（含 layout.pages_filter / rotate_per_page）。

    多图策略（无 per-image 时）：
    - layout.rotate_per_page 为 True 时，每页循环取一张图片；
    - 否则在一页里按顺序叠放全部图片。
    progress_cb(done_pages, total_pages) 可选，导出过程中循环调用。

    实现说明（性能关键，勿改回逐页 PyPDF2 merge）：
    - 全部水印页先画进一个 reportlab「薄覆盖层」PDF：同一张水印图
      在覆盖层内只嵌入一次（XObject 跨页共享），几百页的导出产物
      体积不再随页数线性膨胀。
    - 再用 PyMuPDF show_pdf_page 把覆盖层页合并到源页（overlay=True
      画在原内容之上）。整条链路都在 C 层完成，替代此前 PyPDF2
      纯 Python 的逐页 merge_page（每页 0.5~1s，长文档表现为「卡死」）。
    - /Rotate 页保持既有约定：布局矩形以显示方向为基准，覆盖层仍按
      未旋转 mediabox 空间绘制（绘制时手工逆变换），合并矩形亦取
      未旋转 mediabox，与旧实现的像素结果逐位一致（_verify_align 覆盖）。
    """
    if images is None:
        images = []
    if layout is None:
        layout = Layout()

    import fitz

    doc = fitz.open(pdf_path)
    try:
        n_pages = doc.page_count
        allowed = layout.pages_filter  # Optional[set[int]] 0基；None=全部
        native_cache: dict[str, tuple[int, int]] = {}

        def _native(path) -> tuple[int, int]:
            key = str(path)
            if key not in native_cache:
                native_cache[key] = image_native_size(path)
            return native_cache[key]

        # ---- 第一遍：确定每页水印组合并绘制薄覆盖层 ----
        page_pairs: list[list[tuple]] = []
        overlay_index_of: dict[int, int] = {}
        packet = io.BytesIO()
        c = canvas.Canvas(packet)
        overlay_count = 0

        for page_index in range(n_pages):
            if progress_cb:
                progress_cb(page_index, n_pages)
            page = doc[page_index]

            # ---- 选定本页各图布局（与旧实现逐分支一致） ----
            if page_image_layouts is not None:
                per_img = page_image_layouts.get(page_index)
                if not per_img:
                    page_pairs.append([])
                    continue
                pairs = [(images[i] if i < len(images) else None, lay)
                         for i, lay in per_img.items()]
                pairs = [(p, l) for p, l in pairs if p is not None]
            elif page_layouts is not None:
                page_layout = page_layouts.get(page_index)
                if page_layout is None:
                    page_pairs.append([])
                    continue
                pairs = [(p, page_layout) for p in images]
            else:
                page_layout = layout
                if allowed is not None and page_index not in allowed:
                    page_pairs.append([])
                    continue
                if page_layout.rotate_per_page:
                    pairs = ([(images[page_index % len(images)], page_layout)]
                             if images else [])
                else:
                    pairs = [(p, page_layout) for p in images]
            page_pairs.append(pairs)
            if not pairs:
                continue

            mb = page.mediabox
            page_w = float(mb.width)
            page_h = float(mb.height)
            # 页面旋转属性（0/90/180/270）。预览（fitz）按显示方向渲染，
            # 因此布局矩形以「显示方向」为基准，绘制时逆变换回未旋转坐标。
            rot = int(page.rotation or 0) % 360
            disp_w, disp_h = ((page_h, page_w) if rot in (90, 270)
                              else (page_w, page_h))

            c.setPageSize((page_w, page_h))
            for img_path, img_layout in pairs:
                iw, ih = _native(img_path)
                # 显示方向坐标系（左上原点）中的矩形
                x, y, w, h = rect_for(img_layout, disp_w, disp_h, iw, ih)
                c.saveState()
                if rot == 0:
                    # reportlab 坐标：从左下原点，因此 y_bottom = page_h - y_tl - h
                    c.drawImage(str(img_path), x, page_h - y - h, width=w,
                                height=h, mask="auto")
                elif rot == 90:
                    c.translate(y + h, x)
                    c.rotate(90)
                    c.drawImage(str(img_path), 0, 0, width=w, height=h,
                                mask="auto")
                elif rot == 180:
                    c.translate(page_w - x, page_h - y)
                    c.rotate(180)
                    c.drawImage(str(img_path), 0, 0, width=w, height=h,
                                mask="auto")
                else:  # 270
                    c.translate(page_w - y - h, page_h - x)
                    c.rotate(-90)
                    c.drawImage(str(img_path), 0, 0, width=w, height=h,
                                mask="auto")
                c.restoreState()
            c.showPage()
            overlay_index_of[page_index] = overlay_count
            overlay_count += 1
        c.save()

        # ---- 第二遍：fitz 将覆盖层合并到源页并写出 ----
        if overlay_count:
            packet.seek(0)
            overlay = fitz.open(stream=packet.read(), filetype="pdf")
            try:
                for page_index, ov_index in overlay_index_of.items():
                    page = doc[page_index]
                    mb = page.mediabox
                    page.show_pdf_page(
                        fitz.Rect(0.0, 0.0, float(mb.width), float(mb.height)),
                        overlay, ov_index, overlay=True,
                    )
            finally:
                overlay.close()
        data = doc.tobytes(garbage=1, deflate=True)
    finally:
        doc.close()

    with open(output_path, "wb") as f:
        f.write(data)


def merge_overlay(page, overlay_pdf_bytes: bytes):
    """把 reportlab 生成的覆盖层 PDF 合并到目标页。"""
    overlay_reader = PdfReader(io.BytesIO(overlay_pdf_bytes))
    overlay_page = overlay_reader.pages[0]
    page.merge_page(overlay_page)
    return page


def list_pdf_pages(pdf_path: str) -> int:
    """返回 PDF 页数。"""
    return len(PdfReader(pdf_path).pages)


def parse_page_range(spec: str, page_count: int) -> Optional[set[int]]:
    """解析页码范围（页码从 1 开始）。

    支持：空 / "all" → None（全部）；"1-5"、"1,3,7"、"1,3-5,9" 等。
    越界页码自动忽略；返回 set[int]（0 基索引），或 None 表示不限。
    """
    spec = (spec or "").strip()
    if not spec or spec.lower() == "all":
        return None
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            try:
                lo, hi = int(lo_s.strip()), int(hi_s.strip())
            except ValueError:
                continue
            lo, hi = sorted((lo, hi))
        else:
            try:
                lo = hi = int(part)
            except ValueError:
                continue
        lo = max(1, lo)
        hi = min(page_count, hi)
        result.update(range(lo, hi + 1))
    # 零基索引
    return {p - 1 for p in result if 1 <= p <= page_count}