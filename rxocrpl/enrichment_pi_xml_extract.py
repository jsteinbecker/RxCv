#!/usr/bin/env python3
"""
extract_prep.py — Extract preparation / reconstitution / dilution instructions
from an FDA SPL (Structured Product Labeling) XML package insert.

Design goal: reproducibility across *different* package inserts, not just one.
So the extractor anchors on structural + semantic signals rather than on the
exact wording or paragraph positions of any single PI:

  1. Locate the Dosage & Administration section by LOINC code 34068-7
     (falls back to a title regex if the code is absent).
  2. Within it, walk the flattened block stream (paragraphs, list items,
     tables) and detect a "preparation region" that begins at a heading
     matching a preparation cue (Directions / Reconstitution / Preparation /
     Dilution / How Supplied prep, etc.) and is internally segmented by
     sub-headings (Reconstitution, Filtration and Dilution, Storage, ...).
  3. Classify each block as: subheading | numbered_step | caution | note |
     body | table, preserving order and the numbered-step index.

Output: a structured dict -> JSON, suitable for diffing across PIs.

Input may be any of:
  * a DailyMed setId (36-char UUID)  -> downloaded as a zip and unpacked
  * a local .zip containing exactly one .xml
  * a local .xml

Usage:
    python3 extract_prep.py 2b2f3ff5-9d62-4ad2-8eac-1181e5911513 [-o out.json]
    python3 extract_prep.py label.zip --text
    python3 extract_prep.py label.xml
    python3 extract_prep.py --batch setids.txt -o outdir/
"""
from __future__ import annotations
import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, asdict, field
from typing import Optional

URL_PATTERN = "https://dailymed.nlm.nih.gov/dailymed/getFile.cfm?setid={}&type=zip"
USER_AGENT = "spl-prep-extractor/1.0 (+research use)"
SETID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

NS = {"v3": "urn:hl7-org:v3"}
V3 = "{urn:hl7-org:v3}"

DNA_LOINC = "34068-7"  # DOSAGE & ADMINISTRATION
HOWSUPPLIED_LOINC = "34069-5"  # sometimes carries storage/handling

# --- cue vocabularies (case-insensitive, matched on heading-like blocks) ----
PREP_START_CUES = [
      r"directions?\s+for\s+(reconstitution|preparation|use|administration)",
      r"\breconstitution\b",
      r"\bpreparation\b",
      r"reconstitution,?\s+filtration",
      r"preparation\s+of\s+(the\s+)?(solution|infusion|dose|admixture)",
      r"instructions?\s+for\s+(preparation|reconstitution|dilution|use)",
]
SUBHEAD_CUES = [
      r"^reconstitution\b",
      r"^filtration(\s+and\s+dilution)?\b",
      r"^dilution\b",
      r"^storage\b",
      r"^storage\s+of\b",
      r"^stability\b",
      r"^administration\b",
      r"^read\s+this\s+entire\s+section",
      r"^directions?\s+for",
      r"^preparation\b",
      r"^handling\b",
      r"^compatibility\b",
]
# A block that ends the prep region if we hit an unrelated D&A subheading.
STOP_CUES = [
      r"^drug\s+interactions\b",
      r"^dosage\s+in\b",
      r"^recommended\s+dosage\b",
      r"^overdos",
]
CAUTION_CUES = [r"^\s*caution\b", r"^\s*warning\b", r"^\s*do\s+not\b"]
NOTE_CUES = [r"^\s*note\b", r"^\s*important\b"]

STEP_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+(.*)", re.S)


# ---------------------------------------------------------------- loading --
class LoadError(RuntimeError):
      pass


def fetch_zip (setid: str, timeout: int = 60, retries: int = 3,
               cache_dir: Optional[str] = None) -> bytes:
      """Download the DailyMed zip for a setId. Cached to disk if cache_dir given."""
      if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            path = os.path.join(cache_dir, f"{setid}.zip")
            if os.path.exists(path) and os.path.getsize(path) > 0:
                  with open(path, "rb") as f:
                        return f.read()

      url = URL_PATTERN.format(setid)
      last = None
      for attempt in range(retries):
            try:
                  req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                  with urllib.request.urlopen(req, timeout=timeout) as r:
                        data = r.read()
                  if not data:
                        raise LoadError(f"empty response for setId {setid}")
                  if not data.startswith(b"PK"):
                        # DailyMed returns an HTML error page for unknown setIds
                        snippet = data[:200].decode("utf-8", "replace")
                        raise LoadError(f"not a zip for setId {setid}: {snippet!r}")
                  if cache_dir:
                        with open(os.path.join(cache_dir, f"{setid}.zip"), "wb") as f:
                              f.write(data)
                  return data
            except (urllib.error.URLError, TimeoutError) as e:
                  last = e
      raise LoadError(f"download failed for setId {setid}: {last}")


