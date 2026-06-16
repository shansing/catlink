# This file is part of Xpra.
# Copyright (C) 2026
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from __future__ import annotations

import base64
from io import BytesIO
import re

import objc
from AppKit import (
    NSAttachmentAttributeName, NSAttributedString,
    NSCharacterEncodingDocumentAttribute, NSDocumentTypeDocumentAttribute,
    NSHTMLTextDocumentType, NSRTFDTextDocumentType, NSRTFTextDocumentType,
    NSPasteboardTypeHTML, NSPasteboardTypeRTF, NSPasteboardTypeRTFD,
)
from CoreFoundation import CFDataGetBytes, CFDataGetLength
from Foundation import NSMakeRange, NSUTF8StringEncoding

from xpra.clipboard.proxy import filter_data
from xpra.log import Logger

log = Logger("clipboard", "osx")

HTML_TARGETS = ("text/html", "text/html;charset=utf-8")


def nsdata_to_bytes(data) -> bytes:
    if not data:
        return b""
    length = CFDataGetLength(data)
    return CFDataGetBytes(data, (0, length), None)


def first_result(value):
    if isinstance(value, tuple):
        return value[0] if value else None
    return value


def has_rich_text(types) -> bool:
    return NSPasteboardTypeHTML in types or NSPasteboardTypeRTF in types or NSPasteboardTypeRTFD in types


def get_attributed_string(pasteboard):
    dtype = NSPasteboardTypeRTFD
    data = pasteboard.dataForType_(dtype)
    document_type = NSRTFDTextDocumentType
    if not data:
        dtype = NSPasteboardTypeRTF
        data = pasteboard.dataForType_(dtype)
        document_type = NSRTFTextDocumentType
    if not data:
        log("get_attributed_string() no rich text data")
        return None
    options = {NSDocumentTypeDocumentAttribute: document_type}
    attributed = NSAttributedString.alloc().initWithData_options_documentAttributes_error_(
        data, options, objc.NULL, objc.NULL,
    )
    attributed = first_result(attributed)
    log("get_attributed_string() type=%s length=%s attributed=%s",
        dtype, attributed.length() if attributed else 0, bool(attributed))
    return attributed


def get_rich_text_html(pasteboard) -> bytes:
    if NSPasteboardTypeHTML in (pasteboard.types() or ()):
        data = pasteboard.dataForType_(NSPasteboardTypeHTML)
        if data:
            html = nsdata_to_bytes(data)
            log("get_rich_text_html() public.html=%i bytes", len(html))
            return html
    attributed = get_attributed_string(pasteboard)
    if not attributed:
        return b""
    length = attributed.length()
    options = {
        NSDocumentTypeDocumentAttribute: NSHTMLTextDocumentType,
        NSCharacterEncodingDocumentAttribute: NSUTF8StringEncoding,
    }
    data = attributed.dataFromRange_documentAttributes_error_(
        NSMakeRange(0, length), options, objc.NULL,
    )
    data = first_result(data)
    html = nsdata_to_bytes(data)
    html = inline_html_images(attributed, html)
    log("get_rich_text_html() exported %i bytes", len(html))
    return html


def inline_html_images(attributed, html: bytes) -> bytes:
    images = get_rich_text_images(attributed, "image/png")
    if not images:
        return html
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError:
        log("inline_html_images() cannot decode HTML as UTF-8")
        return html
    image_iter = iter(images)

    def replace_src(match) -> str:
        try:
            image = next(image_iter)
        except StopIteration:
            return match.group(0)
        uri = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        return match.group(1) + uri + match.group(2)

    text, count = re.subn(r'(<img\b[^>]*?\bsrc=")[^"]*("[^>]*>)', replace_src, text, flags=re.IGNORECASE)
    if count < len(images):
        text, single_count = re.subn(
            r"(<img\b[^>]*?\bsrc=')[^']*('[^>]*>)", replace_src, text, flags=re.IGNORECASE,
        )
        count += single_count
    log("inline_html_images() embedded %i of %i images", count, len(images))
    return text.encode("utf-8")


def get_rich_text_image(pasteboard, target: str) -> bytes | None:
    attributed = get_attributed_string(pasteboard)
    images = get_rich_text_images(attributed, target)
    return images[0] if images else None


def get_rich_text_images(attributed, target: str) -> list[bytes]:
    if target not in ("image/png", "image/jpeg", "image/tiff"):
        return []
    if not attributed:
        return []
    images = []
    length = attributed.length()
    for offset in range(length):
        attachment = attributed.attribute_atIndex_effectiveRange_(NSAttachmentAttributeName, offset, None)
        attachment = first_result(attachment)
        if not attachment:
            continue
        file_wrapper = attachment.fileWrapper()
        data = get_image_from_file_wrapper(file_wrapper, target)
        if data:
            images.append(data)
    return images


def get_image_from_file_wrapper(file_wrapper, target: str) -> bytes | None:
    if not file_wrapper:
        return None
    if file_wrapper.isDirectory():
        for child in (file_wrapper.fileWrappers() or {}).values():
            data = get_image_from_file_wrapper(child, target)
            if data:
                return data
        return None
    data = file_wrapper.regularFileContents()
    if not data:
        return None
    filename = str(file_wrapper.preferredFilename() or file_wrapper.filename() or "").lower()
    raw = nsdata_to_bytes(data)
    if not raw:
        return None
    if filename.endswith(".png"):
        src_dtype = "image/png"
    elif filename.endswith((".jpg", ".jpeg")):
        src_dtype = "image/jpeg"
    elif filename.endswith((".tif", ".tiff")):
        src_dtype = "image/tiff"
    elif filename.endswith(".gif"):
        src_dtype = "image/gif"
    else:
        src_dtype = guess_image_type(raw)
    if not src_dtype:
        log("file wrapper image type not recognized: filename=%s", filename)
        return None
    if src_dtype == target:
        log("get_image_from_file_wrapper() found %s image %i bytes", src_dtype, len(raw))
        return raw
    try:
        if src_dtype == "image/gif":
            from xpra.codecs.pillow.decoder import open_only
            img = open_only(raw, ("gif",))
            buf = BytesIO()
            img.save(buf, target.split("/")[-1].upper())
            img_data = buf.getvalue()
            buf.close()
        else:
            img_data = filter_data(dtype=src_dtype, dformat=8, data=raw, trusted=False, output_dtype=target)
        log("get_image_from_file_wrapper() converted %s to %s: %i -> %i bytes",
            src_dtype, target, len(raw), len(img_data or b""))
        return img_data
    except Exception:
        log("get_image_from_file_wrapper()", exc_info=True)
        return None


def guess_image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return ""
