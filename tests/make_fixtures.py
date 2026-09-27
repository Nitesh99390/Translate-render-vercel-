#!/usr/bin/env python3
"""Generate small sample documents in every supported format (for docconv tests)."""
import io, random, zipfile, sys
from pathlib import Path

import pymupdf, docx

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dc")
out.mkdir(parents=True, exist_ok=True)
random.seed(1)
words = "the quick brown fox jumps over lazy dog नमस्ते दुनिया यह एक परीक्षण है lorem ipsum dolor sit amet".split()


def para(n=60):
    return " ".join(random.choice(words) for _ in range(n)) + "."


pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 400), False); pix.clear_with(200)
png = pix.tobytes("png")
pix2 = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 900, 1200), False); pix2.clear_with(60)
cover = pix2.tobytes("jpeg")

# EPUB
with zipfile.ZipFile(out / "book.epub", "w") as z:
    z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    z.writestr("OEBPS/style.css", "p{ text-indent:1em } .big{font-size:2em}")
    z.writestr("OEBPS/images/cover.jpg", cover)
    z.writestr("OEBPS/images/fig.png", png)
    for i in range(1, 4):
        body = f"<h1>Chapter {i}</h1>" + "".join(f"<p>{para()}</p>" for _ in range(40))
        if i == 2:
            body += '<p><img src="images/fig.png" alt="fig"/></p><ul><li>one <b>bold</b></li><li>two</li></ul><table><tr><td>a</td><td>b</td></tr></table>'
        z.writestr(f"OEBPS/ch{i}.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ch {i}</title><link rel="stylesheet" href="style.css"/></head><body>{body}</body></html>')
    man = "".join(f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(1, 4))
    sp = "".join(f'<itemref idref="c{i}"/>' for i in range(1, 4))
    z.writestr("OEBPS/content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="u">x</dc:identifier><dc:title>Test Book</dc:title><dc:creator>Author A</dc:creator><dc:language>hi</dc:language><meta name="cover" content="cov"/></metadata><manifest>{man}<item id="css" href="style.css" media-type="text/css"/><item id="cov" href="images/cover.jpg" media-type="image/jpeg"/><item id="fig" href="images/fig.png" media-type="image/png"/></manifest><spine>{sp}</spine></package>')

(out / "book.txt").write_text("\n\n".join(["CHAPTER ONE"] + [para() for _ in range(30)] + ["CHAPTER TWO"] + [para() for _ in range(30)]), encoding="utf-8")
(out / "book.md").write_text("# Title\n\n## Part A\n\nSome **bold** and *it*.\n\n- a\n- b\n\n## Part B\n\n" + para() + "\n", encoding="utf-8")
(out / "book.html").write_text('<html><head><title>H</title><style>p{color:red}</style></head><body><h1>One</h1>' + "".join(f"<p>{para()}</p>" for _ in range(50)) + '<h1>Two</h1>' + "".join(f"<p>{para()}</p>" for _ in range(50)) + '</body></html>', encoding="utf-8")

d = docx.Document(); d.add_heading("Doc Title", 0)
for i in range(1, 3):
    d.add_heading(f"Heading {i}", 1)
    for _ in range(25):
        d.add_paragraph(para())
    d.add_paragraph("bullet item", style="List Bullet")
    p = d.add_paragraph(); p.add_run("bold ").bold = True; p.add_run("italic").italic = True
    d.add_picture(io.BytesIO(png))
    t = d.add_table(rows=2, cols=2); t.cell(0, 0).text = "c00"; t.cell(1, 1).text = "c11"
d.save(out / "book.docx")

pdf = pymupdf.open()
for i in range(1, 4):
    page = pdf.new_page()
    page.insert_text((72, 72), f"Chapter {i}", fontsize=22)
    y = 110
    for _ in range(8):
        page.insert_textbox(pymupdf.Rect(72, y, 520, y + 90), para(40), fontsize=10); y += 95
    if i == 2:
        page.insert_image(pymupdf.Rect(72, y, 300, y + 150), stream=png)
    page.insert_text((300, 820), str(i), fontsize=9)
pdf.set_toc([[1, f"Chapter {i}", i] for i in range(1, 4)])
pdf.save(out / "book.pdf")
for f in sorted(out.iterdir()):
    print(f.name, f.stat().st_size)
