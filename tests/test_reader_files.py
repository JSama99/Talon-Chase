"""What a carrier sends, made showable: the file type comes from the bytes, pages are
counted from the file, photos come out upright and no larger than a reader can use,
and anything that cannot be shown says why in words a dispatcher can read."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from chase.reader import files
from chase.reader.files import MAX_EDGE_PX, MAX_PAGES, Unshowable, prepare, sniff


def picture(size=(800, 600), fmt="JPEG", mode="RGB", **save) -> bytes:
    image = Image.new(mode, size, "white" if mode != "1" else 1)
    out = io.BytesIO()
    image.save(out, fmt, **save)
    return out.getvalue()


def pdf(pages=1, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(612, 792)
    if password is not None:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def opened(part) -> Image.Image:
    return Image.open(io.BytesIO(part.data))


@pytest.mark.parametrize(
    ("fmt", "kind"),
    [("JPEG", "jpeg"), ("PNG", "png"), ("WEBP", "webp"), ("TIFF", "tiff"), ("BMP", "bmp"),
     ("GIF", "gif"), ("HEIF", "heic")],
)  # fmt: skip
def test_every_format_a_carrier_sends_is_recognised_from_its_bytes(fmt, kind):
    content = picture(fmt=fmt)
    assert sniff(content) == kind
    prepared = prepare(content)
    assert prepared.source == kind and prepared.kind == "image" and prepared.page_count == 1
    assert prepared.parts[0].media_type in ("image/jpeg", "image/png")
    assert opened(prepared.parts[0]).size == (800, 600)


def test_the_name_of_a_file_plays_no_part():
    """prepare() does not even take the name: a PDF called photo.jpg is a PDF."""
    assert prepare(pdf()).kind == "pdf"


def test_a_good_jpeg_or_png_is_passed_through_untouched():
    for fmt, media in (("JPEG", "image/jpeg"), ("PNG", "image/png")):
        content = picture(fmt=fmt)
        (part,) = prepare(content).parts
        assert part.data == content and part.media_type == media


def test_a_phone_photo_is_shrunk_to_what_a_reader_can_use():
    prepared = prepare(picture(size=(4032, 3024)))
    assert opened(prepared.parts[0]).size == (MAX_EDGE_PX, 1932)
    assert prepared.parts[0].media_type == "image/jpeg"


def test_a_photo_stored_sideways_comes_out_upright():
    """Phones often store 'rotate me' in the file instead of rotating the pixels."""
    image = Image.new("RGB", (800, 600), "white")
    image.paste((255, 0, 0), (0, 0, 80, 80))
    exif = image.getexif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to view
    out = io.BytesIO()
    image.save(out, "PNG", exif=exif)
    upright = opened(prepare(out.getvalue()).parts[0])
    assert upright.size == (600, 800)
    red, green, _ = upright.convert("RGB").getpixel((560, 40))
    assert red > 200 and green < 80  # the top-left corner is now top-right
    assert upright.getexif().get(0x0112, 1) == 1


def test_a_see_through_png_gets_a_white_page_not_a_black_one():
    image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    out = io.BytesIO()
    image.save(out, "PNG")
    flat = opened(prepare(out.getvalue()).parts[0]).convert("RGB")
    assert min(flat.getpixel((20, 20))) > 240


def test_a_multi_page_fax_becomes_one_picture_per_page():
    page = Image.new("1", (1700, 2200), 1)
    out = io.BytesIO()
    page.save(out, "TIFF", save_all=True, append_images=[page, page], compression="group4")
    prepared = prepare(out.getvalue())
    assert prepared.page_count == 3 and len(prepared.parts) == 3
    assert {p.media_type for p in prepared.parts} == {"image/jpeg"}


def test_pdf_pages_are_counted_from_the_file():
    prepared = prepare(pdf(pages=3))
    assert (prepared.kind, prepared.page_count) == ("pdf", 3)
    assert prepared.parts[0].media_type == "application/pdf"
    assert PdfReader(io.BytesIO(prepared.parts[0].data)).get_num_pages() == 3


def test_a_pdf_locked_with_an_empty_password_is_unlocked():
    """Scanner software does this. It opens for anyone, but is still encrypted, and
    the model's API refuses encrypted PDFs."""
    locked = pdf(pages=2, password="")
    assert PdfReader(io.BytesIO(locked)).is_encrypted
    prepared = prepare(locked)
    assert prepared.page_count == 2
    assert not PdfReader(io.BytesIO(prepared.parts[0].data)).is_encrypted


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (b"", "the file is empty"),
        (b"hello, see attached", "it is not a photo or a PDF"),
        (b"PK\x03\x04" + b"\0" * 40, "it is a zip or an Office file, not a photo or a PDF"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 40, "it is an Office file"),
        (b"\x00\x00\x00\x18ftypmp42" + b"\0" * 40, "it is a video, not a photo or a PDF"),
        (b"  <!DOCTYPE html><html>", "it is a web page, not a photo or a PDF"),
        (b"%PDF-1.7\n", "the PDF is damaged and will not open"),
        (b"\xff\xd8\xff\xe0 not really a jpeg", "the picture is damaged and will not open"),
    ],
)
def test_what_cannot_be_shown_says_why_in_plain_words(content, why):
    with pytest.raises(Unshowable) as caught:
        prepare(content)
    assert why in str(caught.value)


def test_a_cut_off_upload_is_caught_here_not_at_the_reader():
    whole = picture(size=(1200, 900))
    with pytest.raises(Unshowable, match="damaged"):
        prepare(whole[: len(whole) // 2])


def test_a_password_protected_pdf_is_refused():
    with pytest.raises(Unshowable, match="password-protected"):
        prepare(pdf(password="secret"))


def test_a_file_too_long_to_be_a_pod_is_refused():
    with pytest.raises(Unshowable, match=f"{MAX_PAGES + 1} pages, too many"):
        prepare(pdf(pages=MAX_PAGES + 1))
    page = Image.new("1", (100, 100), 1)
    out = io.BytesIO()
    page.save(out, "TIFF", save_all=True, append_images=[page] * MAX_PAGES)
    with pytest.raises(Unshowable, match="too many"):
        prepare(out.getvalue())


def test_a_picture_too_large_to_open_safely_is_refused(monkeypatch):
    monkeypatch.setattr(files, "MAX_PIXELS", 1000)
    with pytest.raises(Unshowable, match="too large to open"):
        prepare(picture(size=(800, 600)))


def test_a_pdf_too_large_to_send_is_refused(monkeypatch):
    monkeypatch.setattr(files, "MAX_PDF_BYTES", 100)
    with pytest.raises(Unshowable, match="too large to read"):
        prepare(pdf())
