"""
spl_extract.py — Extract preparation/reconstitution and storage/handling
information from an FDA SPL (Structured Product Labeling) HL7 v3 XML file.

Design
------
SPL is HL7 v3. Every <section> carries a <code> from LOINC
(codeSystem 2.16.840.1.113883.6.1). Those codes are stable controlled
vocabulary; the <title> text is not. So we anchor extraction on LOINC
codes and walk the nested <component><section> tree, rather than matching
title strings.

Two complications this handles:

1. Storage instructions are frequently NOT in a dedicated STORAGE & HANDLING
   section. They appear inside DOSAGE & ADMINISTRATION subsections that are
   coded only as "SPL UNCLASSIFIED SECTION" (42229-5). So after pulling
   whole sections by code, we run a keyword classifier over each rendered
   paragraph/list to tag storage- vs prep-relevant content even when it is
   buried in a generic subsection.

2. The SPL text model (<paragraph>, <list>/<item>, <content>, <linkHtml>,
   <sup>, <sub>, <br>, <table>, <caption>) must be flattened to readable
   plain text while preserving list nesting. render_text() does that.

Usage
-----
    from spl_extract import extract_from_file
    result = extract_from_file("zusduri.xml")
    print(result.to_markdown())

    # or programmatically
    for blk in result.preparation:
        print(blk.title, blk.text)

    # from the command line
    python spl_extract.py <spl.xml>     # extract and print markdown
    python spl_extract.py --help        # show help
    python spl_extract.py               # interactive guided entry

Stdlib only (xml.etree). No external deps.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

V3 = "urn:hl7-org:v3"
NS = {"v3": V3}
LOINC = "2.16.840.1.113883.6.1"


def _t(tag: str) -> str:
      """Namespaced tag for the default HL7 v3 namespace."""
      return f"{{{V3}}}{tag}"


# ---------------------------------------------------------------------------
# LOINC section codes of interest
# ---------------------------------------------------------------------------

# Sections whose entire body is about preparation / reconstitution / admin.
PREP_SECTION_CODES = {
      "34068-7": "Dosage & Administration",
      "59845-8": "Instructions for Use",  # IFP (pharmacy) + IFA (administration)
}

# Sections that are (or contain) storage & handling.
STORAGE_SECTION_CODES = {
      "44425-7": "Storage and Handling",
      "34069-5": "How Supplied / Storage and Handling",
}

# Generic / unclassified wrapper codes — used to know a subsection is
# *not* self-describing, so we must classify its text by keyword.
UNCLASSIFIED_CODES = {"42229-5"}

# Keyword classifiers (case-insensitive) for paragraph-level tagging.
STORAGE_PATTERNS = re.compile(
      r"\b(store|storage|refriger|°c|°f|room temperature|"
      r"protect(ed)? from light|discard|excursion|controlled room|"
      r"do not freeze|shelf life|after reconstitution.*(store|discard)|"
      r"chill|chilled|ice bath|freezer|stable for|beyond[- ]use|bud)\b",
      re.IGNORECASE,
)
PREP_PATTERNS = re.compile(
      r"\b(reconstitut|prepar|admixture|swirl|inject|withdraw|"
      r"dilut|mix|vial adaptor|syringe adaptor|sterile water|"
      r"hydrogel|instill|cstd|aseptic|hood|isolator)\b",
      re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Block:
      """A rendered chunk of section text with provenance."""
      loinc: str
      section_title: str
      text: str
      tags: set = field(default_factory=set)  # {"storage"}, {"preparation"}, ...

      def __repr__(self) -> str:
            head = self.text.strip().splitlines()[0][:60] if self.text.strip() else ""
            return f"<Block {self.loinc} {sorted(self.tags)} {head!r}>"


@dataclass
class ExtractResult:
      product: str
      set_id: str
      version: str
      effective_time: str
      blocks: list[Block] = field(default_factory=list)  # canonical, deduped

      @property
      def preparation(self) -> list[Block]:
            return [b for b in self.blocks if "preparation" in b.tags]

      @property
      def storage(self) -> list[Block]:
            return [b for b in self.blocks if "storage" in b.tags]

      def to_markdown(self) -> str:
            out = [f"# {self.product or '(product name not found)'}",
                   f"_setId {self.set_id} · v{self.version} · {self.effective_time}_",
                   "", "## Preparation / Reconstitution", ""]
            for b in self.preparation:
                  also = " · also: storage" if "storage" in b.tags else ""
                  out.append(f"### {b.section_title}  `({b.loinc})`{also}")
                  out.append(b.text.rstrip())
                  out.append("")
            out += ["## Storage & Handling", ""]
            for b in self.storage:
                  also = " · also: preparation" if "preparation" in b.tags else ""
                  out.append(f"### {b.section_title}  `({b.loinc})`{also}")
                  out.append(b.text.rstrip())
                  out.append("")
            return "\n".join(out)


# ---------------------------------------------------------------------------
# SPL text-model rendering
# ---------------------------------------------------------------------------

def inline_text(node: ET.Element) -> str:
      """Collect all text from a node and its inline children, including the
      text that follows child elements (.tail). Used for titles and inline runs."""
      buf = [node.text or ""]
      for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "br":
                  buf.append("\n")
            else:
                  buf.append(inline_text(child))
            buf.append(child.tail or "")
      return "".join(buf)


def render_text(elem: ET.Element, indent: int = 0) -> str:
      """
      Flatten an SPL <text>/<paragraph>/<list> subtree to readable plain text,
      preserving list nesting. Inline elements (content, linkHtml, sup, sub)
      are unwrapped; <br> becomes a newline; tables become tab-separated rows.
      """
      parts: list[str] = []
      inline = inline_text

      def walk(node: ET.Element, depth: int):
            tag = node.tag.split("}")[-1]

            if tag in ("text", "excerpt", "highlight"):
                  for child in node:
                        walk(child, depth)

            elif tag == "paragraph":
                  txt = inline(node).strip()
                  if txt:
                        parts.append("    " * depth + txt)

            elif tag == "list":
                  for i, item in enumerate(node.findall(_t("item")), 1):
                        cap = item.find(_t("caption"))
                        marker = (cap.text.strip() if cap is not None and cap.text
                                  else "•")
                        bits = [item.text or ""]  # text before first child
                        nested = []
                        for c in item:
                              ct = c.tag.split("}")[-1]
                              if ct == "list":
                                    nested.append(c)
                                    bits.append(c.tail or "")  # text after nested list
                              elif ct == "caption":
                                    bits.append(c.tail or "")  # text after the marker
                              else:
                                    bits.append(inline(c))
                                    bits.append(c.tail or "")
                        line = " ".join("".join(bits).split())
                        if line:
                              parts.append("    " * depth + f"{marker} {line}")
                        for n in nested:
                              walk(n, depth + 1)

            elif tag == "table":
                  cap = node.find(_t("caption"))
                  if cap is not None:
                        parts.append("    " * depth + "[Table] " + inline(cap).strip())
                  for tr in node.iter(_t("tr")):
                        cells = []
                        for cell in tr:
                              if cell.tag.split("}")[-1] in ("td", "th"):
                                    cells.append(inline(cell).strip().replace("\n", " "))
                        if any(cells):
                              parts.append("    " * depth + "  ".join(c for c in cells if c))

            elif tag in ("content", "renderMultiMedia", "br"):
                  txt = inline(node).strip()
                  if txt:
                        parts.append("    " * depth + txt)

            else:
                  for child in node:
                        walk(child, depth)

      walk(elem, indent)
      return "\n".join(parts)


# ---------------------------------------------------------------------------
# Section tree walking
# ---------------------------------------------------------------------------

def _section_code(section: ET.Element) -> str:
      code = section.find(_t("code"))
      return code.get("code", "") if code is not None else ""


def _section_title(section: ET.Element) -> str:
      title = section.find(_t("title"))
      if title is not None:
            return " ".join(inline_text(title).split()) or "(untitled)"
      return "(untitled)"


def _own_text_element(section: ET.Element) -> ET.Element | None:
      """The <text> that belongs to THIS section, not a child <component>."""
      return section.find(_t("text"))


def iter_sections(root: ET.Element):
      """Yield every <section> in the structuredBody, depth-first."""
      body = root.find(f".//{_t('structuredBody')}")
      if body is None:
            return
      for comp in body.findall(_t("component")):
            sec = comp.find(_t("section"))
            if sec is not None:
                  yield from _descend(sec)


def _descend(section: ET.Element):
      yield section
      for comp in section.findall(_t("component")):
            child = comp.find(_t("section"))
            if child is not None:
                  yield from _descend(child)


def _nearest_titled_ancestor_title(section: ET.Element,
                                   parent_map: dict) -> str:
      """Walk up until we find a section that has its own non-empty <title>."""
      cur = section
      while cur is not None:
            t = cur.find(_t("title"))
            if t is not None and inline_text(t).strip():
                  return " ".join(inline_text(t).split())
            cur = parent_map.get(cur)
      return "(untitled)"


# ---------------------------------------------------------------------------
# Top-level extraction
# ---------------------------------------------------------------------------

def extract(root: ET.Element) -> ExtractResult:
      # ---- metadata ----
      def first(path) -> ET.Element | None:
            el = root.find(path)
            return el if el is not None else None

      set_id_el = first(_t("setId"))
      version_el = first(_t("versionNumber"))
      eff_el = first(_t("effectiveTime"))

      # `.get(...)` may return None even when the element exists; normalize to str.
      set_id = (set_id_el.get("root") or "") if set_id_el is not None else ""
      version = (version_el.get("value") or "") if version_el is not None else ""
      eff = (eff_el.get("value") or "") if eff_el is not None else ""

      product = ""
      mp = root.find(f".//{_t('manufacturedProduct')}")
      if mp is not None:
            nm = mp.find(_t("name"))
            if nm is None:
                  inner = mp.find(_t("manufacturedProduct"))
                  nm = inner.find(_t("name")) if inner is not None else None
            if nm is not None and nm.text:
                  product = nm.text.strip()
      if not product:
            for nm in root.iter(_t("name")):
                  if nm.text and nm.text.strip():
                        product = nm.text.strip()
                        break

      result = ExtractResult(product=product, set_id=set_id,
                             version=version, effective_time=eff)

      # parent map so we can attribute untitled subsections to their parent
      parent_map = {c: p for p in root.iter() for c in p}

      seen_texts: set[str] = set()

      for sec in iter_sections(root):
            code = _section_code(sec)
            own = _own_text_element(sec)
            if own is None:
                  continue
            rendered = render_text(own).strip()
            if not rendered or rendered in seen_texts:
                  continue

            title = _nearest_titled_ancestor_title(sec, parent_map)

            in_prep_tree = _in_code_tree(sec, parent_map, PREP_SECTION_CODES)
            in_storage_tree = _in_code_tree(sec, parent_map, STORAGE_SECTION_CODES)

            is_storage = bool(STORAGE_PATTERNS.search(rendered)) or in_storage_tree
            is_prep = bool(PREP_PATTERNS.search(rendered)) or in_prep_tree

            # Only keep blocks relevant to either category.
            if not (is_storage or is_prep):
                  continue

            seen_texts.add(rendered)
            tags = set()
            if is_storage:
                  tags.add("storage")
            if is_prep:
                  tags.add("preparation")

            block = Block(loinc=code or "(none)", section_title=title,
                          text=rendered, tags=tags)
            result.blocks.append(block)

      return result


def _in_code_tree(section: ET.Element, parent_map: dict, codes: dict) -> bool:
      """True if this section or any ancestor section carries one of `codes`."""
      cur = section
      while cur is not None:
            if cur.tag == _t("section") and _section_code(cur) in codes:
                  return True
            cur = parent_map.get(cur)
      return False


def extract_from_file(path: str) -> ExtractResult:
      tree = ET.parse(path)
      return extract(tree.getroot())


def extract_from_string(xml: str) -> ExtractResult:
      return extract(ET.fromstring(xml))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

PROG = os.path.basename(sys.argv[0]) or "spl_extract.py"

HELP_TEXT = f"""\
{PROG} — extract preparation/reconstitution and storage/handling
information from an FDA SPL (Structured Product Labeling) HL7 v3 XML file.

