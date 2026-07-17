"""
PDF reporting layer.

Consumes the JSON produced by `pipeline.py` (plus the per-component crop
PNGs and segmentation overviews it wrote alongside) and renders a
structured ReportLab PDF.

Usage:
    # Run the CV pipeline (which auto-runs reporting):
    python pipeline.py

    # ...or just re-render the report from existing JSON:
    python report.py
    python report.py --json linked_component_output/linked_components.json \\
                     --pdf  linked_component_output/linked_components_report.pdf
"""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import dataclass
from pathlib import Path

import cv2
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
      SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
      Image as RLImage, PageBreak, KeepTogether,
)

# ============================================================
# DEFAULT PATHS (match pipeline.py)
# ============================================================

DEFAULT_OUT_DIR = Path("linked_component_output")
DEFAULT_JSON = DEFAULT_OUT_DIR / "linked_components.json"
DEFAULT_PDF = DEFAULT_OUT_DIR / "linked_components_report.pdf"


# ============================================================
# GEOMETRY
# ============================================================

@dataclass(frozen=True)
class Layout:
      """
      Single source of truth for page geometry. Every horizontal width
      is derived from `usable_width` so columns always fit and never clip.
      """
      page_w: float
      page_h: float
      margin_left: float
      margin_right: float
      margin_top: float
      margin_bottom: float

      @property
      def usable_width(self) -> float:
            return self.page_w - self.margin_left - self.margin_right

      @property
      def usable_height(self) -> float:
            return self.page_h - self.margin_top - self.margin_bottom

      # Cross-image link row geometry: [left | tag | right]
      # Tag is a fixed slim column; left and right share the rest equally.
      @property
      def link_tag_w(self) -> float:
            return 0.65 * inch

      @property
      def link_side_w(self) -> float:
            # 6pt internal gap between cells (Table cellPadding handles spacing)
            return (self.usable_width - self.link_tag_w) / 2.0

      # Inside a side stack, the component card takes the full side width.
      @property
      def card_w(self) -> float:
            return self.link_side_w

      # Card internal split: [crop image | field table]
      # Image gets ~38% of card width, fields ~62%, with 6pt internal padding.
      @property
      def card_image_w(self) -> float:
            return self.card_w * 0.38

      @property
      def card_image_h(self) -> float:
            # Square-ish; capped so a card doesn't get absurdly tall on narrow pages.
            return min(self.card_image_w * 1.15, 2.0 * inch)

      @property
      def card_fields_w(self) -> float:
            # 12pt total internal padding budget per card
            return self.card_w - self.card_image_w - 12.0

      @property
      def card_fields_label_w(self) -> float:
            return self.card_fields_w * 0.40

      @property
      def card_fields_value_w(self) -> float:
            return self.card_fields_w * 0.60


def default_layout() -> Layout:
      """Letter, 0.5" side margins (printer-safe), 0.55"/0.5" top/bottom."""
      page_w, page_h = letter
      return Layout(
            page_w=page_w,
            page_h=page_h,
            margin_left=0.5 * inch,
            margin_right=0.5 * inch,
            margin_top=0.55 * inch,
            margin_bottom=0.5 * inch,
      )


# ============================================================
# STYLES
# ============================================================

