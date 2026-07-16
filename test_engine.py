import sys, json
from datetime import date
sys.path.insert(0, "app")
import fitz
import engine

PASS = FAIL = 0
def check(name, ok):
    global PASS, FAIL
    print(("PASS " if ok else "FAIL ") + name)
    PASS += ok; FAIL += (not ok)

samples = {n: open(f"samples/{n}.pdf", "rb").read() for n in ("Oliver", "Molina", "Catario")}

# ---------- 1. analyze ----------
print("\n===== ANALYZE =====")
An = {}
for n, b in samples.items():
    a = engine.analyze(b)
    An[n] = a
    print(f"\n{n}: template={a.template} kinds={a.page_kinds}")
    print(f"  printed={a.printed_date} eff={a.effective_date} exp={a.expiration_date} term={a.term_months}mo")
    print(f"  insureds={a.insureds}")
    print(f"  mortgagee={a.mortgagee} loan={a.loan_number}")
    print(f"  2nd={a.second_mortgagee} loan2={a.second_loan_number}")
    print(f"  warnings={a.warnings}")

check("Catario template", An["Catario"].template == "mercury-acord")
check("Molina template", An["Molina"].template == "openly")
check("Catario eff/exp", An["Catario"].effective_date == "7/22/2026" and An["Catario"].expiration_date == "7/22/2027")
check("Catario term 12mo", An["Catario"].term_months == 12)
check("Catario loan", An["Catario"].loan_number == "6012662")
check("Catario 2nd loan", An["Catario"].second_loan_number == "1576119")
check("Catario insureds", len(An["Catario"].insureds) == 2)
check("Catario mortgagee has UHM", any("Union Home" in l for l in An["Catario"].mortgagee))
check("Molina eff long", An["Molina"].effective_date == "July 17, 2026")
check("Molina loan", An["Molina"].loan_number == "92546986")
check("Molina insureds 2", len(An["Molina"].insureds) == 2)
check("Oliver loan", An["Oliver"].loan_number == "1526310197")
check("Oliver mortgagee UWM", any("United Wholesale" in l for l in An["Oliver"].mortgagee))

# ---------- 2. date change: Catario (numeric, 12mo term) ----------
print("\n===== DATE CHANGE: Catario =====")
out, changed, _ = engine.change_dates(samples["Catario"], date(2026, 12, 1), date(2026, 7, 16))
d = fitz.open(stream=out, filetype="pdf"); t = "\n".join(p.get_text() for p in d)
check("eff 12/1/2026", "12/1/2026" in t)
check("exp 12/1/2027 (term preserved)", "12/1/2027" in t)
check("printed 7/16/2026", "7/16/2026" in t)
check("old eff gone", "7/22/2026" not in t)
check("old exp gone", "7/22/2027" not in t)
check("old printed gone", "7/15/2026" not in t)
check("RCE date untouched", "07/03/2026" in t)
d.close()

# ---------- 3. date change: Molina (long form) ----------
print("\n===== DATE CHANGE: Molina =====")
out, changed, _ = engine.change_dates(samples["Molina"], date(2026, 8, 30), date(2026, 7, 16))
d = fitz.open(stream=out, filetype="pdf"); t = "\n".join(p.get_text() for p in d)
check("eff August 30, 2026", "August 30, 2026" in t)
check("exp August 30, 2027", "August 30, 2027" in t)
check("printed 7/16/2026", "7/16/2026" in t)
check("old eff gone", "July 17, 2026" not in t)
check("old printed gone", "7/15/2026" not in t)
d.close()

# ---------- 4. 6-month term simulation ----------
print("\n===== 6-MONTH TERM =====")
# fabricate: take Catario, first change exp to 1/22/2027 (6mo), then run date change
tmp = fitz.open(stream=samples["Catario"], filetype="pdf")
import engine as E
plans = E._plan_string_replacements(tmp, "7/22/2027", "1/22/2027")
E._apply_replacements(tmp, plans)
six = tmp.tobytes(); tmp.close()
a6 = engine.analyze(six)
check("term detected 6mo", a6.term_months == 6)
out, _, _ = engine.change_dates(six, date(2026, 9, 10), date(2026, 7, 16))
d = fitz.open(stream=out, filetype="pdf"); t = "\n".join(p.get_text() for p in d)
check("6mo: eff 9/10/2026", "9/10/2026" in t)
check("6mo: exp 3/10/2027", "3/10/2027" in t)
d.close()