USAGE
    python {PROG} <spl.xml>          Extract from an SPL file, print Markdown
    python {PROG} [options]          See options below
    python {PROG}                    No args: launch the interactive guide

OPTIONS
    -h, --help                       Show this help and exit
    -r, --repr                       Print a Block repr summary instead of full
                                     Markdown (quick provenance inspection)
    --prep-only                      Print only Preparation / Reconstitution
    --storage-only                   Print only Storage & Handling
    --                               Treat the next argument as a filename even
                                     if it begins with a dash

WHAT IT DOES
    Anchors extraction on stable LOINC section codes (not title strings) and
    walks the nested <component><section> tree. Storage text buried inside
    generic "SPL UNCLASSIFIED SECTION" (42229-5) subsections is recovered via
    keyword classification, so instructions that live under Dosage &
    Administration are still tagged correctly.

    Recognized section codes:
      Preparation : {", ".join(f"{k} ({v})" for k, v in PREP_SECTION_CODES.items())}
      Storage     : {", ".join(f"{k} ({v})" for k, v in STORAGE_SECTION_CODES.items())}

EXAMPLES
    python {PROG} zusduri.xml
    python {PROG} --storage-only zusduri.xml
    python {PROG} -r zusduri.xml

LIBRARY USE
    from spl_extract import extract_from_file
    result = extract_from_file("zusduri.xml")
    print(result.to_markdown())
    for blk in result.preparation:
        print(blk.section_title, blk.text)
