#!/usr/bin/env python3
"""
docconv.py — output-format conversion and size-based splitting
================================================================
Used by bot.py after a file has been translated:

    convert(src, dst, font=None)          EPUB/PDF/DOCX/TXT/MD/HTML  →  EPUB/PDF/DOCX/TXT/HTML
    split_file(path, max_bytes)           → [part1, part2, …]  (same format, ≤ max_bytes each)

Conversion goes through a tiny intermediate model (Book → Chapter → XHTML
fragment + image store) so every input format can be rendered to every output
format.  Splitting is done *natively* per format (EPUB spine subsets, PDF page
ranges, DOCX body slices, HTML body children, TXT lines) so nothing is lost —
the parts are ordinary files that open in any reader.

Only lxml is required.  PDF read/write needs PyMuPDF, DOCX *writing* needs
python-docx (DOCX reading is done with lxml directly).
"""

from __future__ import annotations

import hashlib
import html as htmlmod
import io
import logging
import posixpath
import re
import statistics
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

from lxml import etree
from lxml import html as lhtml

try:  # PDF read + write
    import pymupdf  # type: ignore
except Exception:  # pragma: no cover
    pymupdf = None

try:  # DOCX write
    import docx as pydocx  # type: ignore
    from docx.shared import Inches, Pt  # type: ignore
except Exception:  # pragma: no cover
    pydocx = None

log = logging.getLogger("docconv")


class ConvertError(Exception):
    """User-facing conversion / split failure."""


# extension → (kind, label).  Kinds: epub · pdf · docx · txt · html
FORMATS: Dict[str, Tuple[str, str]] = {
    ".epub": ("epub", "EPUB"),
    ".pdf": ("pdf", "PDF"),
    ".docx": ("docx", "DOCX"),
    ".txt": ("txt", "TXT"),
    ".md": ("txt", "Markdown"),
    ".html": ("html", "HTML"),
    ".htm": ("html", "HTML"),
    ".xhtml": ("html", "XHTML"),
}
# what the user may pick as *output*
OUTPUT_EXTS: Tuple[str, ...] = (".epub", ".pdf", ".docx", ".txt", ".html")


def kind_of(ext: str) -> str:
    try:
        return FORMATS[ext.lower()][0]
    except KeyError:
        raise ConvertError(f"Unsupported format {ext}")


def label_of(ext: str) -> str:
    return FORMATS.get(ext.lower(), ("", ext.lstrip(".").upper()))[1]


def available_outputs() -> List[str]:
    """Output formats that can actually be produced on this machine."""
    out = [".epub"]
    if pymupdf is not None:
        out.append(".pdf")
    if pydocx is not None:
        out.append(".docx")
    out += [".txt", ".html"]
    return out


def same_kind(a: str, b: str) -> bool:
    return kind_of(a) == kind_of(b)


# ═══════════════════════════════════════════════════════════════════════════
#  INTERMEDIATE MODEL
# ═══════════════════════════════════════════════════════════════════════════

_MAGIC = (
    (b"\x89PNG", ".png", "image/png"),
    (b"\xff\xd8", ".jpg", "image/jpeg"),
    (b"GIF8", ".gif", "image/gif"),
    (b"BM", ".bmp", "image/bmp"),
    (b"II*\x00", ".tif", "image/tiff"),
    (b"MM\x00*", ".tif", "image/tiff"),
)
_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def sniff_image(data: bytes, hint: str = "") -> Tuple[str, str]:
    """→ (ext, mime) from magic bytes, falling back to the hinted extension."""
    for magic, ext, mime in _MAGIC:
        if data.startswith(magic):
            return ext, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if data.lstrip()[:1] == b"<" and b"<svg" in data[:2048].lower():
        return ".svg", "image/svg+xml"
    hint = hint.lower()
    if hint in _MIME_BY_EXT:
        return (".jpg" if hint == ".jpeg" else hint), _MIME_BY_EXT[hint]
    return ".bin", "application/octet-stream"


class ImageStore:
    """De-duplicating image registry: bytes → stable name (img_0001.png)."""

    def __init__(self) -> None:
        self.images: Dict[str, bytes] = {}
        self.mime: Dict[str, str] = {}
        self._by_hash: Dict[str, str] = {}

    def add(self, data: bytes, hint: str = "") -> Optional[str]:
        if not data or len(data) < 64:
            return None
        h = hashlib.sha1(data).hexdigest()
        if h in self._by_hash:
            return self._by_hash[h]
        ext, mime = sniff_image(data, hint)
        if ext == ".bin":
            return None
        name = f"img_{len(self.images) + 1:04d}{ext}"
        self.images[name] = data
        self.mime[name] = mime
        self._by_hash[h] = name
        return name

    def total_bytes(self) -> int:
        return sum(len(v) for v in self.images.values())


@dataclass
class Chapter:
    title: str
    body: etree._Element          # <div> holding XHTML content; <img src> = ImageStore name

    def text(self) -> str:
        return " ".join(self.body.itertext()).strip()

    def image_names(self) -> List[str]:
        return [img.get("src") for img in self.body.iter("img") if img.get("src")]


@dataclass
class Book:
    title: str = ""
    author: str = ""
    lang: str = "en"
    chapters: List[Chapter] = field(default_factory=list)
    store: ImageStore = field(default_factory=ImageStore)
    cover: Optional[str] = None   # ImageStore name
    css: str = ""                 # original stylesheet(s) (EPUB / HTML sources)


# ═══════════════════════════════════════════════════════════════════════════
#  XHTML HELPERS
# ═══════════════════════════════════════════════════════════════════════════

VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
DROP_TAGS = {
    "script", "style", "noscript", "template", "iframe", "object", "embed", "video", "audio", "canvas",
    "form", "input", "button", "select", "textarea", "head", "title", "meta", "link", "base",
}
KEEP_ATTRS = {"src", "href", "style", "class", "colspan", "rowspan", "alt", "dir", "lang"}
_HEADING_RE = re.compile(r"^h[1-6]$")
_ws_collapse = re.compile(r"\s+")