def xml_from_zip (data: bytes, source: str = "<zip>") -> bytes:
      """Return the bytes of the single .xml member of a zip archive.

      The member name varies per label, so we select by extension rather than by
      name. Directory entries, __MACOSX noise, and non-xml assets (jpg images of
      the carton/label, which every SPL package includes) are ignored.
      """
      with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [
                  n for n in z.namelist()
                  if n.lower().endswith(".xml")
                     and not n.endswith("/")
                     and not os.path.basename(n).startswith((".", "._"))
                     and "__MACOSX" not in n
            ]
            if not names:
                  raise LoadError(f"no .xml member in {source} "
                                  f"(members: {z.namelist()[:10]})")
            if len(names) > 1:
                  raise LoadError(f"expected exactly one .xml in {source}, "
                                  f"found {len(names)}: {names}")
            return z.read(names[0])


def load_root (target: str, cache_dir: Optional[str] = None) -> tuple:
      """Resolve a setId / .zip / .xml into (ElementTree root, resolved_label)."""
      if SETID_RE.match(target.strip()):
            setid = target.strip()
            raw = xml_from_zip(fetch_zip(setid, cache_dir=cache_dir),
                               source=f"setId {setid}")
            return ET.fromstring(raw), f"setId:{setid}"

      if not os.path.exists(target):
            raise LoadError(f"{target!r} is neither an existing file nor a valid setId")

      if target.lower().endswith(".zip"):
            with open(target, "rb") as f:
                  raw = xml_from_zip(f.read(), source=target)
            return ET.fromstring(raw), target

      return ET.parse(target).getroot(), target


def _ws (s: str) -> str:
      return re.sub(r"\s+", " ", s or "").strip()


def _text (el: ET.Element) -> str:
      return _ws("".join(el.itertext()))


def _is_bold (el: ET.Element) -> bool:
      """A block reads as a heading if it is (or wholly wraps) bold content."""
      style = (el.get("styleCode") or "")
      if "bold" in style.lower():
            return True
      kids = [c for c in el if c.tag == f"{V3}content"]
      if kids and all("bold" in (c.get("styleCode") or "").lower() for c in kids):
            return True
      # paragraph whose single content child is bold
      contents = el.findall("v3:content", NS)
      if contents and all("bold" in (c.get("styleCode") or "").lower() for c in contents) \
                and not (el.text or "").strip():
            return True
      return False


def _any (patterns, s: str) -> bool:
      return any(re.search(p, s, re.I) for p in patterns)


@dataclass
class Block:
      kind: str  # subheading|numbered_step|caution|note|body|table
      text: str
      step: Optional[int] = None
      rows: Optional[list] = None  # for tables: list[list[str]]
      list_type: Optional[str] = None  # 'ordered'|'unordered' when from <list>
      source_id: Optional[str] = None


@dataclass
class PrepResult:
      setid: Optional[str]
      drug_title: Optional[str]
      effective_time: Optional[str]
      version: Optional[str]
      dna_section_id: Optional[str]
      found: bool
      blocks: list = field(default_factory=list)
      source: Optional[str] = None
      strategy: Optional[str] = None


def _table_rows (tbl: ET.Element) -> list:
      rows = []
      for tr in tbl.iter(f"{V3}tr"):
            cells = [_text(td) for td in tr.findall("v3:td", NS) + tr.findall("v3:th", NS)]
            if cells:
                  rows.append(cells)
      return rows


def _flatten_blocks (section: ET.Element):
      """Yield (element, kind-agnostic text/table) blocks in document order from a
      section's <text>, descending into nested subsections' <text> too."""
      for text_el in section.iter(f"{V3}text"):
            for child in list(text_el):
                  tag = child.tag.split("}")[-1]
                  if tag == "paragraph":
                        yield child
                  elif tag == "table":
                        yield child
                  elif tag == "list":
                        ltype = (child.get("listType") or "unordered").lower()
                        for i, item in enumerate(child.findall("v3:item", NS), 1):
                              # annotate in-place so classifiers can see list context
                              item.set("_listType", ltype)
                              item.set("_ordinal", str(i))
                              yield item