"""


def print_help() -> None:
      print(HELP_TEXT)


def _render(result: ExtractResult, mode: str) -> str:
      """Render an ExtractResult for the CLI according to `mode`."""
      if mode == "repr":
            lines = [f"{result.product or '(no product)'}  "
                     f"setId={result.set_id} v{result.version} {result.effective_time}",
                     f"{len(result.blocks)} block(s):"]
            lines += [f"  {b!r}" for b in result.blocks]
            return "\n".join(lines)

      if mode == "prep-only":
            md = result.to_markdown()
            # keep header + Preparation section only
            head, _, rest = md.partition("## Storage & Handling")
            return head.rstrip()

      if mode == "storage-only":
            md = result.to_markdown()
            prep_marker = "## Preparation / Reconstitution"
            storage_marker = "## Storage & Handling"
            header = md.split(prep_marker, 1)[0].rstrip()
            storage = storage_marker + md.split(storage_marker, 1)[1]
            return header + "\n\n" + storage

      return result.to_markdown()


def run_file(path: str, mode: str = "markdown") -> int:
      """Extract from a file and print. Returns a process exit code."""
      if not os.path.exists(path):
            print(f"error: file not found: {path}", file=sys.stderr)
            return 1
      try:
            result = extract_from_file(path)
      except ET.ParseError as e:
            print(f"error: could not parse XML in {path!r}: {e}", file=sys.stderr)
            return 1
      except OSError as e:
            print(f"error: could not read {path!r}: {e}", file=sys.stderr)
            return 1
      print(_render(result, mode))
      return 0


# ---------------------------------------------------------------------------
# Interactive guided entry (run with no params)
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str | None = None) -> str:
      """Prompt the user, returning their answer (or `default` on empty input).
      Treats EOF / Ctrl-D as an empty answer so piped/non-tty runs don't crash."""
      suffix = f" [{default}]" if default else ""
      try:
            ans = input(f"{prompt}{suffix}: ").strip()
      except EOFError:
            print()
            return default or ""
      return ans or (default or "")


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
      d = "Y/n" if default else "y/N"
      while True:
            ans = _ask(f"{prompt} ({d})").lower()
            if not ans:
                  return default
            if ans in ("y", "yes"):
                  return True
            if ans in ("n", "no"):
                  return False
            print("  please answer y or n.")


