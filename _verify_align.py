# -*- coding: utf-8 -*-
"""临时验证：预览与导出的水印几何逐像素一致（T1）。

对每个用例：
- 预览 = fitz 渲染(zoom=1.4) + composite_watermark(page_layout(zoom=1.4))
- 导出 = export_pdf(page_layout(zoom=1.0)) 后再用 fitz 渲染(zoom=1.4)
- 断言两图平均像素差很小，且水印区域（相对空白页 diff 的 bbox）
  中心与尺寸一致。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\pyprojects\watermark")

import fitz
from PIL import ImageChops, ImageStat

import pdf_watermark_demo as demo
from pdf_watermark_studio.core.engine import (
    render_page_bg, composite_watermark, export_pdf,
)

ROOT = Path(r"D:\pyprojects\watermark")
SAMPLE = ROOT / "8号线一期南沂区间消防设计说明 (3).pdf"
STAMP = ROOT / "章.png"
ZOOM = demo.PREVIEW_ZOOM
TMP = Path(tempfile.mkdtemp(prefix="wm_verify_"))


def preview_image(pdf_path: Path, st: "demo.PageState", stamp: Path):
    w, h, bg = render_page_bg(pdf_path, 0, zoom=ZOOM)
    layout = demo.page_layout(st, zoom=ZOOM, image_path=str(stamp))
    return w, h, composite_watermark(bg, layout, [stamp])


def exported_render(pdf_path: Path, st: "demo.PageState", stamp: Path,
                    out: Path):
    layout = demo.page_layout(st, zoom=1.0, image_path=str(stamp))
    export_pdf(str(pdf_path), str(out), images=[stamp],
               page_image_layouts={0: {0: layout}})
    w, h, img = render_page_bg(out, 0, zoom=ZOOM)
    return w, h, img


def plain_render(pdf_path: Path):
    w, h, img = render_page_bg(pdf_path, 0, zoom=ZOOM)
    return w, h, img


def wm_bbox_of(img_a, img_b):
    """水印区域：diff 后按阈值滤掉内容流合并带来的全页低幅噪声。"""
    diff = ImageChops.difference(img_a.convert("RGB"), img_b.convert("RGB"))
    gray = diff.convert("L").point(lambda v: 255 if v > 24 else 0)
    return gray.getbbox()


def check_case(name: str, pdf_path: Path, st: "demo.PageState", stamp: Path):
    out = TMP / f"{name}.pdf"
    pw, ph, prev = preview_image(pdf_path, st, stamp)
    ew, eh, exp = exported_render(pdf_path, st, stamp, out)
    _, _, plain = plain_render(pdf_path)
    assert (pw, ph) == (ew, eh), f"{name}: size mismatch {pw}x{ph} vs {ew}x{eh}"

    mean_diff = ImageStat.Stat(
        ImageChops.difference(prev.convert("RGB"), exp.convert("RGB"))
    ).mean
    mean_diff = sum(mean_diff) / 3

    wm_bbox = wm_bbox_of(prev, plain)
    ex_bbox = wm_bbox_of(exp, plain)
    assert wm_bbox and ex_bbox, f"{name}: watermark missing ({wm_bbox}, {ex_bbox})"

    c1 = ((wm_bbox[0] + wm_bbox[2]) / 2, (wm_bbox[1] + wm_bbox[3]) / 2)
    c2 = ((ex_bbox[0] + ex_bbox[2]) / 2, (ex_bbox[1] + ex_bbox[3]) / 2)
    s1 = (wm_bbox[2] - wm_bbox[0], wm_bbox[3] - wm_bbox[1])
    s2 = (ex_bbox[2] - ex_bbox[0], ex_bbox[3] - ex_bbox[1])
    dc = ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2) ** 0.5
    ds = max(abs(s1[i] - s2[i]) / max(1, s1[i]) for i in (0, 1))

    ok = mean_diff < 4.0 and dc < 3.0 and ds < 0.02
    print(f"[{name}] mean_diff={mean_diff:.2f} dcenter={dc:.1f}px "
          f"dsz={ds * 100:.1f}% prev_bbox={wm_bbox} exp_bbox={ex_bbox} "
          f"-> {'OK' if ok else 'FAIL'}")
    return ok


def make_rotated_pdf(out: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(100, 200, 500, 600), color=(0.2, 0.4, 0.8),
                   fill=(0.85, 0.9, 1.0))
    page.insert_text((120, 260), "ROTATED PAGE TEST", fontsize=24,
                     color=(0.1, 0.1, 0.4))
    page.set_rotation(90)
    doc.save(str(out))
    doc.close()
    return out


def main() -> int:
    results = []
    # A: 百分比模式 + 偏移
    results.append(check_case(
        "percent30", SAMPLE,
        demo.PageState(wm=True, dx=37.5, dy=-23.0, size_mode=demo.MODE_PERCENT,
                       percent=30.0),
        STAMP))
    # B: CM 模式（回归：导出曾丢失 cm_w/cm_h）
    results.append(check_case(
        "cm6.5x3.2", SAMPLE,
        demo.PageState(wm=True, dx=-40.0, dy=55.0, size_mode=demo.MODE_CM,
                       cm_w=6.5, cm_h=3.2),
        STAMP))
    # C: 原始尺寸模式
    results.append(check_case(
        "original", SAMPLE,
        demo.PageState(wm=True, dx=0.0, dy=0.0, size_mode=demo.MODE_ORIGINAL),
        STAMP))
    # D: 旋转页（/Rotate 90）
    rot = make_rotated_pdf(TMP / "rot90.pdf")
    results.append(check_case(
        "rot90_percent40", rot,
        demo.PageState(wm=True, dx=20.0, dy=30.0, size_mode=demo.MODE_PERCENT,
                       percent=40.0),
        STAMP))
    # E: 双水印不同偏移
    out2 = TMP / "two.pdf"
    st_a = demo.PageState(wm=True, dx=-60.0, dy=-80.0,
                          size_mode=demo.MODE_PERCENT, percent=25.0)
    st_b = demo.PageState(wm=True, dx=50.0, dy=70.0, size_mode=demo.MODE_CM,
                          cm_w=4.0, cm_h=4.0)
    lay_a = demo.page_layout(st_a, zoom=1.0, image_path=str(STAMP))
    lay_b = demo.page_layout(st_b, zoom=1.0, image_path=str(STAMP))
    w, h, bg = render_page_bg(SAMPLE, 0, zoom=ZOOM)
    prev2 = composite_watermark(bg, demo.page_layout(st_a, zoom=ZOOM,
                                                     image_path=str(STAMP)),
                                [STAMP])
    prev2 = composite_watermark(prev2, demo.page_layout(st_b, zoom=ZOOM,
                                                        image_path=str(STAMP)),
                                [STAMP])
    export_pdf(str(SAMPLE), str(out2), images=[STAMP],
               page_image_layouts={0: {0: lay_a, 1: lay_b}})
    _, _, exp2 = render_page_bg(out2, 0, zoom=ZOOM)
    md = sum(ImageStat.Stat(ImageChops.difference(
        prev2.convert("RGB"), exp2.convert("RGB"))).mean) / 3
    ok2 = md < 4.0
    print(f"[two_stamps] mean_diff={md:.2f} -> {'OK' if ok2 else 'FAIL'}")
    results.append(ok2)

    print("TMP:", TMP)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