def _strip_ns(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _localname_tree(root: etree._Element) -> None:
    """Rename every element to its lower-case local name and drop namespaced /
    event attributes, so the tree is plain XHTML."""
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        el.tag = _strip_ns(el.tag)
        for k in list(el.attrib):
            if "}" in k or ":" in k or k.lower().startswith("on") or k == "xmlns":
                del el.attrib[k]


def _drop(el: etree._Element) -> None:
    """Remove an element but keep its tail text."""
    parent = el.getparent()
    if parent is None:
        return
    if el.tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def _unwrap(el: etree._Element) -> None:
    """Replace an element by its children / text (keeps everything inline)."""
    parent = el.getparent()
    if parent is None:
        return
    idx = parent.index(el)
    prev = el.getprevious()
    if el.text:
        if prev is not None:
            prev.tail = (prev.tail or "") + el.text
        else:
            parent.text = (parent.text or "") + el.text
    children = list(el)
    for i, ch in enumerate(children):
        parent.insert(idx + i, ch)
    if el.tail:
        last = children[-1] if children else prev
        if last is not None:
            last.tail = (last.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def _fix_empty(root: etree._Element) -> None:
    """XML serialisation writes `<p/>` for empty elements; HTML parsers choke on
    that for non-void tags.  Give them an empty text node → `<p></p>`."""
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag not in VOID_TAGS and len(el) == 0 and el.text is None:
            el.text = ""


def clean_fragment(root: etree._Element, resolve_img: Callable[[str], Optional[str]]) -> None:
    """Normalise a parsed <body>-like element in place: plain tags, no
    scripts/styles/forms, <img src> mapped through resolve_img (returns an
    ImageStore name or None → image dropped), SVG <image> → <img>, internal
    links unwrapped, unknown attributes removed."""
    _localname_tree(root)
    for el in list(root.iter()):
        if el is root or el.getparent() is None:
            continue
        tag = el.tag if isinstance(el.tag, str) else ""
        if not tag:  # comments / PIs
            _drop(el)
        elif tag in DROP_TAGS:
            _drop(el)
        elif tag == "svg":
            img = None
            for sub in el.iter():
                if _strip_ns(sub.tag) == "image":
                    href = ""
                    for k, v in sub.attrib.items():
                        if k.endswith("href"):
                            href = v
                    name = resolve_img(href) if href else None
                    if name:
                        img = etree.Element("img", src=name)
                        break
            if img is not None:
                img.tail = el.tail
                el.getparent().replace(el, img)
            else:
                _drop(el)
    for el in list(root.iter("img")):
        name = resolve_img(el.get("src") or "")
        if name:
            alt = el.get("alt")
            el.attrib.clear()
            el.set("src", name)
            if alt:
                el.set("alt", alt)
        else:
            _drop(el)
    for el in list(root.iter("a")):
        href = el.get("href") or ""
        if not href.startswith(("http://", "https://", "mailto:")):
            _unwrap(el)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for k in list(el.attrib):
            if k not in KEEP_ATTRS:
                del el.attrib[k]
    _fix_empty(root)


def frag_to_xml(el: etree._Element) -> str:
    _fix_empty(el)
    return etree.tostring(el, method="xml", encoding="unicode")


def frag_to_html(el: etree._Element) -> str:
    return etree.tostring(el, method="html", encoding="unicode")


def first_heading(root: etree._Element, max_len: int = 150) -> str:
    for el in root.iter():
        if isinstance(el.tag, str) and _HEADING_RE.match(el.tag):
            t = _ws_collapse.sub(" ", " ".join(el.itertext())).strip()
            if t:
                return t[:max_len]
    return ""


def _has_content(root: etree._Element) -> bool:
    if root.find(".//img") is not None:
        return True
    return bool("".join(root.itertext()).strip())


def make_div(body: etree._Element) -> etree._Element:
    """Move text + children of a <body> into a fresh <div>."""
    div = etree.Element("div")
    div.text = body.text
    for ch in list(body):
        div.append(ch)
    return div


def _decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=|<\?xml[^>]+encoding\s*=", re.I)
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>")


def _parse_html_doc(raw: bytes) -> etree._Element:
    """Tolerant parse of an (X)HTML document → root <html> element.

    Files without a charset declaration are decoded by us first (UTF-8 →
    cp1252 → latin-1), otherwise lxml would guess latin-1 and mangle UTF-8."""
    if not _CHARSET_RE.search(raw[:4096]):
        text = _decode_text(raw)
        try:
            return lhtml.document_fromstring(text or "<html><body></body></html>")
        except Exception:
            pass
    try:
        return lhtml.document_fromstring(raw)
    except Exception:
        text = _XML_DECL_RE.sub("", _decode_text(raw), count=1)
        return lhtml.document_fromstring(text or "<html><body></body></html>")


def split_children_at(div: etree._Element, is_boundary: Callable[[etree._Element], bool]) -> List[etree._Element]:
    """Split a <div>'s children into several <div>s, starting a new one at every
    boundary element (the boundary itself begins the new group)."""
    groups: List[etree._Element] = []
    cur = etree.Element("div")
    cur.text = div.text
    for ch in list(div):
        if is_boundary(ch) and (len(cur) or (cur.text or "").strip()):
            groups.append(cur)
            cur = etree.Element("div")
        cur.append(ch)
    if len(cur) or (cur.text or "").strip():
        groups.append(cur)
    return groups


def _find_heading_level(div: etree._Element) -> Optional[str]:
    """Top-most heading tag that occurs ≥ 2× among direct children."""
    for tag in ("h1", "h2", "h3"):
        if sum(1 for ch in div if isinstance(ch.tag, str) and ch.tag == tag) >= 2:
            return tag
    return None


def _promote_to_chapters(book: Book, div: etree._Element, fallback_title: str) -> None:
    level = _find_heading_level(div)
    if level is None:
        if _has_content(div):
            book.chapters.append(Chapter(first_heading(div) or fallback_title, div))
        return
    for i, g in enumerate(split_children_at(div, lambda ch: isinstance(ch.tag, str) and ch.tag == level), 1):
        if _has_content(g):
            book.chapters.append(Chapter(first_heading(g) or f"{fallback_title} {i}", g))


# ═══════════════════════════════════════════════════════════════════════════
#  READERS  (file → Book)
# ═══════════════════════════════════════════════════════════════════════════

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def read_epub(src: Path) -> Book:
    book = Book()
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        lower = {n.lower(): n for n in names}

        def get(name: str) -> Optional[bytes]:
            n = names_map.get(name) or lower.get(name.lower())
            try:
                return z.read(n) if n else None
            except KeyError:
                return None

        names_map = {n: n for n in names}
        # ── locate OPF ──
        opf_path = None
        cont = get("META-INF/container.xml")
        if cont:
            try:
                croot = etree.fromstring(cont)
                rf = croot.find(".//c:rootfile", _NS)
                if rf is not None:
                    opf_path = rf.get("full-path")
            except Exception:
                pass
        if not opf_path or opf_path not in names:
            cands = [n for n in names if n.lower().endswith(".opf")]
            if not cands:
                raise ConvertError("EPUB has no OPF package file")
            opf_path = cands[0]
        opf_dir = posixpath.dirname(opf_path)
        opf = etree.fromstring(get(opf_path))

        def rel(href: str) -> str:
            href = unquote(href.split("#", 1)[0])
            return posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else posixpath.normpath(href)

        # ── metadata ──
        t = opf.find(".//dc:title", _NS)
        a = opf.find(".//dc:creator", _NS)
        lg = opf.find(".//dc:language", _NS)
        book.title = (t.text or "").strip() if t is not None else ""
        book.author = (a.text or "").strip() if a is not None else ""
        book.lang = (lg.text or "en").strip() if lg is not None else "en"

        # ── manifest / spine ──
        manifest: Dict[str, Tuple[str, str, str]] = {}  # id -> (path, media-type, properties)
        for item in opf.iter("{%s}item" % _NS["opf"]):
            manifest[item.get("id", "")] = (rel(item.get("href", "")), item.get("media-type", ""), item.get("properties", "") or "")
        spine_ids = [r.get("idref") for r in opf.iter("{%s}itemref" % _NS["opf"])]
        spine = [manifest[i][0] for i in spine_ids if i in manifest]
        if not spine:  # broken spine → every xhtml in manifest order
            spine = [p for p, m, _ in manifest.values() if "html" in m]
        seen: set = set()
        spine = [p for p in spine if not (p in seen or seen.add(p))]

        # ── CSS (concatenated, kept for HTML/EPUB output) ──
        css_parts = []
        for p, m, _ in manifest.values():
            if m == "text/css" or p.lower().endswith(".css"):
                raw = get(p)
                if raw:
                    css_parts.append(_decode_text(raw))
        book.css = "\n".join(css_parts)

        # ── cover ──
        cover_path = None
        for p, m, props in manifest.values():
            if "cover-image" in props.split():
                cover_path = p
        if cover_path is None:
            meta = opf.find(".//opf:meta[@name='cover']", _NS)
            if meta is not None and meta.get("content") in manifest:
                cover_path = manifest[meta.get("content")][0]
        if cover_path:
            raw = get(cover_path)
            if raw:
                book.cover = book.store.add(raw, Path(cover_path).suffix)

        # ── chapters ──
        for doc_path in spine:
            raw = get(doc_path)
            if not raw:
                continue
            doc_dir = posixpath.dirname(doc_path)

            def resolve(href: str, _dir=doc_dir) -> Optional[str]:
                if not href or href.startswith(("http://", "https://", "data:")):
                    return None
                p = posixpath.normpath(posixpath.join(_dir, unquote(href.split("#", 1)[0]))) if _dir else posixpath.normpath(unquote(href.split("#", 1)[0]))
                data = get(p)
                return book.store.add(data, Path(p).suffix) if data else None

            html_root = _parse_html_doc(raw)
            body = html_root.find("body")
            if body is None:
                body = html_root
            title = ""
            tt = html_root.find(".//title")
            if tt is not None and tt.text and tt.text.strip():
                title = tt.text.strip()
            div = make_div(body)
            clean_fragment(div, resolve)
            if not _has_content(div):
                continue
            book.chapters.append(Chapter(first_heading(div) or title or f"Chapter {len(book.chapters) + 1}", div))
    if not book.chapters:
        raise ConvertError("EPUB contains no readable chapters")
    if not book.title:
        book.title = src.stem
    return book


def read_html(src: Path) -> Book:
    raw = src.read_bytes()
    book = Book()
    root = _parse_html_doc(raw)
    tt = root.find(".//title")
    book.title = (tt.text or "").strip() if tt is not None and tt.text else src.stem
    book.css = "\n".join((s.text or "") for s in root.iter("style"))
    body = root.find("body")
    if body is None:
        body = root
    div = make_div(body)
    base = src.parent

    def resolve(href: str) -> Optional[str]:
        if not href:
            return None
        if href.startswith("data:"):
            try:
                import base64

                meta, b64 = href.split(",", 1)
                data = base64.b64decode(b64) if ";base64" in meta else unquote(b64).encode()
                return book.store.add(data)
            except Exception:
                return None
        if href.startswith(("http://", "https://")):
            return None
        p = base / unquote(href.split("#", 1)[0])
        try:
            if p.is_file() and p.stat().st_size < 20 * 1024 * 1024:
                return book.store.add(p.read_bytes(), p.suffix)
        except Exception:
            pass
        return None

    clean_fragment(div, resolve)
    if not _has_content(div):
        raise ConvertError("HTML file has no readable content")
    _promote_to_chapters(book, div, book.title or "Section")
    return book


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*"), r"<b>\1</b>"),
    (re.compile(r"__(.+?)__"), r"<b>\1</b>"),
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"<i>\1</i>"),
    (re.compile(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)"), r"<i>\1</i>"),
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
]


def _md_inline(s: str) -> str:
    s = htmlmod.escape(s, quote=False)
    for rx, rep in _MD_INLINE:
        s = rx.sub(rep, s)
    return s


def read_txt(src: Path, markdown: Optional[bool] = None) -> Book:
    """Plain text: blank line = paragraph break; a run of 1–3 short lines
    followed by a blank line that looks like 'Chapter N' / ALL CAPS becomes a
    heading.  Markdown: # headings, **bold**, *italic*, lists."""
    text = _decode_text(src.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
    if markdown is None:
        markdown = src.suffix.lower() == ".md"
    book = Book(title=src.stem)
    div = etree.Element("div")
    lines = text.split("\n")
    para: List[str] = []
    list_el: Optional[etree._Element] = None
    chapter_re = re.compile(r"^\s*(chapter|part|book|prologue|epilogue|अध्याय|भाग)\b.{0,60}$", re.I)

    def flush() -> None:
        nonlocal para, list_el
        if not para:
            return
        joined = " ".join(l.strip() for l in para)
        body = _md_inline(joined) if markdown else htmlmod.escape(joined, quote=False)
        is_head = False
        if not markdown and len(para) <= 2 and len(joined) <= 80:
            if chapter_re.match(joined) or (joined.isupper() and any(c.isalpha() for c in joined)):
                is_head = True
        tag = "h2" if is_head else "p"
        el = etree.fromstring(f"<{tag}>{body}</{tag}>", parser=etree.XMLParser(recover=True))
        if el is None:
            el = etree.Element(tag)
            el.text = joined
        div.append(el)
        para = []
        list_el = None

    for line in lines:
        if not line.strip():
            flush()
            list_el = None
            continue
        if markdown:
            m = _MD_HEADING.match(line)
            if m:
                flush()
                lvl = min(len(m.group(1)), 6)
                el = etree.fromstring(f"<h{lvl}>{_md_inline(m.group(2))}</h{lvl}>", parser=etree.XMLParser(recover=True))
                div.append(el if el is not None else etree.Element(f"h{lvl}"))
                continue
            lm = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", line)
            if lm:
                flush()
                tag = "ol" if lm.group(1)[0].isdigit() else "ul"
                if list_el is None or list_el.tag != tag:
                    list_el = etree.SubElement(div, tag)
                li = etree.fromstring(f"<li>{_md_inline(lm.group(2))}</li>", parser=etree.XMLParser(recover=True))
                list_el.append(li if li is not None else etree.Element("li"))
                continue
            if line.startswith(("---", "***", "___")) and len(set(line.strip())) == 1:
                flush()
                etree.SubElement(div, "hr")
                continue
        para.append(line)
    flush()
    if not _has_content(div):
        raise ConvertError("Text file is empty")
    # promote to chapters by headings (h1 for md, h2 for txt heuristics)
    level = _find_heading_level(div)
    if level:
        for i, g in enumerate(split_children_at(div, lambda ch: isinstance(ch.tag, str) and ch.tag == level), 1):
            if _has_content(g):
                book.chapters.append(Chapter(first_heading(g) or f"Part {i}", g))
    else:
        book.chapters.append(Chapter(book.title, div))
    return book


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PKG_R = "http://schemas.openxmlformats.org/package/2006/relationships"
_VML = "urn:schemas-microsoft-com:vml"


def read_docx(src: Path) -> Book:
    """DOCX → paragraphs / headings / lists / tables / inline images (lxml only)."""
    book = Book(title=src.stem)
    W = "{%s}" % _W
    with zipfile.ZipFile(src) as z:
        names = set(z.namelist())
        if "word/document.xml" not in names:
            raise ConvertError("Not a valid DOCX")
        doc = etree.fromstring(z.read("word/document.xml"))
        rels: Dict[str, str] = {}
        if "word/_rels/document.xml.rels" in names:
            for r in etree.fromstring(z.read("word/_rels/document.xml.rels")).iter("{%s}Relationship" % _PKG_R):
                tgt = r.get("Target", "")
                if r.get("TargetMode") == "External":
                    continue
                rels[r.get("Id", "")] = posixpath.normpath(posixpath.join("word", tgt)) if not tgt.startswith("/") else tgt.lstrip("/")
        # core title
        if "docProps/core.xml" in names:
            try:
                core = etree.fromstring(z.read("docProps/core.xml"))
                t = core.find(".//{http://purl.org/dc/elements/1.1/}title")
                if t is not None and t.text and t.text.strip():
                    book.title = t.text.strip()
                a = core.find(".//{http://purl.org/dc/elements/1.1/}creator")
                if a is not None and a.text:
                    book.author = a.text.strip()
            except Exception:
                pass
        # style → heading level map
        style_level: Dict[str, int] = {}
        style_is_list: set = set()
        if "word/styles.xml" in names:
            try:
                st = etree.fromstring(z.read("word/styles.xml"))
                for s in st.iter(W + "style"):
                    sid = s.get(W + "styleId", "")
                    nm = s.find(W + "name")
                    nm_v = (nm.get(W + "val", "") if nm is not None else "").lower()
                    m = re.match(r"^(heading|título|überschrift|titre)\s*(\d)$", nm_v)
                    if m:
                        style_level[sid] = int(m.group(2))
                    elif nm_v == "title":
                        style_level[sid] = 1
                    elif nm_v in ("subtitle",):
                        style_level[sid] = 2
                    elif "list" in nm_v:
                        style_is_list.add(sid)
            except Exception:
                pass

        def img_from_rid(rid: str) -> Optional[str]:
            p = rels.get(rid)
            if p and p in names:
                return book.store.add(z.read(p), Path(p).suffix)
            return None

        def run_html(r: etree._Element) -> str:
            """One <w:r> → inline (X)HTML string."""
            rpr = r.find(W + "rPr")
            b = i = u = s = False
            if rpr is not None:
                b = rpr.find(W + "b") is not None and (rpr.find(W + "b").get(W + "val", "true") not in ("0", "false"))
                i = rpr.find(W + "i") is not None and (rpr.find(W + "i").get(W + "val", "true") not in ("0", "false"))
                u = rpr.find(W + "u") is not None and rpr.find(W + "u").get(W + "val", "single") != "none"
                s = rpr.find(W + "strike") is not None
            out: List[str] = []
            for ch in r:
                tag = _strip_ns(ch.tag)
                if tag == "t":
                    out.append(htmlmod.escape(ch.text or "", quote=False))
                elif tag == "tab":
                    out.append("\u2003")
                elif tag in ("br", "cr"):
                    out.append("<br/>")
                elif tag in ("drawing", "pict", "object"):
                    for blip in ch.iter("{%s}blip" % _A):
                        rid = blip.get("{%s}embed" % _R) or blip.get("{%s}link" % _R)
                        name = img_from_rid(rid) if rid else None
                        if name:
                            out.append(f'<img src="{name}"/>')
                    for im in ch.iter("{%s}imagedata" % _VML):
                        rid = im.get("{%s}id" % _R)
                        name = img_from_rid(rid) if rid else None
                        if name:
                            out.append(f'<img src="{name}"/>')
                elif tag == "sym":
                    out.append("")
            txt = "".join(out)
            if not txt:
                return ""
            if s:
                txt = f"<s>{txt}</s>"
            if u:
                txt = f"<u>{txt}</u>"
            if i:
                txt = f"<i>{txt}</i>"
            if b:
                txt = f"<b>{txt}</b>"
            return txt

        def para_html(p: etree._Element) -> Tuple[str, str, bool]:
            """→ (tag, inner_html, is_list_item)"""
            ppr = p.find(W + "pPr")
            tag = "p"
            is_list = False
            if ppr is not None:
                ps = ppr.find(W + "pStyle")
                sid = ps.get(W + "val", "") if ps is not None else ""
                lvl = style_level.get(sid)
                if lvl is None:
                    m = re.match(r"^(?:Heading|heading|Titre|Ttulo)(\d)$", sid)
                    if m:
                        lvl = int(m.group(1))
                    elif sid.lower() == "title":
                        lvl = 1
                if lvl:
                    tag = f"h{min(lvl, 6)}"
                if ppr.find(W + "numPr") is not None or sid in style_is_list:
                    is_list = True
            parts: List[str] = []
            for el in p.iter():
                t = _strip_ns(el.tag)
                if t == "r" and _strip_ns(el.getparent().tag) in ("p", "hyperlink", "ins", "smartTag", "sdtContent", "fldSimple"):
                    parts.append(run_html(el))
            inner = "".join(parts)
            return tag, inner, is_list

        div = etree.Element("div")
        body = doc.find(W + "body")
        if body is None:
            raise ConvertError("DOCX has no body")
        cur_list: Optional[etree._Element] = None
        parser = etree.XMLParser(recover=True)

        def append_html(container: etree._Element, tag: str, inner: str) -> None:
            el = etree.fromstring(f"<{tag}>{inner}</{tag}>", parser=parser)
            if el is None:
                el = etree.Element(tag)
                el.text = re.sub(r"<[^>]+>", "", inner)
            container.append(el)

        def walk_block(el: etree._Element, container: etree._Element) -> None:
            nonlocal cur_list
            t = _strip_ns(el.tag)
            if t == "p":
                tag, inner, is_list = para_html(el)
                if not inner.strip() and "<img" not in inner:
                    cur_list = None
                    return
                if is_list and tag == "p":
                    if cur_list is None or cur_list.getparent() is not container:
                        cur_list = etree.SubElement(container, "ul")
                    append_html(cur_list, "li", inner)
                else:
                    cur_list = None
                    append_html(container, tag, inner)
            elif t == "tbl":
                cur_list = None
                table = etree.SubElement(container, "table")
                for tr in el.iter(W + "tr"):
                    row = etree.SubElement(table, "tr")
                    for tc in tr.findall(W + "tc"):
                        cell = etree.SubElement(row, "td")
                        span = tc.find(f"{W}tcPr/{W}gridSpan")
                        if span is not None and span.get(W + "val", "1") != "1":
                            cell.set("colspan", span.get(W + "val"))
                        for sub in tc:
                            walk_block(sub, cell)
                        if len(cell) == 0 and not (cell.text or "").strip():
                            cell.text = ""
            elif t in ("sdt", "sdtContent", "customXml", "ins", "moveTo"):
                for sub in el:
                    walk_block(sub, container)
            elif t == "sectPr":
                return

        for el in body:
            walk_block(el, div)

    if not _has_content(div):
        raise ConvertError("DOCX contains no readable text")
    _promote_to_chapters(book, div, book.title or "Section")
    return book


def read_pdf(src: Path) -> Book:
    """PDF → flowing text.  Blocks are classified as headings by font size,
    lines inside a block are joined (de-hyphenated), images extracted in place.
    Layout is *not* preserved (that is what PDF→PDF passthrough is for)."""
    if pymupdf is None:
        raise ConvertError("PDF support is not installed (pip install pymupdf)")
    doc = pymupdf.open(src)
    if doc.is_encrypted and not doc.authenticate(""):
        raise ConvertError("PDF is password-protected")
    book = Book(title=(doc.metadata or {}).get("title") or src.stem, author=(doc.metadata or {}).get("author") or "")
    div = etree.Element("div")
    # ── font-size statistics for heading detection ──
    sizes: List[float] = []
    pages_data = []
    for page in doc:
        d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
        pages_data.append(d)
        for b in d["blocks"]:
            if b["type"] == 0:
                for line in b["lines"]:
                    for s in line["spans"]:
                        if s["text"].strip():
                            sizes.extend([s["size"]] * len(s["text"].strip()))
    if not sizes and not any(b["type"] == 1 for d in pages_data for b in d["blocks"]):
        raise ConvertError("PDF has no selectable text (scanned image PDF)")
    body_size = statistics.median(sizes) if sizes else 11.0
    toc = doc.get_toc(simple=True)
    toc_pages = {p - 1 for _, _, p in toc if p > 0}
    n_pages = len(doc)
    # running header/footer detection: identical short lines on >30 % pages
    line_counts: Dict[str, int] = {}
    for d in pages_data:
        seen_page: set = set()
        for b in d["blocks"]:
            if b["type"] != 0:
                continue
            txt = " ".join("".join(s["text"] for s in l["spans"]) for l in b["lines"]).strip()
            key = re.sub(r"\d+", "#", txt)
            if txt and len(txt) < 80 and key not in seen_page:
                seen_page.add(key)
                line_counts[key] = line_counts.get(key, 0) + 1
    boiler = {k for k, c in line_counts.items() if n_pages >= 4 and c >= max(3, int(n_pages * 0.3))}

    pending: Optional[etree._Element] = None  # paragraph continuing across blocks / pages
    for pno, d in enumerate(pages_data):
        page = doc[pno]
        if pno in toc_pages and len(div):
            pending = None
        blocks = sorted(d["blocks"], key=lambda b: (round(b["bbox"][1] / 5), b["bbox"][0]))
        for b in blocks:
            if b["type"] == 1:  # image block
                try:
                    data = b.get("image")
                    if data:
                        w, h = b.get("width", 0), b.get("height", 0)
                        if w >= 40 and h >= 40:
                            name = book.store.add(data, "." + (b.get("ext") or "png"))
                            if name:
                                fig = etree.SubElement(div, "p")
                                etree.SubElement(fig, "img", src=name)
                                pending = None
                except Exception:
                    pass
                continue
            parts: List[str] = []
            span_sizes: List[float] = []
            bold_chars = 0
            total_chars = 0
            for line in b["lines"]:
                ltxt = ""
                for s in line["spans"]:
                    t = s["text"]
                    if not t.strip():
                        ltxt += t
                        continue
                    span_sizes.extend([s["size"]] * len(t.strip()))
                    total_chars += len(t.strip())
                    if s.get("flags", 0) & 16:
                        bold_chars += len(t.strip())
                    ltxt += t
                ltxt = ltxt.strip()
                if not ltxt:
                    continue
                if parts and parts[-1].endswith("-") and ltxt[:1].islower():
                    parts[-1] = parts[-1][:-1] + ltxt
                else:
                    parts.append(ltxt)
            text = _ws_collapse.sub(" ", " ".join(parts)).strip()
            if not text:
                continue
            key = re.sub(r"\d+", "#", text)
            if key in boiler or (len(text) <= 4 and text.replace(".", "").isdigit()):
                continue  # page numbers / running headers
            size = statistics.median(span_sizes) if span_sizes else body_size
            mostly_bold = total_chars and bold_chars / total_chars > 0.7
            is_heading = len(text) <= 120 and (size >= body_size * 1.25 or (mostly_bold and size >= body_size * 1.05 and len(text) <= 80))
            if is_heading:
                tag = "h1" if size >= body_size * 1.6 else "h2"
                el = etree.SubElement(div, tag)
                el.text = text
                pending = None
                continue
            # paragraph continuation: previous block ended mid-sentence and this starts lowercase
            if pending is not None and pending.text and not re.search(r"[.!?:;\"'”’)\]]\s*$", pending.text) and (text[:1].islower() or pending.text.endswith("-")):
                if pending.text.endswith("-"):
                    pending.text = pending.text[:-1] + text
                else:
                    pending.text = pending.text + " " + text
                continue
            el = etree.SubElement(div, "p")
            el.text = text
            pending = el
        # page break resets nothing (paragraphs can flow), except headings via TOC handled above
    doc.close()
    if not _has_content(div):
        raise ConvertError("PDF contains no readable text")
    # chapters: prefer the PDF outline titles when present, otherwise headings
    _promote_to_chapters(book, div, book.title or "Section")
    return book


def read_any(src: Path) -> Book:
    kind = kind_of(src.suffix)
    if kind == "epub":
        return read_epub(src)
    if kind == "html":
        return read_html(src)
    if kind == "txt":
        return read_txt(src)
    if kind == "docx":
        return read_docx(src)
    if kind == "pdf":
        return read_pdf(src)
    raise ConvertError(f"Unsupported input {src.suffix}")


# ═══════════════════════════════════════════════════════════════════════════
#  WRITERS  (Book → file)
# ═══════════════════════════════════════════════════════════════════════════

BASE_CSS = """
body { font-family: serif; line-height: 1.5; margin: 1em; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.2em 0 0.6em; page-break-after: avoid; }
p { margin: 0 0 0.8em; text-align: justify; }
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; width: 100%; margin: 0.8em 0; }
td, th { border: 1px solid #999; padding: 0.3em 0.5em; vertical-align: top; }
blockquote { margin: 0.8em 2em; font-style: italic; }
pre, code { font-family: monospace; white-space: pre-wrap; }
hr { border: 0; border-top: 1px solid #999; margin: 1.5em 0; }
"""

_XHTML_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n'
    '<head><meta charset="utf-8"/><title>{title}</title><link rel="stylesheet" type="text/css" href="../styles/style.css"/></head>\n'
    '<body>\n{body}\n</body>\n</html>\n'
)


def _rewrite_img_src(div: etree._Element, fn: Callable[[str], str]) -> etree._Element:
    """Deep-copy the fragment and rewrite <img src> (ImageStore name → path)."""
    import copy

    d = copy.deepcopy(div)
    for img in d.iter("img"):
        img.set("src", fn(img.get("src") or ""))
    return d


def _safe_css(css: str) -> str:
    # drop @import/url() to external resources and font-face pointing to files we don't ship
    css = re.sub(r"@import[^;]+;", "", css)
    css = re.sub(r"@font-face\s*{[^}]*}", "", css, flags=re.S)
    css = re.sub(r"url\([^)]*\)", "none", css)
    return css


def write_epub(book: Book, dst: Path) -> None:
    uid = f"urn:uuid:{uuid.uuid4()}"
    lang = book.lang or "en"
    title = book.title or dst.stem
    with zipfile.ZipFile(dst, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
            compress_type=zipfile.ZIP_DEFLATED,
        )
        z.writestr("OEBPS/styles/style.css", BASE_CSS + "\n" + _safe_css(book.css), compress_type=zipfile.ZIP_DEFLATED)
        manifest: List[str] = ['<item id="css" href="styles/style.css" media-type="text/css"/>',
                               '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                               '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
        spine: List[str] = []
        nav_items: List[str] = []
        ncx_points: List[str] = []
        used_images: set = set()
        for i, ch in enumerate(book.chapters, 1):
            fname = f"text/ch{i:04d}.xhtml"
            frag = _rewrite_img_src(ch.body, lambda n: f"../images/{n}")
            used_images.update(ch.image_names())
            body = frag_to_xml(frag)
            z.writestr("OEBPS/" + fname, _XHTML_HEAD.format(lang=lang, title=htmlmod.escape(ch.title or title), body=body), compress_type=zipfile.ZIP_DEFLATED)
            manifest.append(f'<item id="ch{i}" href="{fname}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="ch{i}"/>')
            t = htmlmod.escape(ch.title or f"Chapter {i}")
            nav_items.append(f'<li><a href="{fname}">{t}</a></li>')
            ncx_points.append(f'<navPoint id="np{i}" playOrder="{i}"><navLabel><text>{t}</text></navLabel><content src="{fname}"/></navPoint>')
        cover_item = ""
        if book.cover:
            used_images.add(book.cover)
        for name in sorted(used_images):
            data = book.store.images.get(name)
            if not data:
                continue
            z.writestr(f"OEBPS/images/{name}", data, compress_type=zipfile.ZIP_STORED if book.store.mime[name] != "image/svg+xml" else zipfile.ZIP_DEFLATED)
            props = ' properties="cover-image"' if name == book.cover else ""
            manifest.append(f'<item id="img_{Path(name).stem}" href="images/{name}" media-type="{book.store.mime[name]}"{props}/>')
            if name == book.cover:
                cover_item = f'<meta name="cover" content="img_{Path(name).stem}"/>'
        nav = (
            '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">'
            f'<head><meta charset="utf-8"/><title>{htmlmod.escape(title)}</title></head><body>'
            f'<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>{"".join(nav_items)}</ol></nav></body></html>'
        )
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        ncx = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            f'<head><meta name="dtb:uid" content="{uid}"/><meta name="dtb:depth" content="1"/></head>'
            f'<docTitle><text>{htmlmod.escape(title)}</text></docTitle><navMap>{"".join(ncx_points)}</navMap></ncx>'
        )
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        author = f"<dc:creator>{htmlmod.escape(book.author)}</dc:creator>" if book.author else ""
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:identifier id="bookid">{uid}</dc:identifier><dc:title>{htmlmod.escape(title)}</dc:title>{author}'
            f'<dc:language>{lang}</dc:language>'
            f'<meta property="dcterms:modified">{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}</meta>{cover_item}'
            f'</metadata><manifest>{"".join(manifest)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>'
        )
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)