def _ask_choice(prompt: str, choices: list[str], default_idx: int = 0) -> int:
      """Present a numbered menu; return the chosen 0-based index."""
      print(prompt)
      for i, c in enumerate(choices, 1):
            marker = " (default)" if i - 1 == default_idx else ""
            print(f"  {i}) {c}{marker}")
      while True:
            ans = _ask("Choose a number", str(default_idx + 1))
            if ans.isdigit() and 1 <= int(ans) <= len(choices):
                  return int(ans) - 1
            print(f"  enter a number from 1 to {len(choices)}.")


def interactive() -> int:
      """
      Step-by-step guide shown when the script is run with no arguments.
      Walks the user through locating an SPL file and choosing output options,
      then runs the extraction. Falls back to printing help if the user opts
      out. Returns a process exit code.
      """
      print("=" * 68)
      print(f"  {PROG} — interactive guide")
      print("=" * 68)
      print()
      print("No arguments given, so let's set up an extraction step by step.")
      print("Press Enter to accept the [default] shown in brackets at each step.")
      print("(Run  python", PROG, "--help  to see the non-interactive usage.)")
      print()

      # Non-interactive environments (no tty): show help instead of hanging.
      if not sys.stdin or not sys.stdin.isatty():
            print("stdin is not interactive; showing help instead.\n")
            print_help()
            return 0

      # --- Step 1: locate the SPL file ---
      print("Step 1 of 3 — Locate the SPL XML file")
      print("-" * 68)
      xmls = sorted(f for f in os.listdir(".") if f.lower().endswith(".xml"))
      if xmls:
            print("XML files found in the current directory:")
            for f in xmls:
                  print(f"  • {f}")
            print()
      else:
            print("No .xml files found in the current directory.")
            print()

      path = ""
      while True:
            default = xmls[0] if xmls else None
            path = _ask("Path to the SPL XML file (blank to cancel)", default)
            if not path:
                  print("\nNo file chosen. Showing help so you can run it directly.\n")
                  print_help()
                  return 0
            if os.path.exists(path):
                  break
            print(f"  '{path}' not found — try again, or leave blank to cancel.")
      print()

      # --- Step 2: choose what to extract ---
      print("Step 2 of 3 — Choose what to show")
      print("-" * 68)
      mode_idx = _ask_choice(
            "Which sections do you want in the output?",
            ["Both preparation and storage (full Markdown)",
             "Preparation / reconstitution only",
             "Storage & handling only",
             "Block summary (repr — quick provenance check)"],
            default_idx=0,
      )
      mode = ["markdown", "prep-only", "storage-only", "repr"][mode_idx]
      print()

      # --- Step 3: optional save to file ---
      print("Step 3 of 3 — Output destination")
      print("-" * 68)
      save = _ask_yes_no("Save the output to a file as well as printing it?",
                         default=False)
      out_path = ""
      if save:
            stem = os.path.splitext(os.path.basename(path))[0]
            default_out = f"{stem}_extract.md"
            out_path = _ask("Output filename", default_out)
      print()

      # --- Run ---
      print("Running extraction…")
      print("=" * 68)
      try:
            result = extract_from_file(path)
      except ET.ParseError as e:
            print(f"error: could not parse XML in {path!r}: {e}", file=sys.stderr)
            return 1
      except OSError as e:
            print(f"error: could not read {path!r}: {e}", file=sys.stderr)
            return 1

      rendered = _render(result, mode)
      print(rendered)

      if save and out_path:
            try:
                  with open(out_path, "w", encoding="utf-8") as fh:
                        fh.write(rendered + "\n")
                  print("\n" + "=" * 68)
                  print(f"Saved to {out_path}")
            except OSError as e:
                  print(f"\nwarning: could not write {out_path!r}: {e}",
                        file=sys.stderr)

      # brief summary
      print("\n" + "-" * 68)
      print(f"Done. {len(result.preparation)} preparation block(s), "
            f"{len(result.storage)} storage block(s).")
      return 0


def main(argv: list[str]) -> int:
      args = argv[1:]

      # No arguments → interactive guided entry.
      if not args:
            return interactive()

      mode = "markdown"
      path: str | None = None
      i = 0
      while i < len(args):
            a = args[i]
            if a in ("-h", "--help"):
                  print_help()
                  return 0
            elif a in ("-r", "--repr"):
                  mode = "repr"
            elif a == "--prep-only":
                  mode = "prep-only"
            elif a == "--storage-only":
                  mode = "storage-only"
            elif a == "--":
                  i += 1
                  if i < len(args):
                        path = args[i]
                  break
            elif a.startswith("-"):
                  print(f"error: unknown option {a!r}", file=sys.stderr)
                  print(f"try 'python {PROG} --help'", file=sys.stderr)
                  return 2
            else:
                  path = a
            i += 1

      if path is None:
            print("error: no input file given", file=sys.stderr)
            print(f"try 'python {PROG} --help', or run with no arguments "
                  "for the interactive guide.", file=sys.stderr)
            return 2

      return run_file(path, mode)


if __name__ == "__main__":
      sys.exit(main(sys.argv))
