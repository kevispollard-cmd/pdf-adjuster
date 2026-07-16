"""
PDF Adjuster engine — internal tool for a P&C insurance agency.

Three operations on policy-doc packets (Invoice / EOI or Declarations / RCE):
  1. Effective-date change (printed date + effective + term-preserved expiration)
  2. Merge separate PDFs in fixed order with borrower-based file naming
  3. Mortgagee clause + loan number replacement (1st loan only), "revised" naming

Technique: locate exact text spans with PyMuPDF, redact, overlay new text
matching size/color/style. Nothing is written without the caller previewing.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime

import fitz  # PyMuPDF
from dateutil.relativedelta import relativedelta

# ---------------------------------------------------------------- constants

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")

DATE_NUM_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
DATE_LONG_RE = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s+(\d{4})\b")

NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}

# Page-kind detection (order matters: first match wins)
KIND_PATTERNS = [
    ("invoice", re.compile(r"\bINVOICE\b", re.I)),
    ("eoi", re.compile(r"EVIDENCE OF PROPERTY INSURANCE", re.I)),
    ("dec", re.compile(r"Declarations", re.I)),
    ("rce", re.compile(r"\bRCE\b|Cost Data As Of|Replacement Cost Estimate", re.I)),
]
KIND_RANK = {"invoice": 0, "eoi": 1, "dec": 1, "rce": 2, "other": 3}


# ---------------------------------------------------------------- helpers

def parse_num_date(s: str) -> date | None:
    m = DATE_NUM_RE.search(s)
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_long_date(s: str) -> date | None:
    m = DATE_LONG_RE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)))
    except ValueError:
        return None


def fmt_num(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def fmt_long(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def int_color(c: int) -> tuple[float, float, float]:
    return ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)


def map_font(fontname: str, flags: int) -> str:
    """Map an embedded font to the closest PDF base-14 font."""
    fn = fontname.lower()
    serif = "times" in fn or "georgia" in fn or "garamond" in fn or (flags & 4)
    bold = "bold" in fn or (flags & 16)
    italic = "italic" in fn or "oblique" in fn or (flags & 2)
    if "courier" in fn or "mono" in fn:
        base = "cour"
    elif serif:
        base = "ti"
    else:
        base = "he"
    if base == "cour":
        return "cour" + ("bo" if bold and not italic else
                         "it" if italic and not bold else
                         "bi" if bold and italic else "")
    suffix = ("bi" if bold and italic else "bo" if bold else
              "it" if italic else ("ro" if base == "ti" else "lv"))
    return base + suffix


def iter_spans(page: fitz.Page):
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                yield span


def find_spans(page: fitz.Page, needle: str, case_sensitive: bool = True):
    out = []
    for span in iter_spans(page):
        hay = span["text"] if case_sensitive else span["text"].lower()
        ndl = needle if case_sensitive else needle.lower()
        if ndl in hay:
            out.append(span)
    return out


def span_style(span) -> dict:
    return {
        "size": span["size"],
        "font": map_font(span["font"], span.get("flags", 0)),
        "color": int_color(span.get("color", 0)),
    }


# ------------------------------------------------------- font resolution

import os
from pathlib import Path

FONT_DIR = Path(os.environ.get("PA_FONT_DIR", "") or Path(__file__).parent / "fonts")

# family key -> bundled file stems per (bold, italic)
_BUNDLED = {
    "librefranklin": "LibreFranklin",
    "franklin": "LibreFranklin",
    "calibri": "Carlito",          # metric-compatible
    "carlito": "Carlito",
    "candara": "Carlito",
    "times": "LiberationSerif",    # metric-compatible
    "georgia": "LiberationSerif",
    "garamond": "LiberationSerif",
    "arial": "LiberationSans",     # metric-compatible
    "helvetica": "LiberationSans",
    "segoe": "LiberationSans",
    "verdana": "LiberationSans",
    "tahoma": "LiberationSans",
}
_STYLE_SUFFIX = {(False, False): "Regular", (True, False): "Bold",
                 (False, True): "Italic", (True, True): "BoldItalic"}

_font_cache: dict[str, fitz.Font] = {}


def _span_is_bold(fontname: str, flags: int) -> bool:
    return "bold" in fontname.lower() or bool(flags & 16)


def _span_is_italic(fontname: str, flags: int) -> bool:
    fn = fontname.lower()
    return "italic" in fn or "oblique" in fn or bool(flags & 2)


def resolve_font(fontname: str, flags: int) -> tuple[str, str | None]:
    """Return (alias_or_base14_name, fontfile_path_or_None) that best matches
    the document's font. Bundled real/metric-compatible fonts first."""
    bold = _span_is_bold(fontname, flags)
    italic = _span_is_italic(fontname, flags)
    fn = fontname.lower()
    family = None
    for key, stem in _BUNDLED.items():
        if key in fn:
            family = stem
            break
    if family is None:
        # generic: serif flag (bit 2) or serif-looking name -> serif clone
        family = "LiberationSerif" if (flags & 4) else "LiberationSans"
    path = FONT_DIR / f"{family}-{_STYLE_SUFFIX[(bold, italic)]}.ttf"
    if path.exists():
        alias = f"pa-{path.stem}".replace(".", "-")
        return alias, str(path)
    return map_font(fontname, flags), None  # base-14 fallback