# ---------- 5. mortgagee change: Catario (two loans!) ----------
print("\n===== MORTGAGEE: Catario =====")
new_block = ["Rocket Mortgage, LLC,", "ISAOA/ATIMA", "1050 Woodward Ave", "Detroit, MI 48226"]
out, changed, _ = engine.change_mortgagee(samples["Catario"], new_block, "3487651290")
d = fitz.open(stream=out, filetype="pdf")
t1, t2 = d[0].get_text(), d[1].get_text()
check("new mortgagee on invoice", "Rocket Mortgage" in t1)
check("new mortgagee on EOI", "Rocket Mortgage" in t2)
check("old gone everywhere", "Union Home" not in t1 + t2)
check("new loan# (2 spots)", t2.count("3487651290") == 2)
check("old loan# gone", "6012662" not in t1 + t2)
check("2nd mortgagee intact", "California Housing Finance Agency" in t2)
check("2nd loan intact", "1576119" in t2)
d.close()

# ---------- 6. mortgagee change: Molina (openly) ----------
print("\n===== MORTGAGEE: Molina =====")
out, changed, _ = engine.change_mortgagee(samples["Molina"],
    ["NewRez LLC", "ISAOA/ATIMA", "PO Box 7050", "Troy, MI 48007"], "555123456")
d = fitz.open(stream=out, filetype="pdf"); t = "\n".join(p.get_text() for p in d)
check("new lender present", "NewRez" in t)
check("old lender gone", "American Financial" not in t)
check("new loan present", "555123456" in t)
check("old loan gone", "92546986" not in t)
d.close()

# ---------- 7. merge + naming ----------
print("\n===== MERGE =====")
def split(name, ranges):
    src = fitz.open(stream=samples[name], filetype="pdf")
    parts = []
    for i, (a, b) in enumerate(ranges):
        d = fitz.open(); d.insert_pdf(src, from_page=a, to_page=b)
        parts.append((f"part{i}.pdf", d.tobytes())); d.close()
    src.close(); return parts

# Catario: invoice p1, EOI p2, RCE p3-4 — feed shuffled
parts = split("Catario", [(2, 3), (0, 0), (1, 1)])  # RCE, Invoice, EOI
merged, stem, info = engine.merge(parts)
print("  order:", [i["kind"] for i in info], "stem:", stem)
check("order Invoice,EOI,RCE", [i["kind"] for i in info] == ["invoice", "eoi", "rce"])
check("stem Catario.Ochoa", stem == "Catario.Ochoa")

parts = split("Molina", [(7, 7), (1, 6), (0, 0)])  # RCE, Dec, Invoice
merged, stem, info = engine.merge(parts)
print("  order:", [i["kind"] for i in info], "stem:", stem)
check("order Invoice,Dec,RCE", [i["kind"] for i in info] == ["invoice", "dec", "rce"])
check("stem Molina.Puente", stem == "Molina.Puente")

parts = split("Oliver", [(1, 1), (2, 3), (0, 0)])
merged, stem, info = engine.merge(parts)
print("  order:", [i["kind"] for i in info], "stem:", stem)
check("stem Oliver_Dale.Michelle", stem == "Oliver_Dale.Michelle")