def _classify_stream (container: ET.Element) -> list:
      """Classify every block inside a container element (no region detection).

      Used by the titled-subsection strategy, where the region boundaries are
      already known and we only need per-block typing.
      """
      out: list[Block] = []
      for el in _flatten_blocks(container):
            tag = el.tag.split("}")[-1]
            if tag == "table":
                  out.append(Block("table", "[table]", rows=_table_rows(el),
                                   source_id=el.get("ID")))
                  continue
            raw = _text(el)
            if not raw:
                  continue
            m = STEP_RE.match(raw)
            if m:
                  out.append(Block("numbered_step", _ws(m.group(2)),
                                   step=int(m.group(1)), source_id=el.get("ID")))
            elif _any(CAUTION_CUES, raw):
                  out.append(Block("caution", raw, source_id=el.get("ID")))
            elif _any(NOTE_CUES, raw):
                  out.append(Block("note", raw, source_id=el.get("ID")))
            elif el.get("_ordinal"):
                  # bullet/step from a <list>; SPL's own listType tells us whether the
                  # source asserts an order. We do not invent sequence for unordered.
                  kind = ("numbered_step" if el.get("_listType") == "ordered"
                          else "list_item")
                  out.append(Block(kind, raw, step=int(el.get("_ordinal")),
                                   list_type=el.get("_listType"),
                                   source_id=el.get("ID")))
            elif _is_bold(el) and _any(SUBHEAD_CUES, raw):
                  out.append(Block("subheading", raw, source_id=el.get("ID")))
            else:
                  out.append(Block("body", raw, source_id=el.get("ID")))
      return out


def find_section_by_loinc (root: ET.Element, loinc: str) -> Optional[ET.Element]:
      for sec in root.iter(f"{V3}section"):
            code = sec.find("v3:code", NS)
            if code is not None and code.get("code") == loinc:
                  return sec
      return None


def extract (root: ET.Element) -> PrepResult:
      # --- document metadata ---
      setid_el = root.find("v3:setId", NS)
      title_el = root.find("v3:title", NS)
      eff = root.find("v3:effectiveTime", NS)
      ver = root.find("v3:versionNumber", NS)
      setid = setid_el.get("root") if setid_el is not None else None
      drug_title = _text(title_el) if title_el is not None else None
      effective_time = eff.get("value") if eff is not None else None
      version = ver.get("value") if ver is not None else None

      dna = find_section_by_loinc(root, DNA_LOINC)
      if dna is None:  # title fallback
            for sec in root.iter(f"{V3}section"):
                  t = sec.find("v3:title", NS)
                  if t is not None and re.search(r"dosage\s*&?\s*and?\s*administration", _text(t), re.I):
                        dna = sec
                        break

      result = PrepResult(setid, drug_title, effective_time, version,
                          dna.get("ID") if dna is not None else None, False)
      if dna is None:
            return result

      # --- Strategy A: a *titled* subsection whose title matches a prep cue.
      # This is how PLR-format ("Highlights") labels encode it, e.g.
      # "2.7 Preparation and Administration". Preferred when present because the
      # subsection gives us exact, unambiguous region boundaries.
      prep_subs = []
      for sub in dna.iter(f"{V3}section"):
            t = sub.find("v3:title", NS)
            if t is None:
                  continue
            title_txt = _text(t)
            # strip leading PLR numbering ("2.7 ") before cue matching
            bare = re.sub(r"^[\d.]+\s*", "", title_txt)
            if _any(PREP_START_CUES, bare) and not _any(STOP_CUES, bare):
                  prep_subs.append((sub, title_txt))

      if prep_subs:
            blocks_a: list[Block] = []
            for sub, title_txt in prep_subs:
                  blocks_a.append(Block("subheading", title_txt, source_id=sub.get("ID")))
                  blocks_a.extend(_classify_stream(sub))
            result.found = bool(blocks_a)
            result.blocks = [asdict(b) for b in blocks_a]
            result.strategy = "titled_subsection"
            if result.found:
                  return result

      # --- Strategy B: bold inline heading inside the section's own <text>.
      # Older non-PLR labels (e.g. Amphotericin B) encode prep this way.
      result.strategy = "inline_bold_heading"
      blocks_out: list[Block] = []
      in_prep = False
      for el in _flatten_blocks(dna):
            tag = el.tag.split("}")[-1]
            if tag == "table":
                  if in_prep:
                        blocks_out.append(Block("table", "[table]", rows=_table_rows(el),
                                                source_id=el.get("ID")))
                  continue

            raw = _text(el)
            if not raw:
                  continue
            heading = _is_bold(el)

            if not in_prep:
                  if heading and _any(PREP_START_CUES, raw):
                        in_prep = True
                        blocks_out.append(Block("subheading", raw, source_id=el.get("ID")))
                  continue

            # inside prep region -------------------------------------------------
            if heading and _any(STOP_CUES, raw):
                  break

            m = STEP_RE.match(raw)
            if m:
                  blocks_out.append(Block("numbered_step", _ws(m.group(2)),
                                          step=int(m.group(1)), source_id=el.get("ID")))
            elif _any(CAUTION_CUES, raw):
                  blocks_out.append(Block("caution", raw, source_id=el.get("ID")))
            elif _any(NOTE_CUES, raw):
                  blocks_out.append(Block("note", raw, source_id=el.get("ID")))
            elif el.get("_ordinal"):
                  kind = ("numbered_step" if el.get("_listType") == "ordered"
                          else "list_item")
                  blocks_out.append(Block(kind, raw, step=int(el.get("_ordinal")),
                                          list_type=el.get("_listType"),
                                          source_id=el.get("ID")))
            elif heading and _any(SUBHEAD_CUES, raw):
                  blocks_out.append(Block("subheading", raw, source_id=el.get("ID")))
            else:
                  blocks_out.append(Block("body", raw, source_id=el.get("ID")))

      result.found = bool(blocks_out)
      result.blocks = [asdict(b) for b in blocks_out]
      return result


