#!/usr/bin/env python3
"""Smoke test: every input format → every output format, plus splitting.
Usage: python tests/test_docconv.py [fixture_dir] [font.ttf]"""
import sys, time, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import docconv as dc  # noqa: E402

fx = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dc")
font = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/fonts/NotoSansDevanagari-Regular.ttf")
out = fx / "out"
out.mkdir(exist_ok=True)
fails = 0
for s in ["book.md", "book.txt", "book.html", "book.docx", "book.epub", "book.pdf"]:
    for o in dc.OUTPUT_EXTS:
        dst = out / f"{Path(s).stem}_{Path(s).suffix[1:]}{o}"
        t = time.time()
        try:
            notes = dc.convert(fx / s, dst, font=font if font.exists() else None)
            print(f"{s:10} -> {o:6} {dst.stat().st_size:8d} B  {time.time() - t:.2f}s {notes}", flush=True)
        except Exception as e:
            fails += 1
            print(f"{s:10} -> {o:6} FAIL {e}", flush=True)
            traceback.print_exc()
print("conversion failures:", fails)

# ── splitting ──────────────────────────────────────────────────────────────
import zipfile  # noqa: E402

dc.MIN_SPLIT_BYTES = 1024  # fixtures are tiny; production minimum stays 256 KB
sp = out / "split"
sp.mkdir(exist_ok=True)
sfails = 0
for s, lim in [("book.epub", 25000), ("book.pdf", 244000), ("book.docx", 40000), ("book.txt", 9000), ("book.html", 16000)]:
    try:
        parts = dc.split_file(fx / s, lim, sp)
        sizes = [p.stat().st_size for p in parts]
        assert len(parts) > 1, "expected more than one part"
        assert all(x <= lim for x in sizes), f"part exceeds limit: {sizes}"
        for p in parts:
            if p.suffix == ".epub":
                z = zipfile.ZipFile(p)
                assert z.testzip() is None and z.namelist()[0] == "mimetype"
            elif p.suffix == ".pdf":
                import pymupdf
                assert pymupdf.open(p).page_count > 0
            elif p.suffix == ".docx":
                import docx
                docx.Document(str(p))
        print(f"split {s:10} ≤{lim:7d} B → {len(parts)} parts {sizes}", flush=True)
    except Exception as e:
        sfails += 1
        print(f"split {s:10} FAIL {e}", flush=True)
        traceback.print_exc()
print("split failures:", sfails)
sys.exit(1 if (fails or sfails) else 0)
