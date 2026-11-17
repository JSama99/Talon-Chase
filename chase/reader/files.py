"""Turn whatever a carrier sent into something a vision model can be shown.

Carriers send phone photos (JPEG, and HEIC from iPhones), scans (PDF, multi-page
TIFF from fax machines) and the occasional screenshot (PNG, WebP). ``prepare``
works out what a file is from its bytes, never from its name, and returns either
the PDF as it is or upright JPEG pages no larger than the model can use.

It also answers the one question a model must not be asked: how many pages the
file has. That is counted here, from the file.

Anything that cannot be shown to a reader (a Word file, a password-protected PDF,
a broken upload) raises ``Unshowable`` with a reason a dispatcher can read. Nothing
here touches the network.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

try:  # HEIC and HEIF, the default format of iPhone cameras
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF = True
except ImportError:  # pragma: no cover - the dependency is declared; this is for odd platforms
    HEIF = False

PDF = "application/pdf"
JPEG = "image/jpeg"
PNG = "image/png"

# The long edge the current models read at full detail; larger is downscaled by the
# API anyway, so sending more only costs upload time.
MAX_EDGE_PX = 2576
# Per image, before base64. The API's limit is higher; a page this large is a mistake.
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_PDF_BYTES = 24 * 1024 * 1024
# A POD is a page or two; a rate confirmation with terms might be ten. Past this the
# file is something else, and reading it would cost more than a person's glance.
MAX_PAGES = 20
# Refuse to decode images beyond this many pixels (a decompression bomb, or a wall
# poster). A 48-megapixel phone photo is under it.
MAX_PIXELS = 120_000_000


class Unshowable(Exception):
    """The file cannot be shown to a reader. ``str(exc)`` says why, for a person."""


@dataclass(frozen=True)
class Part:
    media_type: str
    data: bytes


@dataclass(frozen=True)
class Prepared:
    kind: str  # "pdf" or "image"
    parts: list[Part]
    page_count: int
    source: str  # what the file was: "pdf", "jpeg", "heic", ...


def sniff(content: bytes) -> str:
    """What the bytes are: a short lowercase name, or "" when not recognised."""
    head = content[:1024]
    if b"%PDF-" in head:
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:2] == b"BM":
        return "bmp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
            return "heic"
        if brand in (b"avif", b"avis"):
            return "avif"
        return "video"  # an .mp4 or .mov: a driver filmed the paperwork
    if head[:4] == b"PK\x03\x04":
        return "zip"  # also .docx and .xlsx
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "office"
    stripped = head.lstrip()
    if stripped[:5].lower() in (b"<html", b"<!doc"):
        return "html"
    return ""


_NOT_A_PICTURE = {
    "video": "it is a video, not a photo or a PDF",
    "zip": "it is a zip or an Office file, not a photo or a PDF",
    "office": "it is an Office file, not a photo or a PDF",
    "html": "it is a web page, not a photo or a PDF",
    "": "it is not a photo or a PDF",
}


def prepare(content: bytes) -> Prepared:
    """What to show the reader for this file, or ``Unshowable``."""
    if not content:
        raise Unshowable("the file is empty")
    kind = sniff(content)
    if kind == "pdf":
        return _pdf(content)
    if kind in _NOT_A_PICTURE:
        raise Unshowable(_NOT_A_PICTURE[kind])
    if kind == "heic" and not HEIF:  # pragma: no cover
        raise Unshowable("it is an iPhone HEIC photo and this server cannot open those")
    return _image(content, kind)


# ------------------------------------------------------------------------ PDFs


def _pdf(content: bytes) -> Prepared:
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        unlocked = False
        if reader.is_encrypted:
            # Many scanners "secure" a PDF with an empty password: it opens for anyone
            # but is still encrypted, which the model's API refuses. Unlock and rewrite.
            if not reader.decrypt(""):
                raise Unshowable("the PDF is password-protected")
            unlocked = True
        pages = len(reader.pages)
        if pages == 0:
            raise Unshowable("the PDF has no pages")
        if pages > MAX_PAGES:
            raise Unshowable(f"the PDF has {pages} pages, too many to be a proof of delivery")
        if unlocked:
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            out = io.BytesIO()
            writer.write(out)
            content = out.getvalue()
    except Unshowable:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, RecursionError, OSError) as exc:
        raise Unshowable("the PDF is damaged and will not open") from exc
    if len(content) > MAX_PDF_BYTES:
        megabytes = len(content) / (1024 * 1024)
        raise Unshowable(f"the PDF is {megabytes:.0f} MB, too large to read")
    return Prepared("pdf", [Part(PDF, content)], pages, "pdf")


# ---------------------------------------------------------------------- images


def _image(content: bytes, kind: str) -> Prepared:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(content))
            if image.width * image.height > MAX_PIXELS:
                raise Unshowable("the picture is too large to open")
            frames = getattr(image, "n_frames", 1) if kind == "tiff" else 1
            if frames > MAX_PAGES:
                raise Unshowable(f"the scan has {frames} pages, too many to be a proof of delivery")
            parts = []
            for index in range(frames):
                if frames > 1:
                    image.seek(index)
                parts.append(_page(image, content if frames == 1 else None, kind))
    except Unshowable:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
    ) as exc:
        raise Unshowable("the picture is damaged and will not open") from exc
    return Prepared("image", parts, len(parts), kind)


def _page(image: Image.Image, original: bytes | None, kind: str) -> Part:
    """One upright page no larger than the reader can use."""
    orientation = image.getexif().get(0x0112, 1)
    fits = max(image.size) <= MAX_EDGE_PX
    if (
        original is not None
        and kind in ("jpeg", "png")
        and orientation in (1, None)
        and fits
        and len(original) <= MAX_IMAGE_BYTES
        and image.mode in ("RGB", "L")
    ):
        image.load()  # a truncated file fails here, not at the reader
        return Part(JPEG if kind == "jpeg" else PNG, original)  # already fine: no re-encoding

    page = ImageOps.exif_transpose(image)  # phones store "rotate me" instead of rotating
    if page.mode in ("RGBA", "LA", "PA") or "transparency" in page.info:
        page = page.convert("RGBA")
        flat = Image.new("RGB", page.size, "white")
        flat.paste(page, mask=page.getchannel("A"))
        page = flat
    elif page.mode not in ("RGB", "L"):
        page = page.convert("L" if page.mode in ("1", "I", "I;16", "F") else "RGB")
    if max(page.size) > MAX_EDGE_PX:
        page.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.Resampling.LANCZOS)
    for quality in (90, 80, 65):
        out = io.BytesIO()
        page.save(out, format="JPEG", quality=quality, optimize=True)
        if out.tell() <= MAX_IMAGE_BYTES:
            return Part(JPEG, out.getvalue())
    raise Unshowable("the picture is too large to read")  # pragma: no cover
