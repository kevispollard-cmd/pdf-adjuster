"""PDF Adjuster — stateless API (runs identically on localhost and Vercel).

Every operation returns the finished PDF as base64 in the same response as
the preview, so no server-side state is needed and the browser saves the
file directly. Nothing is ever written to disk or stored.
"""

import base64
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

import engine  # noqa: E402

app = FastAPI(title="PDF Adjuster")

MAX_UPLOAD = 4 * 1024 * 1024  # stay under serverless body limits


def _check_size(*blobs: bytes):
    if sum(len(b) for b in blobs) > MAX_UPLOAD:
        raise HTTPException(413, "Upload too large for the web version (max ~4 MB "
                                 "per request). Use the local version for big files.")


def _previews(before: bytes, after: bytes, pages: list[int]) -> list[dict]:
    b = engine.render_pages(before, pages)
    a = engine.render_pages(after, pages)
    return [{"page": p + 1,
             "before": base64.b64encode(b[p]).decode(),
             "after": base64.b64encode(a[p]).decode()}
            for p in pages if p in b and p in a]


def _parse_date(s: str) -> date:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise HTTPException(422, f"Unrecognized date: {s!r}")


@app.get("/")
def index():
    return FileResponse(ROOT / "public" / "index.html")


@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...)):
    data = await file.read()
    _check_size(data)
    try:
        a = engine.analyze(data)
    except Exception as e:
        raise HTTPException(422, f"Could not read PDF: {e}")
    return a.to_dict()


@app.post("/api/edit-dates")
async def api_edit_dates(file: UploadFile = File(...),
                         new_effective: str = Form(...),
                         new_printed: str = Form("")):
    data = await file.read()
    _check_size(data)
    eff = _parse_date(new_effective)
    printed = _parse_date(new_printed) if new_printed.strip() else date.today()
    try:
        out, changed, a = engine.change_dates(data, eff, printed)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"previews": _previews(data, out, changed),
            "filename": engine.revised_filename(file.filename or "document.pdf"),
            "pdf": base64.b64encode(out).decode(),
            "warnings": a.warnings,
            "summary": {"old_effective": a.effective_date,
                        "old_expiration": a.expiration_date,
                        "term_months": a.term_months}}


@app.post("/api/edit-mortgagee")
async def api_edit_mortgagee(file: UploadFile = File(...),
                             new_block: str = Form(...),
                             new_loan: str = Form(...)):
    data = await file.read()
    _check_size(data)
    lines = [l for l in (new_block or "").splitlines() if l.strip()]
    if not lines:
        raise HTTPException(422, "New mortgagee block is empty.")
    try:
        out, changed, a = engine.change_mortgagee(data, lines, new_loan.strip())
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"previews": _previews(data, out, changed),
            "filename": engine.revised_filename(file.filename or "document.pdf"),
            "pdf": base64.b64encode(out).decode(),
            "warnings": a.warnings,
            "summary": {"old_mortgagee": a.mortgagee, "old_loan": a.loan_number,
                        "second_mortgagee": a.second_mortgagee,
                        "second_loan": a.second_loan_number}}


@app.post("/api/merge")
async def api_merge(files: list[UploadFile] = File(...)):
    if len(files) < 2:
        raise HTTPException(422, "Drop at least two PDFs to merge.")
    blobs = []
    for f in files:
        blobs.append((f.filename or "file.pdf", await f.read()))
    _check_size(*[b for _, b in blobs])
    try:
        merged, stem, info = engine.merge(blobs)
    except Exception as e:
        raise HTTPException(422, f"Merge failed: {e}")
    pv = engine.render_pages(merged, [0])
    return {"filename": f"{stem}.pdf",
            "pdf": base64.b64encode(merged).decode(),
            "order": info,
            "first_page": base64.b64encode(pv[0]).decode() if 0 in pv else None}