def write_html(book: Book, dst: Path) -> None:
    """Single self-contained HTML file (images embedded as data URIs)."""
    import base64

    def data_uri(name: str) -> str:
        data = book.store.images.get(name)
        if not data:
            return ""
        return f"data:{book.store.mime[name]};base64,{base64.b64encode(data).decode()}"

    parts = [
        "<!DOCTYPE html>\n<html lang=\"%s\">\n<head>\n<meta charset=\"utf-8\"/>\n" % htmlmod.escape(book.lang or "en"),
        f"<title>{htmlmod.escape(book.title or dst.stem)}</title>\n",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>\n",
        f"<style>{BASE_CSS}\n{_safe_css(book.css)}</style>\n</head>\n<body>\n",
    ]
    if book.cover and book.cover in book.store.images:
        parts.append(f'<div class="cover" style="text-align:center"><img src="{data_uri(book.cover)}" alt="cover"/></div>\n')
    for i, ch in enumerate(book.chapters, 1):
        frag = _rewrite_img_src(ch.body, data_uri)
        frag.set("class", "chapter")
        frag.set("id", f"ch{i}")
        parts.append(frag_to_html(frag))
        parts.append("\n")
    parts.append("</body>\n</html>\n")
    dst.write_text("".join(parts), encoding="utf-8")