def _measure(text: str, size: float, alias: str, fontfile: str | None) -> float:
    if fontfile:
        if fontfile not in _font_cache:
            _font_cache[fontfile] = fitz.Font(fontfile=fontfile)
        return _font_cache[fontfile].text_length(text, fontsize=size)
    return fitz.get_text_length(text, alias, size)


# ------------------------------------------------------- replacement engine
#
# Line-aware model: a replacement redraws the matched span with the new text
# using the closest real font; if the width changes, spans to the RIGHT on the
# same visual line are redrawn shifted so spacing stays true. Spans to the
# left and other lines are never touched.

@dataclass
class Replacement:
    page: int
    rect: tuple                 # area being redacted
    new_text: str
    size: float
    font: str                   # alias or base-14 name
    color: tuple
    old_text: str = ""
    origin: tuple | None = None  # baseline point for insertion
    fontfile: str | None = None


def _plan_pairs(doc: fitz.Document, pairs: list[tuple[str, str]],
                exclude_rects: dict[int, list[fitz.Rect]] | None = None,
                pages: list[int] | None = None) -> list[Replacement]:
    """Plan replacing every visual occurrence of each (old, new) pair.
    All pairs are handled in ONE pass per line so that multiple targets on the
    same visual line compose their width shifts instead of conflicting."""
    plans: list[Replacement] = []
    seen: set[str] = set()
    clean: list[tuple[str, str]] = []
    for o, n in pairs:
        if o and o != n and o not in seen:
            seen.add(o)
            clean.append((o, n))
    if not clean:
        return plans
    for pno in (pages if pages is not None else range(doc.page_count)):
        page = doc[pno]
        active = [(o, n) for o, n in clean if page.search_for(o)]
        if not active:
            continue
        d = page.get_text("dict")
        for block in d["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                spans = line["spans"]
                if not any(o in s["text"] for s in spans for o, _ in active):
                    continue
                shift = 0.0
                for s in spans:
                    srect = fitz.Rect(s["bbox"])
                    if exclude_rects and any(
                            srect.intersects(x) for x in exclude_rects.get(pno, [])):
                        continue
                    alias, ffile = resolve_font(s["font"], s.get("flags", 0))
                    size = s["size"]
                    color = int_color(s.get("color", 0))
                    ox, oy = s.get("origin", (srect.x0, srect.y1 - size * 0.22))
                    new_span = s["text"]
                    for o, n in active:
                        if o in new_span:
                            new_span = new_span.replace(o, n)
                    if new_span != s["text"]:
                        plans.append(Replacement(pno, tuple(srect), new_span.rstrip(),
                                                 size, alias, color, s["text"],
                                                 (ox + shift, oy), ffile))
                        shift += (_measure(new_span.rstrip(), size, alias, ffile)
                                  - _measure(s["text"].rstrip(), size, alias, ffile))
                    elif abs(shift) > 0.4:
                        # untouched span right of a width change: redraw shifted
                        plans.append(Replacement(pno, tuple(srect), s["text"].rstrip(),
                                                 size, alias, color, s["text"],
                                                 (ox + shift, oy), ffile))
        # --- fallback: occurrences that wrap across lines (e.g. table cells) ---
        covered = [fitz.Rect(p.rect) for p in plans if p.page == pno]
        for o, n in active:
            leftovers = [r for r in page.search_for(o)
                         if not any(r.intersects(c) for c in covered)
                         and not (exclude_rects and any(
                             r.intersects(x) for x in exclude_rects.get(pno, [])))]
            for group in _group_wrapped_hits(leftovers):
                first = group[0]
                size, alias, ffile, color = 8.0, "helv", None, (0, 0, 0)
                for s in iter_spans(page):
                    if fitz.Rect(s["bbox"]).intersects(first):
                        size = s["size"]
                        color = int_color(s.get("color", 0))
                        alias, ffile = resolve_font(s["font"], s.get("flags", 0))
                        break
                max_w = max(r.x1 - r.x0 for r in group) + 4
                lines = _wrap_text(n, max_w, size, alias, ffile)
                lead = size * 1.22
                for j, r in enumerate(group):       # redact every segment
                    plans.append(Replacement(pno, tuple(r),
                                             lines[j] if j < len(lines) else "",
                                             size, alias, color, o,
                                             (r.x0, r.y1 - size * 0.22), ffile))
                    covered.append(fitz.Rect(r))
                for j in range(len(group), len(lines)):  # extra wrapped lines
                    base = group[-1]
                    y = base.y1 + lead * (j - len(group) + 1)
                    plans.append(Replacement(pno, (base.x0, y - size, base.x1, y),
                                             lines[j], size, alias, color, "",
                                             (base.x0, y - size * 0.22), ffile))
    return plans


def _plan_string_replacements(doc: fitz.Document, old: str, new: str,
                              exclude_rects: dict[int, list[fitz.Rect]] | None = None,
                              pages: list[int] | None = None) -> list[Replacement]:
    """Plan replacing every visual occurrence of `old` with `new`."""
    return _plan_pairs(doc, [(old, new)], exclude_rects, pages)


def _group_wrapped_hits(rects: list[fitz.Rect]) -> list[list[fitz.Rect]]:
    """Group search_for segment rects that belong to one line-wrapped hit."""
    groups: list[list[fitz.Rect]] = []
    for r in rects:
        if groups:
            prev = groups[-1][-1]
            h = prev.y1 - prev.y0
            if 0 <= r.y0 - prev.y1 < h * 1.2 and r.x0 < prev.x1:
                groups[-1].append(r)
                continue
        groups.append([r])
    return groups


def _wrap_text(text: str, max_w: float, size: float,
               alias: str, ffile: str | None) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if cur and _measure(cand, size, alias, ffile) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines or [""]


def _apply_replacements(doc: fitz.Document, plans: list[Replacement]) -> None:
    by_page: dict[int, list[Replacement]] = {}
    for p in plans:
        by_page.setdefault(p.page, []).append(p)
    for pno, items in by_page.items():
        page = doc[pno]
        for it in items:
            page.add_redact_annot(fitz.Rect(it.rect))
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        except TypeError:  # older PyMuPDF without graphics kwarg
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        for it in items:
            r = fitz.Rect(it.rect)
            size = it.size
            # shrink slightly if the new text is far wider than the old area
            avail = (r.x1 - r.x0) * 1.6 + 14
            while size > 4 and _measure(it.new_text, size, it.font, it.fontfile) > avail:
                size -= 0.25
            point = fitz.Point(it.origin) if it.origin else fitz.Point(r.x0, r.y1 - size * 0.22)
            kwargs = dict(fontname=it.font, fontsize=size, color=it.color)
            if it.fontfile:
                kwargs["fontfile"] = it.fontfile
            page.insert_text(point, it.new_text, **kwargs)


# ---------------------------------------------------------------- analysis

@dataclass
class Analysis:
    template: str = "unknown"           # mercury-acord | openly | unknown
    page_kinds: list = field(default_factory=list)
    printed_date: str | None = None     # M/D/YYYY as shown
    effective_date: str | None = None
    expiration_date: str | None = None
    term_months: int | None = None
    insureds: list = field(default_factory=list)
    mortgagee: list = field(default_factory=list)   # lines of 1st-loan mortgagee block
    loan_number: str | None = None
    second_mortgagee: list = field(default_factory=list)
    second_loan_number: str | None = None
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def classify_page(text: str) -> str:
    head = text[:2500]
    for kind, pat in KIND_PATTERNS:
        if pat.search(head):
            return kind
    return "other"


def _value_near_label(page: fitz.Page, label: str, pattern: re.Pattern,
                      max_dist: float = 120) -> tuple[str | None, fitz.Rect | None]:
    """Find the pattern-matching span nearest (below/right of) a label span."""
    hits = page.search_for(label)
    if not hits:
        return None, None
    lrect = hits[0]
    best, best_d, best_r = None, 1e9, None
    for s in iter_spans(page):
        m = pattern.search(s["text"])
        if not m:
            continue
        srect = fitz.Rect(s["bbox"])
        if srect.y1 < lrect.y0 - 2:        # above the label -> ignore
            continue
        d = abs(srect.y0 - lrect.y0) * 2 + abs(srect.x0 - lrect.x0)
        if d < best_d and d < max_dist * 3:
            best, best_d, best_r = m.group(0), d, srect
    return best, best_r


def _block_lines_after_label(page: fitz.Page, label: str, max_lines: int = 6,
                             stop_words=("Please include", "MORTGAGEE", "LOSS PAYEE",
                                         "CANCELLATION", "Overnight")) -> list[str]:
    """Collect the text lines making up the address block following a label."""
    labels = find_spans(page, label)
    if not labels:
        return []
    lrect = fitz.Rect(labels[0]["bbox"])
    # gather span-lines strictly below label, roughly same left edge, ordered by y
    lines: list[tuple[float, float, str]] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            r = fitz.Rect(line["bbox"])
            if r.y0 > lrect.y1 - 2 and abs(r.x0 - lrect.x0) < 60 and r.y0 - lrect.y1 < 90:
                lines.append((r.y0, r.x0, text))
    lines.sort()
    out = []
    prev_y = lrect.y1
    for y, x, text in lines:
        if any(text.startswith(w) or w in text for w in stop_words):
            break
        if y - prev_y > 16:  # big vertical gap ends the block
            break
        out.append(text)
        prev_y = y
        if len(out) >= max_lines:
            break
    return out


def _second_mortgage_zones(doc: fitz.Document) -> dict[int, list[fitz.Rect]]:
    """Rects around 2nd-mortgage areas — replacements must never touch these.
    Two layout families: ACORD's '2nd Mortgage (if applicable):' block, and
    binder-style '2nd Mortgagee:' column headings. A bare 'Loan Number:' label
    elsewhere is NOT protected (binders label the 1st loan the same way)."""
    zones: dict[int, list[fitz.Rect]] = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        for label, w, h in (("2nd Mortgage (if applicable):", 300, 95),
                            ("2nd Mortgagee:", 280, 120)):
            for r in page.search_for(label):
                zones.setdefault(pno, []).append(
                    fitz.Rect(r.x0 - 10, r.y0 - 2, r.x0 + w, r.y1 + h))
    return zones


def analyze(pdf_bytes: bytes) -> Analysis:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    a = Analysis()
    texts = [doc[i].get_text() for i in range(doc.page_count)]
    a.page_kinds = [classify_page(t) for t in texts]
    full = "\n".join(texts)

    has_eoi = "eoi" in a.page_kinds
    is_openly = "Openly" in full or "openly.com" in full
    a.template = ("mercury-acord" if has_eoi else
                  "openly" if is_openly else "unknown")
    if a.template == "unknown":
        a.warnings.append("Unrecognized template — detected fields may be incomplete. "
                          "Review the preview carefully.")

    # ---- scanned-page guard
    for i, t in enumerate(texts):
        if len(t.strip()) < 40:
            a.warnings.append(f"Page {i+1} has little or no extractable text "
                              "(possibly a scanned image). Edits cannot reach it.")

    # ---- insured names (invoice page preferred)
    inv_idx = a.page_kinds.index("invoice") if "invoice" in a.page_kinds else None
    if inv_idx is not None:
        lines = [l.strip() for l in texts[inv_idx].split("\n")]
        for j, l in enumerate(lines):
            m = re.search(r"(?:First )?Named Insured:\s*(.+)|Prepared for:\s*(.*)", l)
            if m and not (m.group(1) or m.group(2) or "").strip() and m.group(2) is not None:
                # label alone on its line (binder style): names on following lines
                for k in range(j + 1, min(j + 3, len(lines))):
                    nxt = lines[k]
                    if (nxt and ":" not in nxt and not DATE_NUM_RE.search(nxt)
                            and len(nxt.split()) in (2, 3, 4)
                            and not any(c.isdigit() for c in nxt)):
                        a.insureds.append(nxt)
                    else:
                        break
                if a.insureds:
                    break
                continue
            if m:
                a.insureds.append((m.group(1) or m.group(2)).strip())
                # subsequent bare-name lines belong to co-borrowers
                for k in range(j + 1, min(j + 3, len(lines))):
                    nxt = lines[k]
                    if (nxt and ":" not in nxt and not DATE_NUM_RE.search(nxt)
                            and len(nxt.split()) in (2, 3, 4)
                            and not any(c.isdigit() for c in nxt)):
                        a.insureds.append(nxt)
                    else:
                        break
                break

    # ---- dates
    if inv_idx is not None:
        v, _ = _value_near_label(doc[inv_idx], "Date", DATE_NUM_RE)
        if v is None:  # Openly invoice has a bare date under "INVOICE"
            d0 = DATE_NUM_RE.search(texts[inv_idx])
            v = d0.group(0) if d0 else None
        a.printed_date = v

    eff = exp = None
    if has_eoi:
        eoi = doc[a.page_kinds.index("eoi")]
        eff, _ = _value_near_label(eoi, "EFFECTIVE DATE", DATE_NUM_RE)
        exp, _ = _value_near_label(eoi, "EXPIRATION DATE", DATE_NUM_RE)
        if a.printed_date is None:
            a.printed_date, _ = _value_near_label(eoi, "DATE (MM/DD/YYYY)", DATE_NUM_RE)
    else:
        m = re.search(r"policy period is from\s+(" + DATE_LONG_RE.pattern +
                      r").{0,60}?to\s+(" + DATE_LONG_RE.pattern + r")",
                      full, re.S | re.I)
        if m:
            eff_d, exp_d = parse_long_date(m.group(1)), parse_long_date(m.group(5))
            eff = fmt_long(eff_d) if eff_d else None
            exp = fmt_long(exp_d) if exp_d else None
    a.effective_date, a.expiration_date = eff, exp

    ed, xd = (parse_num_date(eff or "") or parse_long_date(eff or ""),
              parse_num_date(exp or "") or parse_long_date(exp or ""))
    if ed and xd:
        delta = relativedelta(xd, ed)
        a.term_months = delta.years * 12 + delta.months + (1 if delta.days > 15 else 0)

    # ---- mortgagee + loan
    if inv_idx is not None:
        for lbl in ("Bill to:", "Bill To:"):
            blk = _block_lines_after_label(doc[inv_idx], lbl)
            if blk:
                a.mortgagee = blk
                break
    if has_eoi:
        eoi = doc[a.page_kinds.index("eoi")]
        if not a.mortgagee:
            a.mortgagee = _block_lines_after_label(eoi, "NAME AND ADDRESS")
        v, _ = _value_near_label(eoi, "LOAN NUMBER", re.compile(r"\b\d{5,12}\b"))
        a.loan_number = v
        # second mortgage details (text-order extraction is reliable here)
        eoi_text = texts[a.page_kinds.index("eoi")]
        m2b = re.search(r"2nd Mortgage \(if applicable\):\s*\n(.*?)\nCANCELLATION",
                        eoi_text, re.S)
        if m2b:
            # keep only real content — drop empty lines and bare field labels
            # (an EOI with no 2nd mortgage still prints the empty labels)
            a.second_mortgagee = [
                l.strip() for l in m2b.group(1).split("\n")
                if l.strip() and not re.fullmatch(
                    r"Loan Number:?\s*|2nd Mortgage.*", l.strip())][:6]
        m2 = re.search(r"Loan Number:\s*(\d{4,12})", eoi_text)
        if m2:
            a.second_loan_number = m2.group(1)
    elif is_openly:
        # Openly: loan number sits in the Mortgage Lender table
        for i, t in enumerate(texts):
            if "Mortgage Lender" in t:
                seg = t[t.index("Mortgage Lender"):]
                m = re.search(r"\b(\d{6,12})\b", seg)
                if m:
                    a.loan_number = m.group(1)
                break

    if a.second_mortgagee or a.second_loan_number:
        a.warnings.append("Two loans detected — only the 1st loan's mortgagee/loan "
                          "number will be updated; the 2nd mortgage block is protected.")
    return a


# ---------------------------------------------------------------- operations

def change_dates(pdf_bytes: bytes, new_effective: date,
                 new_printed: date | None = None) -> tuple[bytes, list[int], Analysis]:
    """Change effective date; shift expiration by the same policy term;
    update printed date(s). Returns (pdf, changed_pages, analysis)."""
    a = analyze(pdf_bytes)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if not a.effective_date or not a.expiration_date:
        raise ValueError("Could not detect the effective/expiration dates in this "
                         "document. Template may be unsupported.")
    old_eff = parse_num_date(a.effective_date) or parse_long_date(a.effective_date)
    old_exp = parse_num_date(a.expiration_date) or parse_long_date(a.expiration_date)
    term = relativedelta(old_exp, old_eff)
    new_exp = new_effective + term
    new_printed = new_printed or date.today()

    pairs: list[tuple[str, str]] = []
    # effective + expiration: replace both numeric and long-form renderings
    for old_d, new_d in ((old_eff, new_effective), (old_exp, new_exp)):
        pairs.append((fmt_num(old_d), fmt_num(new_d)))
        pairs.append((fmt_long(old_d), fmt_long(new_d)))
    # printed date
    if a.printed_date:
        old_p = parse_num_date(a.printed_date)
        if old_p and old_p not in (old_eff, old_exp):
            pairs.append((fmt_num(old_p), fmt_num(new_printed)))
            pairs.append((fmt_long(old_p), fmt_long(new_printed)))
        elif old_p in (old_eff, old_exp):
            a.warnings.append("The printed date equals the effective/expiration "
                              "date in this document, so it moves with it rather "
                              "than to the chosen printed date. Check the preview.")
    plans = _plan_pairs(doc, pairs)

    if not plans:
        raise ValueError("Nothing to change — the new dates are the same as the "
                         "current ones.")
    _apply_replacements(doc, plans)
    changed = sorted({p.page for p in plans})
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out, changed, a


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _find_mortgagee_instances(doc: fitz.Document, old_lines: list[str],
                              zones: dict[int, list[fitz.Rect]]):
    """Locate every rendered instance of the mortgagee block, regardless of how
    the document wraps or columnizes it. Yields (page_no, clusters) where each
    cluster is a dict {rect, pos, style} — one per visual column of the block."""
    anchor = old_lines[0].rstrip(", ")
    # strip ISAOA decorations so the search matches every rendering of the
    # lender name ("X, LLC ISAOA/ATIMA" on a binder vs "X, LLC, ISAOA" on an EOI)
    anchor_base = re.sub(r"[,\s]*\b(ISAOA.*|its successors.*)$", "",
                         anchor, flags=re.I).strip(" ,") or anchor
    block_norm = _norm(" ".join(old_lines)) + "isaoa atimaisaoaatima"
    for pno in range(doc.page_count):
        page = doc[pno]
        hits = _group_wrapped_hits([r for r in page.search_for(anchor_base)
                                    if not any(r.intersects(z)
                                               for z in zones.get(pno, []))])
        if not hits:
            continue
        page_lines = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                text = "".join(s["text"] for s in line["spans"])
                page_lines.append((fitz.Rect(line["bbox"]), text, line["spans"]))
        page_lines.sort(key=lambda x: (x[0].y0, x[0].x0))

        for group in hits:
            top = group[0]
            lead_limit = (top.y1 - top.y0) * 1.9
            band_bottom = group[-1].y1
            # members: (rect, norm_text, spans)
            members = []
            for lrect, text, spans in page_lines:
                tn = _norm(text)
                if lrect.y1 < top.y0 - 2:
                    continue
                intersects_anchor = any(lrect.intersects(g) for g in group)
                same_col = abs(lrect.x0 - top.x0) <= 40
                in_band = lrect.y0 - band_bottom < lead_limit * 2.2
                # zip+4 tolerance: "City, ST 29502-2028" belongs to a block
                # recorded as "City, ST 29502"
                belongs = tn and (tn in block_norm or
                                  (len(tn) >= 10 and tn[:-4] in block_norm))
                if intersects_anchor:
                    members.append((lrect, tn, spans))
                    band_bottom = max(band_bottom, lrect.y1)
                elif belongs and in_band and (
                        same_col or len(tn) >= 6):
                    if any(lrect.intersects(z) for z in zones.get(pno, [])):
                        continue
                    members.append((lrect, tn, spans))
                    band_bottom = max(band_bottom, lrect.y1)
                elif (same_col and in_band and members and not tn
                      and text.strip() and (lrect.x1 - lrect.x0) < 25):
                    # stray punctuation remnant (e.g. a lone comma) — but never
                    # whitespace-only or wide lines, which would balloon the region
                    members.append((lrect, tn, spans))
            # cluster members by x-column
            clusters: list[dict] = []
            for lrect, tn, spans in sorted(members, key=lambda m: m[0].x0):
                placed = False
                for c in clusters:
                    if abs(lrect.x0 - c["rect"].x0) <= 60:
                        c["rect"] |= lrect
                        c["rects"].append(fitz.Rect(lrect))
                        c["pos"] = min(c["pos"],
                                       block_norm.find(tn) if tn else 10**6)
                        placed = True
                        break
                if not placed:
                    style = spans[0] if spans else None
                    clusters.append({"rect": fitz.Rect(lrect),
                                     "rects": [fitz.Rect(lrect)],
                                     "pos": block_norm.find(tn) if tn else 10**6,
                                     "style": style})
            member_rects = [m[0] for m in members]
            for c in clusters:
                nxt = None
                for lrect, text, _ in page_lines:
                    if (text.strip() and lrect.y0 > c["rect"].y1 - 2
                            and lrect.x0 < c["rect"].x1 and lrect.x1 > c["rect"].x0
                            and not any(abs(lrect.y0 - mr.y0) < 2 and
                                        abs(lrect.x0 - mr.x0) < 2
                                        for mr in member_rects)):
                        nxt = lrect.y0
                        break
                c["free_below"] = max(0.0, (nxt - c["rect"].y1) if nxt else 200.0)
            clusters.sort(key=lambda c: c["pos"])
            yield pno, clusters


ISAOA_RE = re.compile(
    r",?\s*\b(ISAOA\s*/?\s*(?:ATIMA)?|ATIMA|its successors and/?or assigns)\b\.?,?",
    re.I)
CITY_ZIP_RE = re.compile(
    r",?\s*([A-Za-z .'-]+,?\s*[A-Z]{2}[,.]?\s*\d{5}(?:-\d{4})?)\s*$")
STREET_RE = re.compile(
    r",?\s*((?:P\.?\s?O\.?\s?Box\s+[\w-]+|\d+\s+[^,]+?)"
    r"(?:,\s*(?:Suite|Ste\.?|Unit|Apt\.?|Bldg\.?|#)\s*[\w-]+)?)\s*$", re.I)


def normalize_mortgagee_lines(raw: str) -> list[str]:
    """Accept a mortgagee pasted in any shape — structured lines OR one big
    comma-run — and return clean block lines: name / ISAOA-ATIMA / street /
    city-state-zip. Already-structured input (3+ lines) passes through."""
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    if len(lines) >= 3:
        return lines
    text = re.sub(r"\s+", " ", " ".join(lines)).strip().strip(",")
    if not text:
        return []
    city = street = isaoa = None
    m = CITY_ZIP_RE.search(text)
    if m:
        city = m.group(1).strip(" ,")
        text = text[:m.start()].strip(" ,")
    m = STREET_RE.search(text)
    if m:
        street = m.group(1).strip(" ,")
        text = text[:m.start()].strip(" ,")
    m = ISAOA_RE.search(text)
    if m:
        token = m.group(1)
        isaoa = ("ISAOA/ATIMA" if "atima" in token.lower()
                 else "ISAOA" if "isaoa" in token.lower() else token)
        text = (text[:m.start()] + " " + text[m.end():]).strip(" ,")
        text = re.sub(r"\s+", " ", text).strip(" ,")
    name = text.strip(" ,")
    out = []
    if name:
        out.append(name if name.endswith(",") else name + ",")
    if isaoa:
        out.append(isaoa)
    if street:
        out.append(street)
    if city:
        out.append(city)
    return out or lines


def change_mortgagee(pdf_bytes: bytes, new_lines: list[str],
                     new_loan: str) -> tuple[bytes, list[int], Analysis]:
    """Replace the 1st-loan mortgagee block and loan number everywhere they appear,
    never touching a 2nd-mortgage block. Works block-wise, so it is immune to
    different line-wrapping between the invoice, EOI and declarations table."""
    a = analyze(pdf_bytes)
    if not a.mortgagee:
        raise ValueError("Could not detect the current mortgagee block.")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    zones = _second_mortgage_zones(doc)

    old_lines = [l for l in a.mortgagee if l.strip()]
    new_lines = normalize_mortgagee_lines("\n".join(new_lines))
    if not new_lines:
        raise ValueError("New mortgagee is empty after formatting.")

    # Mirror the ORIGINAL block's structure so line count (and therefore font
    # size and spacing) is preserved: if the document writes the ISAOA/ATIMA
    # designation inline with the lender name, keep it inline; if it puts it
    # on its own line, split it out.
    def _is_isaoa_line(l: str) -> bool:
        return bool(re.fullmatch(r"ISAOA\s*/?\s*(ATIMA)?\.?,?", l.strip(), re.I))
    old_inline = bool(old_lines and ISAOA_RE.search(old_lines[0]))
    if old_inline and len(new_lines) >= 2 and _is_isaoa_line(new_lines[1]):
        new_lines = ([new_lines[0].rstrip(", ") + " " + new_lines[1].strip()]
                     + new_lines[2:])
    elif (not old_inline and new_lines and ISAOA_RE.search(new_lines[0])
          and not (len(new_lines) >= 2 and _is_isaoa_line(new_lines[1]))):
        m = ISAOA_RE.search(new_lines[0])
        token = m.group(1)
        name = (new_lines[0][:m.start()] + new_lines[0][m.end():]).strip(" ,")
        isaoa = ("ISAOA/ATIMA" if "atima" in token.lower()
                 else "ISAOA" if "isaoa" in token.lower() else token)
        new_lines = [name + ",", isaoa] + new_lines[1:]
    changed: set[int] = set()

    # split the new block into name-part and address-part (for columnized tables)
    addr_re = re.compile(r"^(\d|P\.?\s?O\.?\s?Box|Box\s\d|PO\s?Box)", re.I)
    split_at = next((i for i, l in enumerate(new_lines) if addr_re.match(l)),
                    len(new_lines))

    # ---- block replacement, one instance at a time
    instances = list(_find_mortgagee_instances(doc, old_lines, zones))
    if not instances:
        raise ValueError("Could not locate the mortgagee block in the document.")
    for pno, clusters in instances:
        page = doc[pno]
        # assign new lines to clusters: single column gets everything;
        # two+ columns get name-part / address-part
        if len(clusters) == 1:
            assignments = [new_lines]
        else:
            assignments = [new_lines[:split_at], new_lines[split_at:]]
            for _ in range(len(clusters) - 2):
                assignments.append([])
        # redact the member lines only (a bounding box could swallow
        # unrelated lines sitting inside it, e.g. a binder's loan-number row)
        for c in clusters:
            for r in c["rects"]:
                page.add_redact_annot(r)
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        except TypeError:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        for c, lines_for_c in zip(clusters, assignments):
            if not lines_for_c:
                continue
            region = c["rect"]
            style_span = c["style"]
            if style_span is not None:
                size = style_span["size"]
                color = int_color(style_span.get("color", 0))
                alias, ffile = resolve_font(style_span["font"],
                                            style_span.get("flags", 0))
            else:
                size, color, alias, ffile = 7.0, (0, 0, 0), "helv", None
            max_w = max(region.x1 - region.x0, 90)
            out_lines: list[str] = []
            for nl in lines_for_c:
                out_lines += _wrap_text(nl, max_w, size, alias, ffile)
            lead = size * 1.22
            # vertical budget: the old block's rows plus whatever empty space
            # sits below before the next line of real text (e.g. a binder's
            # loan-number row must never be overwritten)
            budget = (region.y1 - region.y0) + c.get("free_below", 200.0) - 1.5
            def needed():
                return (len(out_lines) - 1) * lead + size * 1.15
            # 1) tighten line spacing a little            (invisible change)
            while needed() > budget and lead > size * 1.04:
                lead -= 0.25
            # 2) shrink the font, at most 25%             (still readable)
            floor = size * 0.75
            while needed() > budget and size > floor:
                size -= 0.25
                lead = size * 1.1
                out_lines = []
                for nl in lines_for_c:
                    out_lines += _wrap_text(nl, max_w, size, alias, ffile)
            kwargs = dict(fontname=alias, fontsize=size, color=color)
            if ffile:
                kwargs["fontfile"] = ffile
            y = region.y0 + size        # first baseline
            for ln in out_lines:
                page.insert_text((region.x0, y), ln, **kwargs)
                y += lead
        changed.add(pno)

    # ---- loan number (span-level replacement is exact here)
    if a.loan_number and new_loan and a.loan_number != new_loan:
        plans = _plan_string_replacements(doc, a.loan_number, new_loan,
                                          exclude_rects=zones)
        _apply_replacements(doc, plans)
        changed |= {p.page for p in plans}

    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out, sorted(changed), a


def merge(files: list[tuple[str, bytes]]) -> tuple[bytes, str, list[dict]]:
    """Merge in Invoice -> EOI/Dec -> RCE order; compute borrower filename.
    Returns (pdf, suggested_filename_stem, per_file_info)."""
    docs = []
    for fname, data in files:
        d = fitz.open(stream=data, filetype="pdf")
        kind = classify_page(d[0].get_text())
        docs.append({"name": fname, "kind": kind, "doc": d})
    docs.sort(key=lambda x: KIND_RANK.get(x["kind"], 3))

    out = fitz.open()
    for item in docs:
        out.insert_pdf(item["doc"])
    merged = out.tobytes(garbage=3, deflate=True)

    stem = "Merged"
    insureds = analyze(merged).insureds
    if insureds:
        stem = build_filename_stem(insureds)
    info = [{"name": d["name"], "kind": d["kind"], "pages": d["doc"].page_count}
            for d in docs]
    for d in docs:
        d["doc"].close()
    out.close()
    return merged, stem, info


# ---------------------------------------------------------------- naming

def _split_name(full: str) -> tuple[str, str]:
    parts = [p for p in full.replace(",", " ").split() if p]
    while parts and parts[-1].lower() in NAME_SUFFIXES:
        parts.pop()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], parts[-1]