def _styles():
      base = getSampleStyleSheet()
      return {
            "title": ParagraphStyle("title", parent=base["Title"],
                                    fontSize=20, leading=24, spaceAfter=6,
                                    alignment=TA_LEFT),
            "subtitle": ParagraphStyle("subtitle", parent=base["Normal"],
                                       fontSize=10, leading=13,
                                       textColor=colors.HexColor("#555555"),
                                       spaceAfter=12),
            "h1": ParagraphStyle("h1", parent=base["Heading1"],
                                 fontSize=14, leading=18,
                                 spaceBefore=8, spaceAfter=6,
                                 textColor=colors.HexColor("#1f3a68")),
            "h2": ParagraphStyle("h2", parent=base["Heading2"],
                                 fontSize=11, leading=14,
                                 spaceBefore=2, spaceAfter=4,
                                 textColor=colors.HexColor("#2a2a2a")),
            "body": ParagraphStyle("body", parent=base["Normal"],
                                   fontSize=9.5, leading=12),
            "small": ParagraphStyle("small", parent=base["Normal"],
                                    fontSize=8.5, leading=11,
                                    textColor=colors.HexColor("#444444")),
            "mono": ParagraphStyle("mono", parent=base["Normal"],
                                   fontName="Courier", fontSize=8, leading=10),
            "tag_text": ParagraphStyle("tag_text", parent=base["Normal"],
                                       fontSize=8.5, leading=10.5,
                                       alignment=TA_CENTER,
                                       textColor=colors.white),
      }


# ============================================================
# IMAGE HELPERS
# ============================================================

def _scaled_image(path: str, max_w: float, max_h: float) -> RLImage | None:
      """Return a ReportLab Image scaled to fit the box, preserving aspect."""
      if not path or not Path(path).exists():
            return None
      img = cv2.imread(path)
      if img is None:
            return None
      h, w = img.shape[:2]
      scale = min(max_w / w, max_h / h)
      new_w = max(1, w * scale)
      new_h = max(1, h * scale)
      ok, buf = cv2.imencode(".png", img)
      if not ok:
            return None
      return RLImage(io.BytesIO(buf.tobytes()), width=new_w, height=new_h)


# ============================================================
# COMPONENT CARD
# ============================================================

def _build_field_rows(comp: dict, styles) -> list:
      def cell(label, value):
            return [Paragraph(f"<b>{label}</b>", styles["small"]),
                    Paragraph(str(value) if value not in (None, "") else "&mdash;",
                              styles["small"])]

      return [
            cell("Image / Obj",
                 f"Image {comp['image_index']} · Component {comp['component_index']}"),
            cell("Type", comp.get("component_type")),
            cell("Manufacturer", comp.get("manufacturer")),
            cell("Product", comp.get("product")),
            cell("Strength", comp.get("strength")),
            cell("NDC", comp.get("ndc")),
            cell("LOT", comp.get("lot")),
            cell("EXP", comp.get("expiration")),
            cell("Volume",
                 f"{comp['volume_ml']} mL" if comp.get("volume_ml") else None),
            cell("OCR",
                 f"rot {comp.get('best_rotation', 0)}° + "
                 f"{comp.get('best_deskew', 0.0):+.1f}° · "
                 f"PSM {comp.get('best_psm', '?')} · "
                 f"{comp.get('best_preprocess', '?')} · "
                 f"conf {comp.get('ocr_confidence', 0.0):.0f}"),
      ]


def _component_card(comp: dict, styles, layout: Layout,
                    width_override: float | None = None):
      """
      Return a 2-column inner Table: [crop image | field table].

      `width_override` lets callers force a non-default card width for
      custom rows (e.g. equivalence-class layouts with N cards across).
      All internal proportions are recomputed from the override.
      """
      if width_override is not None:
            card_w = width_override
            image_w = card_w * 0.38
            image_h = min(image_w * 1.15, 2.0 * inch)
            fields_w = card_w - image_w - 12.0
            label_w = fields_w * 0.40
            value_w = fields_w * 0.60
      else:
            image_w = layout.card_image_w
            image_h = layout.card_image_h
            fields_w = layout.card_fields_w
            label_w = layout.card_fields_label_w
            value_w = layout.card_fields_value_w

      img = _scaled_image(comp.get("crop_path", ""), image_w, image_h)
      img_cell = img if img else Paragraph("(no crop)", styles["small"])

      field_rows = _build_field_rows(comp, styles)
      fields = Table(field_rows, colWidths=[label_w, value_w])
      fields.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#dddddd")),
      ]))

      accent = (colors.HexColor("#4287f5")
                if comp["image_index"] == 1
                else colors.HexColor("#3caa5a"))

      inner = Table([[img_cell, fields]],
                    colWidths=[image_w + 6, fields_w + 6])
      inner.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cccccc")),
            ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
      ]))
      return inner