def _block_text_lines(div: etree._Element) -> List[str]:
    """Flatten a fragment to plain-text lines (paragraph per block, blank line between)."""
    out: List[str] = []
    buf: List[str] = []
    block = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "tr", "table", "ul", "ol", "hr", "section", "article", "figure", "figcaption", "dt", "dd", "header", "footer", "aside"}

    def flush() -> None:
        s = _ws_collapse.sub(" ", "".join(buf)).strip()
        if s:
            out.append(s)
            out.append("")
        buf.clear()

    def walk(el: etree._Element) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "img":
            pass
        if tag in block:
            flush()
        if tag == "br":
            buf.append("\n")
        if tag == "hr":
            out.append("* * *")
            out.append("")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = _ws_collapse.sub(" ", " ".join(el.itertext())).strip()
            if t:
                out.append(t.upper() if tag == "h1" else t)
                out.append("")
            if el.tail:
                buf.append(el.tail)
            return
        if tag == "li":
            t = _ws_collapse.sub(" ", " ".join(el.itertext())).strip()
            if t:
                out.append("• " + t)
            if el.tail:
                buf.append(el.tail)
            return
        if tag == "td" or tag == "th":
            buf.append(" | ")
        if el.text and tag != "pre":
            buf.append(el.text)
        elif el.text:
            buf.append(el.text)
        for ch in el:
            walk(ch)
        if tag in block:
            flush()
        if el.tail:
            buf.append(el.tail)

    walk(div)
    flush()
    # collapse repeated blank lines
    res: List[str] = []
    for line in out:
        if line == "" and res and res[-1] == "":
            continue
        res.append(line)
    while res and res[-1] == "":
        res.pop()
    return res