# ---------- 8. naming rules ----------
print("\n===== NAMING =====")
check("single borrower", engine.build_filename_stem(["Faith M Catario"]) == "Catario_Faith")
check("same last", engine.build_filename_stem(["Dale Leroy Oliver", "Michelle Oliver"]) == "Oliver_Dale.Michelle")
check("diff last", engine.build_filename_stem(["Mario Puente", "Claudia Jamilett Molina"]) == "Molina.Puente")
check("revised new", engine.revised_filename("Catario.Ochoa HOI Docs.pdf") == "Catario.Ochoa HOI Docs revised.pdf")
check("revised bare -> 1", engine.revised_filename("X revised.pdf") == "X revised 1.pdf")
check("revised 3 -> 4", engine.revised_filename("X revised 3.pdf") == "X revised 4.pdf")

# ---------- 9. regressions ----------
print("\n===== REGRESSIONS =====")
check("Oliver: no false 2-loan warning", not An["Oliver"].warnings)
check("Molina: no false 2-loan warning", not An["Molina"].warnings)
check("Catario: 2-loan warning kept", any("Two loans" in w for w in An["Catario"].warnings))

# same-line multi-target: both dates on ONE line must not double-print
synth = fitz.open(); sp = synth.new_page()
sp.insert_text((72, 100), "Policy period from 7/22/2026 to 7/22/2027 inclusive.", fontsize=11)
sb = synth.tobytes()
sd = fitz.open(stream=sb, filetype="pdf")
plans = engine._plan_pairs(sd, [("7/22/2026", "12/1/2026"), ("7/22/2027", "12/1/2027")])
engine._apply_replacements(sd, plans)
st = sd[0].get_text()
check("same-line: both dates replaced", "12/1/2026" in st and "12/1/2027" in st)
check("same-line: no duplicated tail", st.count("inclusive") == 1)
check("same-line: old dates gone", "7/22/2026" not in st and "7/22/2027" not in st)
sd.close()

# ---------- 10. smart paste ----------
print("\n===== SMART PASTE =====")
got = engine.normalize_mortgagee_lines(
    "Kind Lending, LLC, c/o LoanCare, LLC ISAOA/ATIMA, PO Box 202049, Florence, SC 29502-2049")
check("single-line paste splits correctly",
      got == ["Kind Lending, LLC, c/o LoanCare, LLC,", "ISAOA/ATIMA",
              "PO Box 202049", "Florence, SC 29502-2049"])
check("structured input untouched",
      engine.normalize_mortgagee_lines("A, LLC,\nISAOA\n1 Main St\nCity, TX 75000")
      == ["A, LLC,", "ISAOA", "1 Main St", "City, TX 75000"])
# end-to-end: single-line paste through change_mortgagee
out, _, _ = engine.change_mortgagee(samples["Catario"],
    ["Kind Lending, LLC, c/o LoanCare, LLC ISAOA/ATIMA, PO Box 202049, Florence, SC 29502-2049"],
    "9988776655")
d = fitz.open(stream=out, filetype="pdf"); t = "\n".join(p.get_text() for p in d)
check("paste e2e: new lender in", "Kind Lending" in t and "Florence, SC 29502-2049" in t)
check("paste e2e: old gone", "Union Home" not in t)
check("paste e2e: 2nd loan intact", "1576119" in t)
d.close()