def to_plaintext (res: PrepResult) -> str:
      lines = [f"# {res.drug_title}", ""]
      for b in res.blocks:
            k = b["kind"]
            if k == "subheading":
                  lines += ["", f"## {b['text']}"]
            elif k == "numbered_step":
                  lines.append(f"{b['step']}. {b['text']}")
            elif k == "list_item":
                  lines.append(f"  - {b['text']}")
            elif k == "caution":
                  lines.append(f"[CAUTION] {b['text']}")
            elif k == "note":
                  lines.append(f"[NOTE] {b['text']}")
            elif k == "table":
                  for row in b["rows"]:
                        lines.append("   | " + " | ".join(row))
            else:
                  lines.append(b["text"])
      return "\n".join(lines)


def extract_one (target: str, cache_dir: Optional[str] = None) -> PrepResult:
      root, label = load_root(target, cache_dir=cache_dir)
      res = extract(root)
      res.source = label
      return res


def main (argv=None):
      ap = argparse.ArgumentParser(
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
      ap.add_argument("input", nargs="?",
                      help="DailyMed setId, .zip, or .xml")
      ap.add_argument("--batch", metavar="FILE",
                      help="file with one setId (or path) per line")
      ap.add_argument("-o", "--out",
                      help="output .json file, or output directory in --batch mode")
      ap.add_argument("--cache", metavar="DIR", default=".spl_cache",
                      help="cache downloaded zips here (default: .spl_cache)")
      ap.add_argument("--text", action="store_true", help="also print plaintext")
      a = ap.parse_args(argv)

      if not a.input and not a.batch:
            ap.error("provide an input, or --batch FILE")

      # ---------------- batch mode ----------------
      if a.batch:
            targets = [ln.strip() for ln in open(a.batch)
                       if ln.strip() and not ln.startswith("#")]
            outdir = a.out or "prep_out"
            os.makedirs(outdir, exist_ok=True)
            ok = fail = 0
            for t in targets:
                  key = t.strip().split("/")[-1].rsplit(".", 1)[0]
                  try:
                        res = extract_one(t, cache_dir=a.cache)
                  except LoadError as e:
                        print(f"[LOAD-FAIL] {key}: {e}", file=sys.stderr)
                        fail += 1
                        continue
                  except ET.ParseError as e:
                        print(f"[XML-FAIL]  {key}: {e}", file=sys.stderr)
                        fail += 1
                        continue
                  with open(os.path.join(outdir, f"{key}.json"), "w") as f:
                        json.dump(asdict(res), f, indent=2, ensure_ascii=False)
                  steps = sum(1 for b in res.blocks
                              if b["kind"] in ("numbered_step", "list_item"))
                  if res.found:
                        ok += 1
                        print(f"[OK] {res.strategy[:6]:6} {key}: {len(res.blocks):>3} blocks, {steps} steps"
                              f" — {(res.drug_title or '')[:50]}")
                  else:
                        fail += 1
                        print(f"[NO-PREP]   {key}: no preparation region found"
                              f" — {(res.drug_title or '')[:50]}", file=sys.stderr)
            print(f"\n{ok} extracted, {fail} failed, {len(targets)} total",
                  file=sys.stderr)
            return 0 if fail == 0 else 1

      # ---------------- single mode ----------------
      try:
            res = extract_one(a.input, cache_dir=a.cache)
      except LoadError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

      js = json.dumps(asdict(res), indent=2, ensure_ascii=False)
      if a.out:
            with open(a.out, "w") as f:
                  f.write(js)
      else:
            print(js)
      if a.text:
            print("\n" + "=" * 60 + "\n", file=sys.stderr)
            print(to_plaintext(res), file=sys.stderr)
      return 0 if res.found else 1


if __name__ == "__main__":
      raise SystemExit(main())