def write_txt(book: Book, dst: Path) -> None:
    lines: List[str] = []
    if book.title:
        lines += [book.title, "=" * min(len(book.title), 60), ""]
    if book.author:
        lines += [book.author, ""]
    for ch in book.chapters:
        body = _block_text_lines(ch.body)
        if ch.title and (not body or body[0].strip().lower() != ch.title.strip().lower()):
            lines += ["", ch.title, "-" * min(len(ch.title), 60), ""]
        elif lines:
            lines.append("")
        lines += body
    dst.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def write_docx(book: Book, dst: Path) -> None:
    if pydocx is None:
        raise ConvertError("DOCX output is not installed (pip install python-docx)")
    import io

    d = pydocx.Document()
    d.core_properties.title = book.title or dst.stem
    if book.author:
        d.core_properties.author = book.author
    section = d.sections[0]
    max_w = section.page_width - section.left_margin - section.right_margin

    def add_image(container, name: str) -> None:
        data = book.store.images.get(name)
        if not data or book.store.mime.get(name) == "image/svg+xml":
            return
        try:
            para = container.add_paragraph()
            para.alignment = 1
            para.add_run().add_picture(io.BytesIO(data), width=max_w if max_w < Inches(6.5) else Inches(6.5))
        except Exception as e:  # unsupported codec etc.
            log.debug("docx image skipped: %s", e)

    def add_inline(para, el: etree._Element, fmt: Dict[str, bool], container) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        f = dict(fmt)
        if tag in ("b", "strong"):
            f["bold"] = True
        if tag in ("i", "em", "cite", "dfn", "var"):
            f["italic"] = True
        if tag in ("u", "ins"):
            f["underline"] = True
        if tag in ("s", "strike", "del"):
            f["strike"] = True
        if tag in ("code", "kbd", "samp", "tt"):
            f["mono"] = True
        if tag == "sup":
            f["sup"] = True
        if tag == "sub":
            f["sub"] = True
        if tag == "br":
            para.add_run().add_break()
        elif tag == "img":
            add_image(container, el.get("src") or "")
        elif el.text:
            run = para.add_run(_ws_collapse.sub(" ", el.text))
            run.bold = f.get("bold")
            run.italic = f.get("italic")
            run.underline = f.get("underline")
            run.font.strike = f.get("strike")
            run.font.superscript = f.get("sup")
            run.font.subscript = f.get("sub")
            if f.get("mono"):
                run.font.name = "Courier New"
        for ch in el:
            add_inline(para, ch, f, container)
            if ch.tail:
                run = para.add_run(_ws_collapse.sub(" ", ch.tail))
                run.bold = f.get("bold")
                run.italic = f.get("italic")
                run.underline = f.get("underline")

    def block_has_text(el) -> bool:
        return bool("".join(el.itertext()).strip()) or el.find(".//img") is not None

    def add_block(el: etree._Element, container, list_style: Optional[str] = None) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            p = container.add_heading("", level=int(tag[1]))
            add_inline(p, el, {}, container)
            _strip_leading_ws(p)
        elif tag in ("ul", "ol"):
            style = "List Bullet" if tag == "ul" else "List Number"
            for li in el:
                if isinstance(li.tag, str) and li.tag == "li":
                    add_block(li, container, style)
        elif tag == "li":
            nested = [c for c in el if isinstance(c.tag, str) and c.tag in ("ul", "ol")]
            try:
                p = container.add_paragraph(style=list_style or "List Bullet")
            except Exception:
                p = container.add_paragraph()
            tmp = etree.Element("span")
            tmp.text = el.text
            for c in el:
                if c not in nested:
                    import copy

                    tmp.append(copy.deepcopy(c))
            add_inline(p, tmp, {}, container)
            for n in nested:
                add_block(n, container)
        elif tag == "table":
            rows = [r for r in el.iter("tr")]
            if not rows:
                return
            ncols = max(sum(int(c.get("colspan", "1") or 1) for c in r if isinstance(c.tag, str) and c.tag in ("td", "th")) for r in rows) or 1
            table = container.add_table(rows=0, cols=ncols)
            try:
                table.style = "Table Grid"
            except Exception:
                pass
            for r in rows:
                cells = table.add_row().cells
                ci = 0
                for c in r:
                    if not isinstance(c.tag, str) or c.tag not in ("td", "th"):
                        continue
                    if ci >= ncols:
                        break
                    cell = cells[ci]
                    cell.paragraphs[0].text = ""
                    blocks = [x for x in c if isinstance(x.tag, str) and x.tag in _BLOCKS]
                    if blocks or not c.text:
                        if (c.text or "").strip():
                            add_inline(cell.paragraphs[0], _text_only(c), {}, cell)
                        for b in blocks:
                            add_block(b, cell)
                    else:
                        add_inline(cell.paragraphs[0], c, {"bold": c.tag == "th"}, cell)
                    ci += int(c.get("colspan", "1") or 1)
        elif tag == "hr":
            container.add_paragraph("* * *").alignment = 1
        elif tag == "img":
            add_image(container, el.get("src") or "")
        elif tag == "pre":
            p = container.add_paragraph()
            run = p.add_run("".join(el.itertext()))
            run.font.name = "Courier New"
            run.font.size = Pt(9)
        elif tag in ("div", "section", "article", "aside", "header", "footer", "main", "nav", "figure", "body", "blockquote", "center", "dl"):
            # container: text directly inside becomes a paragraph, block children recurse
            if (el.text or "").strip():
                p = container.add_paragraph()
                if tag == "blockquote":
                    p.paragraph_format.left_indent = Inches(0.5)
                p.add_run(_ws_collapse.sub(" ", el.text).strip())
            inline_buf: List[etree._Element] = []

            def flush_inline() -> None:
                if not inline_buf:
                    return
                tmp = etree.Element("span")
                import copy

                for x in inline_buf:
                    tmp.append(copy.deepcopy(x))
                if block_has_text(tmp):
                    p = container.add_paragraph()
                    if tag == "blockquote":
                        p.paragraph_format.left_indent = Inches(0.5)
                    add_inline(p, tmp, {}, container)
                inline_buf.clear()

            for ch in el:
                if isinstance(ch.tag, str) and ch.tag in _BLOCKS:
                    flush_inline()
                    add_block(ch, container)
                    if (ch.tail or "").strip():
                        t = etree.Element("span")
                        t.text = ch.tail
                        inline_buf.append(t)
                else:
                    inline_buf.append(ch)
            flush_inline()
        else:  # p, dt, dd, figcaption, address, span-ish blocks
            if not block_has_text(el):
                return
            p = container.add_paragraph()
            if tag == "figcaption":
                p.alignment = 1
            add_inline(p, el, {}, container)
            _strip_leading_ws(p)

    if book.title:
        d.add_heading(book.title, level=0)
    if book.author:
        d.add_paragraph(book.author).alignment = 1
    if book.cover:
        add_image(d, book.cover)
    for i, ch in enumerate(book.chapters):
        if i or book.title or book.cover:
            d.add_page_break()
        add_block(ch.body, d)
    d.save(dst)


_BLOCKS = {"p", "div", "section", "article", "aside", "header", "footer", "main", "nav", "figure", "figcaption",
           "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd", "blockquote", "pre",
           "table", "hr", "address", "center", "img"}


def _text_only(el: etree._Element) -> etree._Element:
    t = etree.Element("span")
    t.text = el.text
    return t


def _strip_leading_ws(p) -> None:
    try:
        if p.runs and p.runs[0].text:
            p.runs[0].text = p.runs[0].text.lstrip()
    except Exception:
        pass


# ── PDF ─────────────────────────────────────────────────────────────────────

PDF_CSS = """
body { font-size: 11pt; line-height: 1.4; }
h1 { font-size: 20pt; margin: 18pt 0 10pt; page-break-after: avoid; }
h2 { font-size: 16pt; margin: 14pt 0 8pt; page-break-after: avoid; }
h3 { font-size: 13pt; margin: 12pt 0 6pt; }
h4, h5, h6 { font-size: 11.5pt; margin: 10pt 0 5pt; }
p { margin: 0 0 6pt; text-align: justify; }
li { margin: 0 0 3pt; }
img { max-width: 100%; }
blockquote { margin: 6pt 24pt; }
pre, code { font-family: monospace; font-size: 9pt; }
td, th { padding: 2pt 4pt; }
"""


def _image_dims(data: bytes) -> Optional[Tuple[int, int]]:
    """Pixel size of an image via PyMuPDF (None if undecodable)."""
    try:
        pix = pymupdf.Pixmap(data)
        w, h = pix.width, pix.height
        pix = None
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _flatten_tables(root: etree._Element) -> None:
    """PyMuPDF's Story never converges when a <table> is followed by a
    page-break (verified on 1.28) — so for PDF output every table row becomes
    a paragraph with ' | ' between the cells."""
    for tbl in list(root.iter("table")):
        parent = tbl.getparent()
        if parent is None:
            continue
        idx = parent.index(tbl)
        rows = []
        for tr in tbl.iter("tr"):
            cells = [_ws_collapse.sub(" ", " ".join(c.itertext())).strip() for c in tr if isinstance(c.tag, str) and c.tag in ("td", "th")]
            if any(cells):
                p = etree.Element("p")
                p.text = "  |  ".join(cells)
                rows.append(p)
        if rows:
            rows[-1].tail = tbl.tail
        for j, p in enumerate(rows):
            parent.insert(idx + j, p)
        parent.remove(tbl)


def _pdf_story_html(book: Book, with_images: bool, box_w: float, box_h: float) -> Tuple[str, Dict[str, bytes]]:
    """Build the Story HTML.  Images get explicit width/height (fitted into the
    text box) — without them PyMuPDF's layout engine can loop forever on
    oversized pictures."""
    parts: List[str] = []
    used: Dict[str, bytes] = {}
    max_w, max_h = box_w * 0.98, box_h * 0.9

    def fit(name: str) -> Optional[Tuple[int, int]]:
        data = book.store.images.get(name)
        if not data or book.store.mime.get(name) == "image/svg+xml":
            return None
        dims = _image_dims(data)
        if not dims:
            return None
        w, h = dims
        scale = min(max_w / w, max_h / h, 1.0)  # px ≈ pt at 72 dpi
        return max(1, int(w * scale)), max(1, int(h * scale))

    if with_images and book.cover and book.cover in book.store.images:
        d = fit(book.cover)
        if d:
            used[book.cover] = book.store.images[book.cover]
            parts.append(f'<div id="cover" style="text-align:center"><img src="{book.cover}" width="{d[0]}" height="{d[1]}"/></div>')
    for i, ch in enumerate(book.chapters, 1):
        frag = _rewrite_img_src(ch.body, lambda n: n)
        _flatten_tables(frag)
        for img in list(frag.iter("img")):
            name = img.get("src") or ""
            d = fit(name) if with_images else None
            if d:
                used[name] = book.store.images[name]
                img.set("width", str(d[0]))
                img.set("height", str(d[1]))
                for k in list(img.attrib):
                    if k not in ("src", "width", "height"):
                        del img.attrib[k]
            else:
                _drop(img)
        frag.set("id", f"ch{i}")
        if parts:
            frag.set("style", "page-break-before: always")
        parts.append(frag_to_html(frag))
    return f'<html><head><meta charset="utf-8"/></head><body>{"".join(parts)}</body></html>', used