# ---------- 11. Castillo (Pacific Specialty binder template) ----------
import os
if os.path.exists("samples/Castillo.pdf"):
    print("\n===== CASTILLO (binder) =====")
    cb = open("samples/Castillo.pdf", "rb").read()
    ca = engine.analyze(cb)
    check("castillo insureds", ca.insureds == ["Oscar Reyes Castillo"])
    check("castillo loan", ca.loan_number == "1526307871")
    check("castillo no false 2-loan warning", not ca.warnings)
    out, _, _ = engine.change_dates(cb, date(2026, 9, 1), date(2026, 7, 16))
    d = fitz.open(stream=out, filetype="pdf"); t = "\n".join(p.get_text() for p in d)
    check("castillo fused term string updated", "9/1/2026-9/1/2027" in t)
    check("castillo old dates gone", "7/31/2026" not in t and "7/31/2027" not in t)
    d.close()
    out, _, _ = engine.change_mortgagee(cb,
        ["Kind Lending, LLC, c/o LoanCare, LLC ISAOA/ATIMA, PO Box 202049, Florence, SC 29502-2049"],
        "9988776655")
    d = fitz.open(stream=out, filetype="pdf")
    t1, t2 = d[0].get_text(), d[1].get_text()
    check("castillo binder lender swapped", "Kind Lending" in t1 and "United Wholesale" not in t1)
    check("castillo binder city present", "Florence, SC 29502-2049" in t1)
    check("castillo binder loan swapped in place", "9988776655" in t1 and "1526307871" not in t1)
    check("castillo binder labels intact", t1.count("Loan Number:") == 2)
    check("castillo eoi swapped", "Kind Lending" in t2 and t2.count("9988776655") == 2)
    # structure + size parity: inline ISAOA kept inline, font size unchanged
    check("castillo inline ISAOA preserved",
          "Kind Lending, LLC, c/o LoanCare, LLC ISAOA/ATIMA" in t1)
    def _span_size(doc_, pno, needle):
        for b_ in doc_[pno].get_text("dict")["blocks"]:
            if b_.get("type") != 0: continue
            for l_ in b_["lines"]:
                for s_ in l_["spans"]:
                    if needle in s_["text"]: return round(s_["size"], 2)
    orig_d = fitz.open(stream=cb, filetype="pdf")
    check("castillo font size parity",
          _span_size(orig_d, 0, "United Wholesale") == _span_size(d, 0, "Kind Lending"))
    orig_d.close()
    d.close()

# ---------- 12. padded dates + inline-ISAOA mirroring (self-contained) ----------
print("\n===== PADDED DATES / INLINE MIRROR =====")
sd = fitz.open(); sp = sd.new_page(width=612, height=792)
sp.insert_text((72, 70), "INVOICE", fontsize=12, fontname="hebo")
sp.insert_text((72, 90), "Date: 07/15/2026", fontsize=10)
sp2 = sd.new_page(width=612, height=792)
sp2.insert_text((72, 70), "EVIDENCE OF PROPERTY INSURANCE", fontsize=12, fontname="hebo")
sp2.insert_text((72, 100), "EFFECTIVE DATE", fontsize=7); sp2.insert_text((72, 112), "08/01/2026", fontsize=9)
sp2.insert_text((200, 100), "EXPIRATION DATE", fontsize=7); sp2.insert_text((200, 112), "02/01/2027", fontsize=9)
sp2.insert_text((350, 100), "LOAN NUMBER", fontsize=7); sp2.insert_text((350, 112), "12345678", fontsize=9)
sp2.insert_text((72, 200), "ADDITIONAL INTEREST  NAME AND ADDRESS", fontsize=7)
sp2.insert_text((72, 214), "Acme Lending Co., ISAOA", fontsize=8)
sp2.insert_text((72, 224), "9 Elm St", fontsize=8)
sp2.insert_text((72, 234), "Mesa, AZ 85201", fontsize=8)
pb = sd.tobytes(); sd.close()
out, _, _ = engine.change_dates(pb, date(2026, 10, 15), date(2026, 7, 16))
t = "\n".join(p.get_text() for p in fitz.open(stream=out, filetype="pdf"))
check("padded eff replaced", "10/15/2026" in t and "08/01/2026" not in t)
check("padded exp 6mo shift", "04/15/2027" in t)
check("padded printed replaced", "07/16/2026" in t and "07/15/2026" not in t)
out, _, _ = engine.change_mortgagee(pb, ["New Lender LLC, ISAOA/ATIMA, PO Box 5, Tempe, AZ 85281"], "999888777")
t = fitz.open(stream=out, filetype="pdf")[1].get_text()
check("inline ISAOA mirrored", "New Lender LLC ISAOA/ATIMA" in t)
check("long insured names", engine.normalize_mortgagee_lines("A, LLC, ISAOA, 1 Way, X, TX 75000")[0] == "A, LLC,")

print(f"\n===== {PASS} passed, {FAIL} failed =====")
sys.exit(1 if FAIL else 0)
