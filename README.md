# PDF Adjuster

Internal tool for a P&C insurance agency. Three operations on policy-document
packets (Invoice / EOI or Declarations / RCE), all with a mandatory
before/after preview — nothing is saved until a human reviews and clicks
Download. Documents never leave the machine.

1. **Effective date change** — detects the current effective/expiration dates
   and policy term (12 mo, 6 mo, whatever it is), moves the expiration date by
   the same term, and updates the printed date (defaults to today). Accepts
   multiple PDFs at once and walks through them one at a time, prompting the
   dates for each file.
2. **Combine & name** — merges dropped PDFs in Invoice → EOI/Declarations →
   RCE order regardless of drop order, and names the file by the borrower
   rules: `Lastname_Firstname` (one borrower), `Lastname_First1.First2`
   (two borrowers, same last name), `Lastname.Lastname` (two different last
   names, alphabetical, no first names).
3. **Mortgagee / loan update** — detects the current 1st-loan mortgagee block
   and loan number, replaces them everywhere they appear (invoice Bill-To,
   EOI Additional Interest, both loan-number fields), and **never touches a
   2nd-mortgage block**. Output name gets ` revised`, then ` revised 1`,
   ` revised 2`, … if the input was already revised.

Supported templates so far: **Mercury (ACORD 27 EOI)** and **Openly/Rock
Ridge declarations**. Unknown templates get a warning and best-effort field
detection — always check the preview.

## Deploy to Vercel (demo link)

The repo is Vercel-ready: `api/index.py` is the serverless function,
`public/index.html` is the UI, `vercel.json` wires them together.

1. Push this folder to a GitHub repo (see below).
2. In Vercel: **Add New → Project → Import** the repo. No framework preset
   needed — leave everything default and click Deploy.
3. Share the `*.vercel.app` URL.

**Demo-mode warning:** with no login, anyone with the URL can use the site,
and uploaded PDFs are processed on Vercel's servers (in memory only, never
stored). Demo with sample documents; do not process real borrower files on
the public link until authentication (Clerk) is added. The web version also
caps uploads around 4 MB per request — the local version has no limit.

## Run it (Windows, VS Code)

Open this folder in VS Code, then in the terminal (Terminal → New Terminal):

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open http://localhost:8765 in your browser.
Next time, you only need:

```
.venv\Scripts\activate
python app.py
```

(Mac/Linux: `source .venv/bin/activate` instead of the second line.)

## Files

- `engine.py` — all PDF logic (detection, replacement, merge, naming)
- `app.py` — local web server (FastAPI)
- `static/index.html` — the entire UI
- `test_engine.py` — test suite; put sample PDFs named `Oliver.pdf`,
  `Molina.pdf`, `Catario.pdf` in a `samples/` folder next to it, then
  `python test_engine.py` (50 checks)

## Known limits / notes

- Replacement text uses real fonts bundled in `fonts/`: Libre Franklin (the
  actual font in Openly documents), Carlito (metric-identical to Calibri), and
  Liberation Serif/Sans (metric-identical to Times/Arial), with the original
  size, weight, style, and color. When a replacement changes text width, the
  rest of the line is re-laid-out so spacing stays true. Keep the `fonts/`
  folder next to `engine.py`.
- Scanned/image-only pages can't be edited (the tool warns if it sees one).
- Date edits replace every occurrence of the exact old date strings on any
  page; the RCE report date is untouched because it differs in format/value.
  The preview shows every changed page — review it.
- If both loans ever have the *same* lender and loan number, the tool cannot
  distinguish them by text alone; the preview would reveal it. Flag if this
  case actually occurs.