def _pdf_render(book: Book, dst: Path, font: Optional[Path], page: str, with_images: bool) -> Dict[str, int]:
    """One Story rendering pass → dst.  Returns {chapter_id: first_page}.
    Raises ConvertError when the layout engine does not converge."""
    rect = pymupdf.paper_rect(page)
    where = rect + (54, 54, -54, -60)
    html_doc, used = _pdf_story_html(book, with_images, where.width, where.height)
    archive = pymupdf.Archive()
    css = PDF_CSS
    if font is not None and font.exists():
        archive.add(str(font.parent))
        css = (
            f"@font-face {{ font-family: tr; src: url({font.name}); }}\n"
            "body, p, li, h1, h2, h3, h4, h5, h6, td, th, blockquote, div {{ font-family: tr, sans-serif; }}\n"
        ).replace("{{", "{").replace("}}", "}") + css
    for name, data in used.items():
        try:
            archive.add(data, name)
        except Exception:
            pass
    story = pymupdf.Story(html=html_doc, user_css=css, archive=archive)
    # a generous upper bound: ~1 page per 1.5k chars + images + chapters
    total_chars = sum(len(ch.text()) for ch in book.chapters)
    max_pages = 50 + total_chars // 1200 + len(used) * 2 + len(book.chapters) * 2
    chapter_page: Dict[str, int] = {}
    state = {"pno": 0}

    def rec(pos) -> None:
        if pos.id and pos.id.startswith("ch") and pos.id not in chapter_page and pos.depth <= 2:
            chapter_page[pos.id] = state["pno"] + 1

    writer = pymupdf.DocumentWriter(str(dst))
    pno = 0
    more = True
    stuck = 0
    last_filled = None
    while more:
        dev = writer.begin_page(rect)
        more, filled = story.place(where)
        state["pno"] = pno
        story.element_positions(rec)
        story.draw(dev)
        writer.end_page()
        pno += 1
        # convergence guard: identical fill rects page after page = layout loop
        key = tuple(round(x, 1) for x in filled)
        stuck = stuck + 1 if key == last_filled else 0
        last_filled = key
        if pno > max_pages or stuck > 40:
            writer.close()
            raise ConvertError("PDF layout did not converge")
    writer.close()
    return chapter_page


def write_pdf(book: Book, dst: Path, font: Optional[Path] = None, page: str = "a4") -> bool:
    """Reflowed PDF via PyMuPDF Story: chapters start on a new page, TOC
    bookmarks added, fonts subsetted.

    Returns True when images were included, False when the renderer had to
    fall back to a text-only document (layout engine did not converge)."""
    if pymupdf is None:
        raise ConvertError("PDF output is not installed (pip install pymupdf)")
    with_images = bool(book.store.images)
    tmp = dst.with_suffix(".render.pdf")
    try:
        chapter_page = _pdf_render(book, tmp, font, page, with_images)
    except ConvertError as e:
        if not with_images:
            raise
        log.warning("PDF render with images failed (%s) — retrying text-only", e)
        tmp.unlink(missing_ok=True)
        with_images = False
        chapter_page = _pdf_render(book, tmp, font, page, False)
    # bookmarks + metadata + font subsetting
    doc = pymupdf.open(str(tmp))
    toc = []
    for i, ch in enumerate(book.chapters, 1):
        p = chapter_page.get(f"ch{i}")
        if p and p <= len(doc):
            toc.append([1, (ch.title or f"Chapter {i}")[:200], p])
    if toc:
        try:
            doc.set_toc(toc)
        except Exception as e:
            log.debug("set_toc failed: %s", e)
    doc.set_metadata({"title": book.title or dst.stem, "author": book.author or "", "producer": "docconv", "creator": "EPUB Translator Bot"})
    try:
        doc.subset_fonts()
    except Exception as e:
        log.debug("subset_fonts failed: %s", e)
    doc.save(str(dst), garbage=4, deflate=True, deflate_images=True, deflate_fonts=True)
    doc.close()
    tmp.unlink(missing_ok=True)
    return with_images


def write_any(book: Book, dst: Path, font: Optional[Path] = None) -> List[str]:
    """Write the book; returns a list of user-facing notes (e.g. images dropped)."""
    kind = kind_of(dst.suffix)
    notes: List[str] = []
    if kind == "epub":
        write_epub(book, dst)
    elif kind == "html":
        write_html(book, dst)
    elif kind == "txt":
        write_txt(book, dst)
        if book.store.images:
            notes.append("images are not included in TXT output")
    elif kind == "docx":
        write_docx(book, dst)
    elif kind == "pdf":
        if not write_pdf(book, dst, font) and book.store.images:
            notes.append("PDF was rendered text-only (images could not be laid out)")
    else:
        raise ConvertError(f"Unsupported output {dst.suffix}")
    return notes


def convert(src: Path, dst: Path, font: Optional[Path] = None, lang: Optional[str] = None) -> List[str]:
    """Convert `src` to the format given by `dst`'s extension.  Same kind →
    plain copy (no lossy round-trip).  Returns user-facing notes (may be empty)."""
    if same_kind(src.suffix, dst.suffix):
        if src.resolve() != dst.resolve():
            import shutil

            shutil.copyfile(src, dst)
        return []
    book = read_any(src)
    if lang:
        book.lang = lang
    return write_any(book, dst, font)


# ═══════════════════════════════════════════════════════════════════════════
#  SPLITTING  (one file → N files of the same format, each ≤ max_bytes)
# ═══════════════════════════════════════════════════════════════════════════

MIN_SPLIT_BYTES = 256 * 1024          # refuse silly limits (< 256 KB)


def part_name(src: Path, i: int, n: int) -> str:
    return f"{src.stem} (part {i} of {n}){src.suffix}"


def _finalize_parts(src: Path, tmp_parts: List[Path], out_dir: Path) -> List[Path]:
    n = len(tmp_parts)
    out: List[Path] = []
    for i, p in enumerate(tmp_parts, 1):
        dst = out_dir / part_name(src, i, n)
        p.replace(dst)
        out.append(dst)
    return out