# ============================================================
# LINK ROW
# ============================================================

def _stack(cards, styles, layout: Layout):
      """Vertical stack of cards (or a placeholder when empty)."""
      if not cards:
            return Paragraph("<i>(none)</i>", styles["small"])
      rows = [[c] for c in cards]
      t = Table(rows, colWidths=[layout.link_side_w])
      t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
      ]))
      return t


def _link_block(left_cards, right_cards, link_label, link_color,
                styles, layout: Layout):
      """One row: [left stack | link tag | right stack]."""
      link_para = Paragraph(link_label, styles["tag_text"])
      link_cell = Table([[link_para]], colWidths=[layout.link_tag_w])
      link_cell.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("BACKGROUND", (0, 0), (-1, -1), link_color),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
      ]))

      outer = Table(
            [[_stack(left_cards, styles, layout),
              link_cell,
              _stack(right_cards, styles, layout)]],
            colWidths=[layout.link_side_w, layout.link_tag_w, layout.link_side_w],
      )
      outer.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
      ]))
      return outer


# ============================================================
# REPORT BUILDER
# ============================================================

TIER_COLORS = {
      "identity": colors.HexColor("#2f8a4a"),
      "sku": colors.HexColor("#3a7bd5"),
      "therapeutic": colors.HexColor("#c08a2c"),
      "unmatched": colors.HexColor("#888888"),
}


