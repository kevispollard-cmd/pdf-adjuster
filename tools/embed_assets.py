"""Regenerate api/_assets.py from public/index.html + fonts/. Run from repo root."""
import base64, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
html = (root / "public/index.html").read_text()
fonts = {p.name: base64.b64encode(p.read_bytes()).decode()
         for p in sorted((root / "fonts").glob("*.ttf"))}
out = ["\"\"\"Auto-generated: UI + fonts embedded so serverless bundling can never lose them.",
       "Regenerate with tools/embed_assets.py after editing public/index.html or fonts/.\"\"\"",
       "", f"UI_HTML = {html!r}", "", "FONTS = {"]
for k, v in fonts.items():
    out.append(f"    {k!r}: {v!r},")
out.append("}")
(root / "api/_assets.py").write_text("\n".join(out))
print("regenerated api/_assets.py")