def _pack(
    n_items: int,
    estimate: Callable[[int], int],
    build: Callable[[int, int, Path], None],
    max_bytes: int,
    tmp_dir: Path,
    fixed_overhead: int = 0,
) -> List[Path]:
    """Sequential bin-packing with verification.

    estimate(i) → rough byte cost of item i; build(start, end, path) writes
    items [start, end) to path.  After building, the real size is checked and
    the range shrunk until it fits (a single item that still doesn't fit is
    emitted as-is — nothing more can be done at this level)."""
    parts: List[Path] = []
    budget = max(int(max_bytes * 0.9) - fixed_overhead, max_bytes // 4)
    start = 0
    k = 0
    while start < n_items:
        end = start
        acc = 0
        while end < n_items and (end == start or acc + estimate(end) <= budget):
            acc += estimate(end)
            end += 1
        # never let a big item sneak in when we already have several
        while True:
            k += 1
            path = tmp_dir / f".part{k:04d}"
            build(start, end, path)
            size = path.stat().st_size
            if size <= max_bytes or end - start <= 1:
                break
            # shrink proportionally, at least by one item
            new_len = max(1, min(end - start - 1, int((end - start) * max_bytes / size * 0.9)))
            end = start + new_len
            path.unlink(missing_ok=True)
        parts.append(path)
        start = end
    return parts


# ── TXT ────────────────────────────────────────────────────────────────────

def split_txt(src: Path, max_bytes: int, out_dir: Path) -> List[Path]:
    text = _decode_text(src.read_bytes())
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parts: List[Path] = []
    buf: List[str] = []
    size = 0
    k = 0

    def flush() -> None:
        nonlocal buf, size, k
        if not buf:
            return
        k += 1
        p = out_dir / f".part{k:04d}"
        p.write_bytes(nl.join(buf).encode("utf-8"))
        parts.append(p)
        buf, size = [], 0

    last_blank = -1
    for line in lines:
        b = len(line.encode("utf-8")) + len(nl)
        if size + b > max_bytes and buf:
            # prefer cutting at the last blank line (paragraph boundary)
            if last_blank > len(buf) // 2:
                tail = buf[last_blank + 1:]
                buf = buf[: last_blank + 1]
                flush()
                buf = tail
                size = sum(len(x.encode("utf-8")) + len(nl) for x in buf)
            else:
                flush()
            last_blank = -1
        if not line.strip():
            last_blank = len(buf)
        buf.append(line)
        size += b
    flush()
    return _finalize_parts(src, parts, out_dir)


# ── HTML ───────────────────────────────────────────────────────────────────

def _explode_big(div: etree._Element, max_bytes: int) -> List[etree._Element]:
    """Children of `div` as a list; any child whose serialised size exceeds
    max_bytes/2 is replaced by its own children (one level) so packing has
    small enough items."""
    items: List[etree._Element] = []
    if (div.text or "").strip():
        p = etree.Element("p")
        p.text = div.text
        items.append(p)
    for ch in list(div):
        if isinstance(ch.tag, str) and ch.tag in ("div", "section", "article", "body") and len(etree.tostring(ch, encoding="utf-8")) > max_bytes // 2 and len(ch):
            items.extend(_explode_big(ch, max_bytes))
        else:
            items.append(ch)
    return items


def split_html(src: Path, max_bytes: int, out_dir: Path) -> List[Path]:
    raw = src.read_bytes()
    root = _parse_html_doc(raw)
    body = root.find("body")
    if body is None:
        raise ConvertError("HTML has no <body>")
    head = root.find("head")
    head_html = etree.tostring(head, method="html", encoding="unicode") if head is not None else "<head><meta charset=\"utf-8\"/></head>"
    html_attrs = " ".join(f'{k}="{htmlmod.escape(v, quote=True)}"' for k, v in root.attrib.items())
    body_attrs = " ".join(f'{k}="{htmlmod.escape(v, quote=True)}"' for k, v in body.attrib.items())
    items = _explode_big(body, max_bytes)
    ser = [etree.tostring(it, method="html", encoding="utf-8") + (it.tail or "").encode("utf-8") for it in items]
    overhead = len(head_html.encode("utf-8")) + 200

    def build(s: int, e: int, path: Path) -> None:
        with open(path, "wb") as f:
            f.write(f"<!DOCTYPE html>\n<html {html_attrs}>\n".encode("utf-8"))
            f.write(head_html.encode("utf-8"))
            f.write(f"\n<body {body_attrs}>\n".encode("utf-8"))
            for x in ser[s:e]:
                f.write(x)
                f.write(b"\n")
            f.write(b"</body>\n</html>\n")

    parts = _pack(len(ser), lambda i: len(ser[i]), build, max_bytes, out_dir, overhead)
    return _finalize_parts(src, parts, out_dir)


# ── EPUB ───────────────────────────────────────────────────────────────────

def _split_chapter(ch: Chapter, max_bytes: int) -> List[Chapter]:
    """Cut one oversized chapter into consecutive pieces (by top-level blocks)."""
    items = _explode_big(ch.body, max_bytes)
    pieces: List[Chapter] = []
    cur = etree.Element("div")
    cur_size = 0
    for it in items:
        s = len(etree.tostring(it, encoding="utf-8"))
        if cur_size + s > max_bytes and len(cur):
            pieces.append(Chapter(f"{ch.title} ({len(pieces) + 1})", cur))
            cur = etree.Element("div")
            cur_size = 0
        cur.append(it)
        cur_size += s
    if len(cur) or (cur.text or "").strip():
        pieces.append(Chapter(f"{ch.title} ({len(pieces) + 1})" if pieces else ch.title, cur))
    return pieces or [ch]


def split_epub(src: Path, max_bytes: int, out_dir: Path) -> List[Path]:
    book = read_epub(src)
    # text compresses ~3-4×, images don't
    text_budget = max_bytes // 3
    chapters: List[Chapter] = []
    for ch in book.chapters:
        xml_size = len(etree.tostring(ch.body, encoding="utf-8"))
        if xml_size > text_budget:
            chapters.extend(_split_chapter(ch, text_budget))
        else:
            chapters.append(ch)
    cover_size = len(book.store.images.get(book.cover, b"")) if book.cover else 0

    def estimate(i: int) -> int:
        ch = chapters[i]
        s = len(etree.tostring(ch.body, encoding="utf-8")) // 3 + 600
        for name in set(ch.image_names()):
            s += len(book.store.images.get(name, b""))
        if i == 0:
            s += cover_size  # the cover only travels with the first part
        return s

    def build(s: int, e: int, path: Path) -> None:
        sub = Book(title=book.title, author=book.author, lang=book.lang, chapters=chapters[s:e], store=book.store,
                   cover=book.cover if s == 0 else None, css=book.css)
        write_epub(sub, path)

    overhead = len(book.css.encode("utf-8")) // 3 + 4096
    parts = _pack(len(chapters), estimate, build, max_bytes, out_dir, overhead)
    # part titles: "Title (1/3)"
    return _finalize_parts(src, parts, out_dir)


# ── PDF ────────────────────────────────────────────────────────────────────

def _xref_len(doc, xref: int) -> int:
    try:
        t, v = doc.xref_get_key(xref, "Length")
        return int(v) if t == "int" else 0
    except Exception:
        return 0


def split_pdf(src: Path, max_bytes: int, out_dir: Path) -> List[Path]:
    if pymupdf is None:
        raise ConvertError("PDF support is not installed")
    doc = pymupdf.open(str(src))
    if doc.is_encrypted and not doc.authenticate(""):
        raise ConvertError("PDF is password-protected")
    n = len(doc)
    if n == 0:
        raise ConvertError("PDF has no pages")
    toc = doc.get_toc(simple=True)
    total = src.stat().st_size
    # rough per-page cost: content-stream length + images on the page
    est: List[int] = []
    for page in doc:
        s = 0
        try:
            for x in page.get_contents():
                s += _xref_len(doc, x)
            for im in page.get_images(full=False):
                s += _xref_len(doc, im[0])
        except Exception:
            pass
        est.append(max(s, 512))
    scale = total / max(sum(est), 1)
    est = [int(e * scale) + 200 for e in est]

    def build(s: int, e: int, path: Path) -> None:
        out = pymupdf.open()
        out.insert_pdf(doc, from_page=s, to_page=e - 1)
        sub = [[lvl, t, p - s] for lvl, t, p in toc if s < p <= e]
        if sub:
            # bookmark levels must start at 1 and not jump
            fixed = []
            prev = 0
            for lvl, t, p in sub:
                lvl = max(1, min(lvl, prev + 1))
                fixed.append([lvl, t, p])
                prev = lvl
            try:
                out.set_toc(fixed)
            except Exception:
                pass
        out.set_metadata(doc.metadata or {})
        out.save(str(path), garbage=4, deflate=True, deflate_images=True, deflate_fonts=True)
        out.close()

    parts = _pack(n, lambda i: est[i], build, max_bytes, out_dir, 2048)
    doc.close()
    return _finalize_parts(src, parts, out_dir)


# ── DOCX ───────────────────────────────────────────────────────────────────

def split_docx(src: Path, max_bytes: int, out_dir: Path) -> List[Path]:
    W = "{%s}" % _W
    with zipfile.ZipFile(src) as z:
        infos = {i.filename: i for i in z.infolist() if not i.filename.endswith("/")}
        if "word/document.xml" not in infos:
            raise ConvertError("Not a valid DOCX")
        doc = etree.fromstring(z.read("word/document.xml"))
        body = doc.find(W + "body")
        if body is None:
            raise ConvertError("DOCX has no body")
        children = list(body)
        sect = None
        if children and _strip_ns(children[-1].tag) == "sectPr":
            sect = children.pop()
        rels_name = "word/_rels/document.xml.rels"
        rels_root = etree.fromstring(z.read(rels_name)) if rels_name in infos else None
        rel_target: Dict[str, str] = {}
        media_rels: Dict[str, str] = {}  # rId -> zip path (images only)
        if rels_root is not None:
            for r in rels_root:
                rid = r.get("Id", "")
                tgt = r.get("Target", "")
                if r.get("TargetMode") == "External":
                    continue
                p = tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath(posixpath.join("word", tgt))
                rel_target[rid] = p
                if "/image" in (r.get("Type") or ""):
                    media_rels[rid] = p
        # media referenced elsewhere (headers, footnotes …) must never be dropped
        keep_media: set = set()
        for name in infos:
            if name.startswith("word/_rels/") and name != rels_name:
                try:
                    for r in etree.fromstring(z.read(name)):
                        tgt = r.get("Target", "")
                        if "/image" in (r.get("Type") or "") and r.get("TargetMode") != "External":
                            keep_media.add(tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath(posixpath.join("word", tgt)))
                except Exception:
                    pass
        media_size = {p: infos[p].compress_size for p in set(media_rels.values()) if p in infos}
        base_files = [n for n in infos if n != "word/document.xml" and n not in media_size]
        base_size = sum(infos[n].compress_size for n in base_files) + 2048

        def rids_of(el: etree._Element) -> set:
            out: set = set()
            for sub in el.iter():
                for k, v in sub.attrib.items():
                    if k.startswith("{%s}" % _R) and v in media_rels:
                        out.add(v)
            return out

        child_rids = [rids_of(c) for c in children]
        est = []
        for c, rids in zip(children, child_rids):
            e = len(etree.tostring(c, encoding="utf-8")) // 4 + 40
            for rid in rids:
                e += media_size.get(media_rels[rid], 0)
            est.append(e)
        src_zip_bytes = src.read_bytes()

        def build(s: int, e: int, path: Path) -> None:
            used = set()
            for r in child_rids[s:e]:
                used |= r
            used_paths = {media_rels[r] for r in used}
            drop_media = {p for rid, p in media_rels.items() if p not in used_paths and p not in keep_media}
            # rebuild <w:body> with just this slice (+ the section properties)
            for ch in list(body):
                body.remove(ch)
            for ch in children[s:e]:
                body.append(ch)
            if sect is not None:
                body.append(sect)
            new_doc = etree.tostring(doc, xml_declaration=True, encoding="UTF-8", standalone=True)
            with zipfile.ZipFile(io.BytesIO(src_zip_bytes)) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    if info.filename.endswith("/") or info.filename in drop_media:
                        continue
                    if info.filename == "word/document.xml":
                        zout.writestr(info.filename, new_doc)
                    elif info.filename == rels_name and rels_root is not None and drop_media:
                        rr = etree.fromstring(zin.read(info.filename))
                        for r in list(rr):
                            if r.get("Id") in media_rels and media_rels[r.get("Id")] in drop_media:
                                rr.remove(r)
                        zout.writestr(info.filename, etree.tostring(rr, xml_declaration=True, encoding="UTF-8", standalone=True))
                    else:
                        zout.writestr(info, zin.read(info.filename))

        parts = _pack(len(children), lambda i: est[i], build, max_bytes, out_dir, base_size)
    return _finalize_parts(src, parts, out_dir)


def split_file(src: Path, max_bytes: int, out_dir: Optional[Path] = None) -> List[Path]:
    """Split `src` into same-format parts no larger than max_bytes.
    Returns [src] unchanged when it already fits."""
    out_dir = out_dir or src.parent
    if max_bytes < MIN_SPLIT_BYTES:
        raise ConvertError(f"Split size too small (min {MIN_SPLIT_BYTES // 1024} KB)")
    if src.stat().st_size <= max_bytes:
        return [src]
    kind = kind_of(src.suffix)
    fn = {"txt": split_txt, "html": split_html, "epub": split_epub, "pdf": split_pdf, "docx": split_docx}[kind]
    parts = fn(src, max_bytes, out_dir)
    oversized = [p for p in parts if p.stat().st_size > max_bytes]
    if oversized and len(parts) > 1:
        # even single items exceed the limit → the format's fixed overhead
        # (styles, fonts, a single page/image) is bigger than the requested size
        smallest = min(p.stat().st_size for p in parts)
        for p in parts:
            p.unlink(missing_ok=True)
        raise ConvertError(
            f"{label_of(src.suffix)} cannot be split into parts smaller than ~{fmt_size(smallest)} — choose a larger split size"
        )
    if len(parts) == 1:
        # couldn't split (single huge item) — hand back the original name
        p = parts[0]
        dst = out_dir / src.name
        if p != dst:
            if dst.exists() and dst != src:
                dst.unlink()
            if p != src:
                p.replace(dst)
        return [dst]
    return parts


def fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1024:.0f} KB"


# ═══════════════════════════════════════════════════════════════════════════
#  CRAWLER CLUTTER — drop "Source / Generated by / Table of Contents" pages
# ═══════════════════════════════════════════════════════════════════════════
#
# EPUBs produced by novel crawlers (Lightnovel Crawler, its Telegram bots,
# WebToEpub, …) start with an "intro" page — synopsis, "Source: https://…",
# "Generated by Lightnovel Crawler", "Made by: BOT …" — followed by the
# navigation document ("Table of Contents • Intro • Chapter 1 …") *inside the
# reading order*.  Text-to-speech readers dutifully read all of that aloud
# before the story starts, and translating it wastes quota.  This pass removes
# those pages from the spine (the nav file itself stays in the manifest, as
# EPUB 3 requires) and drops stray "Source:/Generated by" footers from chapters.

_CLUTTER_MARK_RE = re.compile(
    r"(?is)\b(?:generated\s+by|made\s+(?:by|with)|source\s*:|downloaded\s+(?:from|by)|"
    r"lightnovel\s+crawler|novel\s+downloader|telegram\s+(?:bot|group|channel)|webtoepub|"
    r"epub\s+(?:created|generated)\s+by)\b"
)
_URL_RE = re.compile(r"https?://\S+|\bt\.me/\S+|\b(?:www\.)?[a-z0-9-]+\.(?:me|com|net|org|io|xyz|app)\b", re.I)
_TOC_HEAD_RE = re.compile(r"(?i)^\s*(?:table\s+of\s+)?contents?\s*$")
_CHAPTER_LABEL_RE = re.compile(r"(?i)^\s*(?:\W*\s*)?(?:(?:vol(?:ume)?|book|part|arc)\s*\d+\s*[:.\-–—]?\s*)?(?:ch(?:apter)?\.?\s*\d+|\d+\s*[:.\-–—])")
_INTRO_NAMES = {"intro", "intro_page", "about", "info", "credits", "generated", "source"}