def build_report(result: dict, out_pdf: str | Path,
                 out_dir: Path = DEFAULT_OUT_DIR,
                 layout: Layout | None = None):
      """Render the PDF report from a pipeline result dict."""
      if layout is None:
            layout = default_layout()

      image_entries = result["images"]
      comps1 = image_entries[0]["components"]
      comps2 = image_entries[1]["components"]
      img1_path = image_entries[0]["path"]
      img2_path = image_entries[1]["path"]

      by1 = {c["component_index"]: c for c in comps1}
      by2 = {c["component_index"]: c for c in comps2}

      linkage = result["linkage"]
      classes1 = linkage["equiv_classes_image1"]
      classes2 = linkage["equiv_classes_image2"]
      within1 = linkage["within_image_relations"]["image1"]
      within2 = linkage["within_image_relations"]["image2"]
      identity_links = linkage["links_by_tier"]["identity"]
      sku_links = linkage["links_by_tier"]["sku"]
      therapeutic_links = linkage["links_by_tier"]["therapeutic"]
      unmatched1 = linkage["unmatched_image1"]
      unmatched2 = linkage["unmatched_image2"]

      styles = _styles()
      doc = SimpleDocTemplate(
            str(out_pdf), pagesize=(layout.page_w, layout.page_h),
            leftMargin=layout.margin_left, rightMargin=layout.margin_right,
            topMargin=layout.margin_top, bottomMargin=layout.margin_bottom,
            title="Cross-Image Component Linking Report",
      )
      story = []

      # ---- cover ----
      cover = []
      cover.append(Paragraph("Cross-Image Component Linking Report",
                             styles["title"]))
      cover.append(Paragraph(
            f"Image 1: {Path(img1_path).name} &nbsp;·&nbsp; "
            f"Image 2: {Path(img2_path).name}", styles["subtitle"]))

      summary_rows = [
            ["", "Image 1", "Image 2"],
            ["Components detected", str(len(comps1)), str(len(comps2))],
            ["Equivalence classes", str(len(classes1)), str(len(classes2))],
            ["Within-image relations", str(len(within1)), str(len(within2))],
            ["Identity (very high) links", str(len(identity_links)), ""],
            ["SKU (high) links", str(len(sku_links)), ""],
            ["Therapeutic (medium) links", str(len(therapeutic_links)), ""],
            ["Unmatched", str(len(unmatched1)), str(len(unmatched2))],
      ]
      summary_label_w = layout.usable_width * 0.50
      summary_val_w = (layout.usable_width - summary_label_w) / 2.0
      st = Table(summary_rows,
                 colWidths=[summary_label_w, summary_val_w, summary_val_w])
      st.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a68")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
      ]))
      cover.append(st)
      cover.append(Spacer(1, 14))

      cover.append(Paragraph("Confidence tiers", styles["h2"]))
      tier_explainer = [
            ["Identity",
             "Same physical lot. Shared NDC + Lot (+ optional Exp), or shared "
             "Lot + Exp/Mfg with no conflicts.", "very high"],
            ["SKU",
             "Same product SKU. Shared NDC; lots may differ or be unobserved.",
             "high"],
            ["Therapeutic",
             "Same drug + strength but different manufacturer / NDC. Functionally "
             "substitutable, not the same SKU.",
             "medium"],
      ]
      tier_label_w = layout.usable_width * 0.18
      tier_conf_w = layout.usable_width * 0.16
      tier_desc_w = layout.usable_width - tier_label_w - tier_conf_w
      tx = Table(
            [[Paragraph(f"<b>{a}</b>", styles["small"]),
              Paragraph(b, styles["small"]),
              Paragraph(f"<i>{c}</i>", styles["small"])]
             for a, b, c in tier_explainer],
            colWidths=[tier_label_w, tier_desc_w, tier_conf_w],
      )
      tx.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#dddddd")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
      ]))
      cover.append(tx)

      story.append(KeepTogether(cover))
      story.append(PageBreak())

      # ---- segmentation overviews ----
      seg_max_w = layout.usable_width
      seg_max_h = layout.usable_height * 0.42
      for idx in (1, 2):
            seg_path = out_dir / f"img{idx}_segmentation.png"
            seg_img = _scaled_image(str(seg_path), seg_max_w, seg_max_h)
            seg_chunks = [Paragraph(f"Image {idx} segmentation", styles["h1"])]
            if seg_img:
                  seg_chunks.append(seg_img)
            seg_chunks.append(Spacer(1, 8))
            story.append(KeepTogether(seg_chunks))
      story.append(PageBreak())

      # ---- equivalence-class overviews (only show non-singleton classes) ----
      multi_classes = [(1, c) for c in classes1 if len(c["member_indices"]) > 1] \
                      + [(2, c) for c in classes2 if len(c["member_indices"]) > 1]
      if multi_classes:
            story.append(Paragraph("Within-image equivalence classes", styles["h1"]))
            story.append(Paragraph(
                  "Components grouped here share enough identifying fields "
                  "(typically Lot + Exp, possibly with NDC or Mfg) to be treated as "
                  "the same physical lot. Their evidence is merged before "
                  "cross-image matching.",
                  styles["body"]))
            story.append(Spacer(1, 6))
            for img_idx, c in multi_classes:
                  members = c["member_indices"]
                  comps = [by1[m] if img_idx == 1 else by2[m] for m in members]
                  header = Paragraph(
                        f"<b>Image {img_idx} · Components {members}</b> &nbsp; "
                        f"<font color='#666666'>(grouped: {c['grouping_reason']})</font>",
                        styles["h2"])
                  # Lay cards out in a row, each card capped at link_side_w.
                  n = len(comps)
                  col_w = layout.usable_width / max(n, 1)
                  if col_w > layout.link_side_w:
                        col_w = layout.link_side_w
                  cards = [_component_card(c, styles, layout, width_override=col_w)
                           for c in comps]
                  block = Table([cards], colWidths=[col_w] * n)
                  block.setStyle(TableStyle([
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 2),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                  ]))
                  story.append(KeepTogether([header, block, Spacer(1, 10)]))
            story.append(PageBreak())

      # ---- cross-image link sections ----
      def _render_links(links, section_title, tier_label, tier_color):
            if not links:
                  return
            story.append(Paragraph(section_title, styles["h1"]))
            sorted_links = sorted(
                  links,
                  key=lambda lk: (lk["image1_components"], lk["image2_components"]))
            for lk in sorted_links:
                  left_indices = lk["image1_components"]
                  right_indices = lk["image2_components"]
                  left = [by1[i] for i in left_indices if i in by1]
                  right = [by2[j] for j in right_indices if j in by2]
                  shared = lk.get("shared_fields") or []
                  inferred = lk.get("inferred_fields") or []
                  notes = lk.get("notes") or ""

                  header_html = (
                        f"<b>img1 {left_indices}</b> &nbsp;↔&nbsp; "
                        f"<b>img2 {right_indices}</b> &nbsp; "
                        f"<font color='#666666'>"
                        f"confidence: {lk['confidence']} · score {lk['score']:.2f}"
                        f"</font>"
                  )
                  chunks = [Paragraph(header_html, styles["h2"])]

                  ev_lines = []
                  if shared:
                        ev_lines.append(f"<b>shared:</b> {', '.join(shared)}")
                  if inferred:
                        ev_lines.append(f"<b>inferred via class merge:</b> "
                                        f"{', '.join(inferred)}")
                  if notes:
                        ev_lines.append(f"<b>notes:</b> {notes}")
                  if ev_lines:
                        chunks.append(Paragraph("<br/>".join(ev_lines), styles["small"]))

                  chunks.append(Spacer(1, 4))
                  chunks.append(_link_block(
                        [_component_card(c, styles, layout) for c in left],
                        [_component_card(c, styles, layout) for c in right],
                        f"{tier_label}<br/>{lk['score']:.1f}",
                        tier_color, styles, layout,
                  ))
                  chunks.append(Spacer(1, 12))
                  story.append(KeepTogether(chunks))
            story.append(PageBreak())

      _render_links(identity_links,
                    "Identity links — same physical lot",
                    "IDENTITY", TIER_COLORS["identity"])
      _render_links(sku_links,
                    "SKU links — same product, different/unknown lot",
                    "SKU", TIER_COLORS["sku"])
      _render_links(therapeutic_links,
                    "Therapeutic links — same drug, different manufacturer",
                    "THERAP.", TIER_COLORS["therapeutic"])

      # ---- within-image cross-class relations ----
      if within1 or within2:
            story.append(Paragraph("Within-image relations", styles["h1"]))
            story.append(Paragraph(
                  "Components in the same image that aren't the same physical lot "
                  "but represent the same SKU or are therapeutically equivalent.",
                  styles["body"]))
            story.append(Spacer(1, 6))

            for img_idx, rel_list in [(1, within1), (2, within2)]:
                  for rel in rel_list:
                        a_idx = rel["class_a_components"]
                        b_idx = rel["class_b_components"]
                        source = by1 if img_idx == 1 else by2
                        left = [source[i] for i in a_idx if i in source]
                        right = [source[j] for j in b_idx if j in source]
                        header = Paragraph(
                              f"<b>Image {img_idx}</b>: "
                              f"{a_idx} ↔ {b_idx} &nbsp; "
                              f"<font color='#666666'>"
                              f"{rel['tier']} ({rel['confidence']})"
                              f"</font>",
                              styles["h2"])
                        shared = rel.get("shared_fields") or []
                        notes = rel.get("notes") or ""
                        meta_html = []
                        if shared:
                              meta_html.append(f"<b>shared:</b> {', '.join(shared)}")
                        if notes:
                              meta_html.append(f"<b>notes:</b> {notes}")

                        tier_color = TIER_COLORS.get(rel["tier"], TIER_COLORS["sku"])
                        chunks = [header]
                        if meta_html:
                              chunks.append(Paragraph("<br/>".join(meta_html),
                                                      styles["small"]))
                        chunks.append(Spacer(1, 4))
                        chunks.append(_link_block(
                              [_component_card(c, styles, layout) for c in left],
                              [_component_card(c, styles, layout) for c in right],
                              rel["tier"].upper(), tier_color, styles, layout,
                        ))
                        chunks.append(Spacer(1, 12))
                        story.append(KeepTogether(chunks))
            story.append(PageBreak())

      # ---- unmatched ----
      if unmatched1 or unmatched2:
            story.append(Paragraph("Unmatched components", styles["h1"]))
            for u in unmatched1:
                  members = u["component_indices"]
                  comps = [by1[i] for i in members if i in by1]
                  header = Paragraph(
                        f"<b>Image 1 · Component(s) {members}</b> &nbsp; "
                        f"<font color='#666666'>"
                        f"(no cross-image match — {u.get('reason', 'no_candidate')})"
                        f"</font>",
                        styles["h2"])
                  chunks = [
                        header,
                        _link_block(
                              [_component_card(c, styles, layout) for c in comps], [],
                              "ONLY<br/>IMG 1", TIER_COLORS["unmatched"],
                              styles, layout),
                        Spacer(1, 10),
                  ]
                  story.append(KeepTogether(chunks))
            for u in unmatched2:
                  members = u["component_indices"]
                  comps = [by2[i] for i in members if i in by2]
                  header = Paragraph(
                        f"<b>Image 2 · Component(s) {members}</b> &nbsp; "
                        f"<font color='#666666'>"
                        f"(no cross-image match — {u.get('reason', 'no_candidate')})"
                        f"</font>",
                        styles["h2"])
                  chunks = [
                        header,
                        _link_block(
                              [], [_component_card(c, styles, layout) for c in comps],
                              "ONLY<br/>IMG 2", TIER_COLORS["unmatched"],
                              styles, layout),
                        Spacer(1, 10),
                  ]
                  story.append(KeepTogether(chunks))

      # ---- OCR appendix ----
      story.append(PageBreak())
      story.append(Paragraph("OCR appendix — raw text per component", styles["h1"]))
      for comps, label in [(comps1, "Image 1"), (comps2, "Image 2")]:
            story.append(Paragraph(label, styles["h2"]))
            for c in comps:
                  head = Paragraph(
                        f"<b>Component {c['component_index']}</b> "
                        f"<font color='#666666'>"
                        f"(rot {c.get('best_rotation', 0)}° + "
                        f"{c.get('best_deskew', 0.0):+.1f}°, "
                        f"PSM {c.get('best_psm', '?')}, "
                        f"{c.get('best_preprocess', '?')}, "
                        f"mean conf {c.get('ocr_confidence', 0.0):.0f})</font>",
                        styles["small"])
                  raw = (c.get("text") or "(no text)")
                  raw = (raw.replace("&", "&amp;")
                         .replace("<", "&lt;")
                         .replace(">", "&gt;"))
                  body = Paragraph(f"<pre>{raw}</pre>", styles["mono"])
                  story.append(KeepTogether([head, body, Spacer(1, 6)]))

      doc.build(story)


# ============================================================
# CLI
# ============================================================

def main():
      parser = argparse.ArgumentParser(
            description="Render the linking PDF report from pipeline JSON.")
      parser.add_argument("--json", default=str(DEFAULT_JSON),
                          help=f"Input JSON path (default: {DEFAULT_JSON})")
      parser.add_argument("--pdf", default=str(DEFAULT_PDF),
                          help=f"Output PDF path (default: {DEFAULT_PDF})")
      parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                          help="Directory containing crops & segmentation PNGs "
                               f"(default: {DEFAULT_OUT_DIR})")
      args = parser.parse_args()

      with open(args.json, "r", encoding="utf-8") as f:
            result = json.load(f)

      build_report(result, args.pdf, out_dir=Path(args.out_dir))
      print(f"Wrote PDF: {args.pdf}")


if __name__ == "__main__":
      main()