def build_filename_stem(insureds: list[str]) -> str:
    names = [_split_name(x) for x in insureds if x.strip()]
    if not names:
        return "Merged"
    if len(names) == 1:
        f, l = names[0]
        return f"{l}_{f}"
    (f1, l1), (f2, l2) = names[0], names[1]
    if l1.lower() == l2.lower():
        return f"{l1}_{f1}.{f2}"            # same last name: listed order
    a, b = sorted([l1, l2], key=str.lower)  # different last names: alphabetical
    return f"{a}.{b}"


def revised_filename(original: str) -> str:
    stem = re.sub(r"\.pdf$", "", original, flags=re.I).strip()
    m = re.search(r"(?i)\brevised(?:\s+(\d+))?\s*$", stem)
    if not m:
        return f"{stem} revised.pdf"
    nxt = int(m.group(1) or 0) + 1
    return f"{stem[:m.start()].rstrip()} revised {nxt}.pdf"


# ---------------------------------------------------------------- previews

def render_pages(pdf_bytes: bytes, pages: list[int], dpi: int = 110) -> dict[int, bytes]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = {}
    for p in pages:
        if 0 <= p < doc.page_count:
            out[p] = doc[p].get_pixmap(dpi=dpi).tobytes("png")
    doc.close()
    return out