def _norm_text(el: etree._Element) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _looks_like_toc_page(body: etree._Element) -> bool:
    """A page that is essentially a list of chapter links / labels."""
    if body.find(".//nav") is not None:
        return True
    lines = [t for t in (_norm_text(li) for li in body.iter("li", "a", "p")) if t]
    if len(lines) < 5:
        return False
    hits = sum(1 for t in lines if _CHAPTER_LABEL_RE.match(t) or t.lower() in ("intro", "introduction", "cover", "prologue", "epilogue"))
    head = body.find(".//h1")
    head = head if head is not None else body.find(".//h2")
    if head is not None and _TOC_HEAD_RE.match(_norm_text(head) or ""):
        return hits >= 3
    return hits / len(lines) >= 0.8 and body.find(".//a") is not None


_SYNOPSIS_RE = re.compile(
    r"(?i)\b(?:summary|synopsis|description|blurb|about\s+(?:the\s+)?(?:book|novel|story)|"
    r"author\s*:|status\s*:|genres?\s*:|tags?\s*:|translator\s*:|original\s+(?:title|language)|"
    r"total\s+chapters?|chapters?\s*:\s*\d+|last\s+updated?|rating\s*:)\b"
)


def _prose_len_without_clutter(body: etree._Element) -> int:
    """Characters of text left once every small 'Source / Generated by' block is
    ignored — i.e. how much *real* content the page has."""
    total = 0
    for el in body.iter("p", "div", "li", "h1", "h2", "h3", "h4", "blockquote", "span"):
        if len(el) and el.tag in ("div", "blockquote"):
            continue  # container — its children are counted individually
        text = _norm_text(el)
        if not text:
            continue
        if len(text) <= 400 and (_CLUTTER_MARK_RE.search(text) or _URL_RE.search(text)):
            continue
        total += len(text)
    return total


def _is_intro_page(body: etree._Element, path: str) -> bool:
    """The crawler's title/synopsis page ("Summary: …", "Author: …",
    "Source: https://…", "Generated by …") that sits before chapter 1.

    A page is *never* treated as intro when it is a real chapter — i.e. its
    heading looks like "Chapter 12" or it still has a lot of prose once the
    Source/Generated-by footers are ignored.  (Crawlers append those footers to
    every chapter, so their presence alone must not delete chapter 1.)"""
    stem = Path(path).stem.lower()
    text = _norm_text(body)
    if len(text) > 8000:
        return False
    head = body.find(".//h1")
    head = head if head is not None else body.find(".//h2")
    head_txt = _norm_text(head) if head is not None else ""
    named_intro = stem in _INTRO_NAMES or stem.startswith(("intro", "generated", "synopsis", "summary", "info"))
    if head_txt and _CHAPTER_LABEL_RE.match(head_txt) and not named_intro:
        return False
    prose = _prose_len_without_clutter(body)
    if prose > 3000 and not named_intro:
        return False
    if named_intro or body.find(".//*[@id='intro']") is not None:
        return True
    marks = len(_CLUTTER_MARK_RE.findall(text))
    has_url = bool(_URL_RE.search(text))
    synopsis = bool(_SYNOPSIS_RE.search(text))
    if marks >= 2:
        return True
    if marks and (has_url or synopsis):
        return True
    # pure synopsis page: "Author: … Genre: … Summary: …" with metadata lines but no footer
    return synopsis and len(_SYNOPSIS_RE.findall(text)) >= 2 and prose < 3000


def _drop_clutter_blocks(body: etree._Element) -> int:
    """Remove small blocks such as '<p>Source: https://… Generated by …</p>'
    that crawlers append to chapters.  Returns how many were removed."""
    removed = 0
    for el in list(body.iter("p", "div", "footer", "section", "span", "small", "aside", "li")):
        if el.getparent() is None or el is body:
            continue
        # skip nodes whose ancestor was already dropped (avoid double counting)
        anc, detached = el.getparent(), False
        while anc is not None:
            if anc is body:
                break
            anc = anc.getparent()
        else:
            detached = True
        if detached:
            continue
        text = _norm_text(el)
        if not text or len(text) > 400:
            continue
        marks = _CLUTTER_MARK_RE.findall(text)
        if not marks:
            continue
        if len(marks) >= 2 or _URL_RE.search(text) or (el.tag in ("footer", "small") or "footer" in (el.get("class") or "").lower()):
            # never delete a block that carries real prose besides the footer line
            if len(text) > 200 and not _URL_RE.search(text):
                continue
            _drop(el)
            removed += 1
    return removed


def strip_crawler_extras(src: Path, dst: Path) -> List[str]:
    """Copy *src* EPUB to *dst* without crawler-generated intro / TOC pages.

    Returns a list of human-readable notes (empty when nothing was changed —
    in that case *dst* is still a valid identical copy)."""
    notes: List[str] = []
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        lower = {n.lower(): n for n in names}

        def get(name: str) -> Optional[bytes]:
            n = name if name in names else lower.get(name.lower())
            try:
                return z.read(n) if n else None
            except KeyError:
                return None

        opf_path = None
        cont = get("META-INF/container.xml")
        if cont:
            try:
                rf = etree.fromstring(cont).find(".//c:rootfile", _NS)
                if rf is not None:
                    opf_path = rf.get("full-path")
            except Exception:
                pass
        if not opf_path or opf_path not in names:
            cands = [n for n in names if n.lower().endswith(".opf")]
            if not cands:
                raise ConvertError("EPUB has no OPF package file")
            opf_path = cands[0]
        opf_dir = posixpath.dirname(opf_path)
        opf = etree.fromstring(get(opf_path))

        def rel(href: str) -> str:
            href = unquote(href.split("#", 1)[0])
            return posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else posixpath.normpath(href)

        manifest: Dict[str, Tuple[str, str, str]] = {}
        for item in opf.iter("{%s}item" % _NS["opf"]):
            manifest[item.get("id", "")] = (rel(item.get("href", "")), item.get("media-type", ""), item.get("properties", "") or "")
        spine_el = opf.find(".//{%s}spine" % _NS["opf"])
        if spine_el is None:
            raise ConvertError("EPUB has no spine")
        itemrefs = list(spine_el.iter("{%s}itemref" % _NS["opf"]))
        doc_refs = [(r, manifest[r.get("idref")][0]) for r in itemrefs if r.get("idref") in manifest]
        if len(doc_refs) < 2:
            shutil_copy(src, dst)
            return notes

        remove_paths: List[str] = []
        rewritten: Dict[str, bytes] = {}
        n_blocks = 0
        for idx, (ref, path) in enumerate(doc_refs):
            props = manifest[ref.get("idref")][2].split()
            raw = get(path)
            if raw is None:
                continue
            if "nav" in props:
                remove_paths.append(path)
                continue
            try:
                root = _parse_html_doc(raw)
            except Exception:
                continue
            body = root.find("body")
            if body is None:
                body = root
            # intro / toc pages only ever live at the very front (or just after the cover)
            if idx <= 3 and (_is_intro_page(body, path) or _looks_like_toc_page(body)):
                remove_paths.append(path)
                continue
            if _CLUTTER_MARK_RE.search(_norm_text(body)):
                n = _drop_clutter_blocks(body)
                if n:
                    n_blocks += n
                    rewritten[path] = _serialize_doc(root, raw)
        # never strip the whole book
        if len(remove_paths) >= len(doc_refs):
            remove_paths = remove_paths[:-1]
        if not remove_paths and not rewritten:
            shutil_copy(src, dst)
            return notes

        removed_set = set(remove_paths)
        if removed_set:
            for ref, path in doc_refs:
                if path in removed_set:
                    spine_el.remove(ref)
            rewritten[opf_path] = etree.tostring(opf, xml_declaration=True, encoding="utf-8", standalone=True)
            # clean dangling TOC entries (nav.xhtml <li>, toc.ncx <navPoint>)
            for pth, mt, _p in manifest.values():
                if pth in removed_set and "nav" not in _p:
                    continue
                if pth.lower().endswith(".ncx") or "nav" in _p.split():
                    raw = get(pth)
                    if raw:
                        new = _prune_toc_doc(raw, pth, removed_set)
                        if new is not None:
                            rewritten[pth] = new
            notes.append(f"removed {len(removed_set)} crawler page{'s' if len(removed_set) > 1 else ''} (intro / table of contents)")
        if n_blocks:
            notes.append(f"dropped {n_blocks} 'Source / Generated by' line{'s' if n_blocks > 1 else ''}")

        with zipfile.ZipFile(dst, "w") as out:
            seen: set = set()
            if "mimetype" in names:
                out.writestr("mimetype", z.read("mimetype"), compress_type=zipfile.ZIP_STORED)
                seen.add("mimetype")
            for info in z.infolist():
                if info.filename in seen or info.filename.endswith("/"):
                    continue
                seen.add(info.filename)
                data = rewritten.get(info.filename)
                if data is None:
                    out.writestr(info, z.read(info.filename))
                    continue
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                out.writestr(zi, data)
    return notes


def shutil_copy(src: Path, dst: Path) -> None:
    if src.resolve() != dst.resolve():
        dst.write_bytes(src.read_bytes())


def _serialize_doc(root: etree._Element, raw: bytes) -> bytes:
    out = lhtml.tostring(root, encoding="unicode", method="xml", doctype="<!DOCTYPE html>")
    if _XML_DECL_RE.match(raw[:200].decode("utf-8", "ignore")):
        out = '<?xml version="1.0" encoding="utf-8"?>\n' + out
    return out.encode("utf-8")


def _prune_toc_doc(raw: bytes, path: str, removed: set) -> Optional[bytes]:
    """Drop <li>/<navPoint> entries whose link points at a removed page."""
    doc_dir = posixpath.dirname(path)

    def target(href: str) -> str:
        href = unquote((href or "").split("#", 1)[0])
        return posixpath.normpath(posixpath.join(doc_dir, href)) if doc_dir else posixpath.normpath(href)

    try:
        if path.lower().endswith(".ncx"):
            root = etree.fromstring(raw)
            changed = False
            for np in list(root.iter("{http://www.daisy.org/z3986/2005/ncx/}navPoint")):
                c = np.find("{http://www.daisy.org/z3986/2005/ncx/}content")
                if c is not None and target(c.get("src", "")) in removed and np.getparent() is not None:
                    np.getparent().remove(np)
                    changed = True
            return etree.tostring(root, xml_declaration=True, encoding="utf-8") if changed else None
        root = _parse_html_doc(raw)
        changed = False
        for a in list(root.iter("a")):
            if target(a.get("href", "")) in removed:
                li = a.getparent()
                while li is not None and li.tag != "li":
                    li = li.getparent()
                victim = li if li is not None else a
                if victim.getparent() is not None:
                    victim.getparent().remove(victim)
                    changed = True
        return _serialize_doc(root, raw) if changed else None
    except Exception as e:  # noqa: BLE001
        log.debug("toc prune skipped for %s: %s", path, e)
        return None
