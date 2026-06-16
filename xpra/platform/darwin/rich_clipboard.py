# This file is part of Xpra.
# Copyright (C) 2026
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from __future__ import annotations

import ctypes
import errno
import os
import os.path
import base64
from io import BytesIO
import re
import shutil
from urllib.parse import unquote

import objc
from AppKit import (
    NSAttachmentAttributeName, NSAttributedString,
    NSCharacterEncodingDocumentAttribute, NSDocumentTypeDocumentAttribute,
    NSHTMLTextDocumentType, NSRTFDTextDocumentType, NSRTFTextDocumentType,
    NSURL, NSPasteboardTypeHTML, NSPasteboardTypeRTF, NSPasteboardTypeRTFD,
    NSPasteboardURLReadingFileURLsOnlyKey,
)
from CoreFoundation import CFDataGetBytes, CFDataGetLength
from Foundation import (
    NSCachesDirectory, NSMakeRange, NSSearchPathForDirectoriesInDomains,
    NSUserDomainMask, NSUTF8StringEncoding,
)

from xpra.clipboard.proxy import filter_data
from xpra.clipboard.uri import file_path_to_clientfs_uri
from xpra.log import Logger

log = Logger("clipboard", "osx")

HTML_TARGETS = ("text/html", "text/html;charset=utf-8")
MATERIALIZED_ROOT = "catlink-clipboard"
CLONE_NOFOLLOW = 1
CLONE_FALLBACK_ERRNOS = tuple(
    e for e in (
        errno.ENOTSUP,
        errno.EXDEV,
        errno.EINVAL,
        getattr(errno, "EOPNOTSUPP", None),
    )
    if e is not None
)

try:
    _libc = ctypes.CDLL(None, use_errno=True)
    _clonefile = _libc.clonefile
    _clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    _clonefile.restype = ctypes.c_int
except AttributeError:
    _clonefile = None


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


def has_rtfd_rtf(types) -> bool:
    return NSPasteboardTypeRTFD in types or NSPasteboardTypeRTF in types


def has_rich_text_content(pasteboard) -> bool:
    attributed = get_attributed_string(pasteboard)
    if not attributed:
        return False
    text = str(attributed.string() or "")
    text = text.replace("\ufffc", "")
    has_content = bool(text.strip())
    log("has_rich_text_content()=%s", has_content)
    return has_content


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
            log_html_debug(pasteboard, html)
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
    log_html_debug(pasteboard, html)
    html = inline_html_images(attributed, html)
    html = rewrite_html_attachment_uris(pasteboard, html)
    log("get_rich_text_html() exported %i bytes", len(html))
    return html


def log_html_debug(pasteboard, html: bytes) -> None:
    if not log.is_debug_enabled():
        return
    log("get_clientfs_uri_map()=%s", get_clientfs_uri_map(pasteboard))
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError:
        text = html.decode("utf-8", "replace")
    log("rich clipboard HTML:\n%s", text)


def rewrite_html_attachment_uris(pasteboard, html: bytes) -> bytes:
    uri_map = get_clientfs_uri_map(pasteboard)
    if not uri_map:
        return html
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError:
        log("rewrite_html_attachment_uris() cannot decode HTML as UTF-8")
        return html
    rewritten = 0

    def replace_attr(match) -> str:
        nonlocal rewritten
        value = match.group(3)
        name = attachment_name(value)
        uris = uri_map.get(name)
        uri = uris.pop(0) if uris else ""
        if not uri:
            return match.group(0)
        rewritten += 1
        return f"{match.group(1)}{match.group(2)}{uri}{match.group(4)}"

    attr_pattern = r"(\b(?:src|href|data)\s*=\s*)([\"'])(.*?)(\2)"
    uri_count = sum(len(uris) for uris in uri_map.values())
    text = re.sub(attr_pattern, replace_attr, text, flags=re.IGNORECASE)
    log("rewrite_html_attachment_uris() rewrote %i HTML attributes using %i file URLs", rewritten, uri_count)
    if rewritten and log.is_debug_enabled():
        log("rewritten rich clipboard HTML:\n%s", text)
    return text.encode("utf-8")


def inline_html_images(attributed, html: bytes) -> bytes:
    image_map = get_rich_text_image_map(attributed, "image/png")
    if not image_map:
        return html
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError:
        log("inline_html_images() cannot decode HTML as UTF-8")
        return html
    inlined = 0

    def replace_attr(match) -> str:
        nonlocal inlined
        value = match.group(3)
        name = attachment_name(value)
        images = image_map.get(name)
        image = images.pop(0) if images else b""
        if not image:
            return match.group(0)
        inlined += 1
        uri = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        return f"{match.group(1)}{match.group(2)}{uri}{match.group(4)}"

    attr_pattern = r"(\b(?:src|data)\s*=\s*)([\"'])(.*?)(\2)"
    image_count = sum(len(images) for images in image_map.values())
    text = re.sub(attr_pattern, replace_attr, text, flags=re.IGNORECASE)
    log("inline_html_images() inlined %i HTML image attributes using %i images", inlined, image_count)
    return text.encode("utf-8")


def get_pasteboard_file_urls(pasteboard) -> list:
    urls = pasteboard.readObjectsForClasses_options_(
        [NSURL],
        {NSPasteboardURLReadingFileURLsOnlyKey: True},
    )
    return list(urls or ())


def get_clientfs_uris(pasteboard, urls=None, materialize=False) -> list[str]:
    uris = []
    png_map = get_png_attachment_map(pasteboard) if materialize else {}
    for url in urls if urls is not None else get_pasteboard_file_urls(pasteboard):
        path = materialize_url(url, png_map) if materialize else str(url.path() or "")
        if path:
            uris.append(file_path_to_clientfs_uri(path))
    return uris


def get_clientfs_uri_map(pasteboard) -> dict[str, list[str]]:
    uri_map = {}
    for url in get_pasteboard_file_urls(pasteboard):
        path = url.path()
        if not path:
            continue
        path = str(path)
        materialized = materialize_path(path)
        uri = file_path_to_clientfs_uri(materialized or path)
        name = attachment_name(path)
        if name:
            uri_map.setdefault(name, []).append(uri)
    return uri_map


def materialize_url(url, png_map=None) -> str:
    path = url.path()
    return materialize_path(str(path), png_map) if path else ""


def materialize_path(path: str, png_map=None) -> str:
    if not path:
        return ""
    png_data = pop_png_attachment(png_map, path)
    if png_data:
        return materialize_png(path, png_data)
    dst = materialized_path(path)
    try:
        if same_file(path, dst):
            return dst
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        clone_or_copy(path, dst)
        log("materialize_path(%s)=%s", path, dst)
        return dst
    except Exception as e:
        log("materialize_path(%s) failed: %s", path, e)
        return ""


def materialize_png(path: str, png_data: bytes) -> str:
    dst = materialized_path(replace_extension(path, ".png"))
    try:
        if same_bytes(png_data, dst):
            return dst
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(png_data)
        log("materialize_png(%s)=%s", path, dst)
        return dst
    except Exception as e:
        log("materialize_png(%s) failed: %s", path, e)
        return ""


def replace_extension(path: str, extension: str) -> str:
    base, _ = os.path.splitext(path)
    return base + extension


def same_bytes(data: bytes, dst: str) -> bool:
    try:
        with open(dst, "rb") as f:
            return f.read() == data
    except OSError:
        return False


def materialized_path(path: str) -> str:
    path = path.replace("\\", "/")
    suffix = path
    marker = "/Library/Group Containers/"
    if marker in path:
        suffix = path[path.index(marker):]
    elif path.startswith("/"):
        suffix = path[1:]
    root = os.path.realpath(get_materialized_root())
    dst = os.path.realpath(os.path.join(root, suffix.lstrip("/")))
    if not is_path_under(dst, root):
        raise ValueError("materialized path escapes cache root")
    return dst


def get_materialized_root() -> str:
    try:
        dirs = NSSearchPathForDirectoriesInDomains(NSCachesDirectory, NSUserDomainMask, True)
        if dirs:
            return os.path.join(str(dirs[0]), MATERIALIZED_ROOT)
    except Exception as e:
        log("get_materialized_root() failed to find user caches directory: %s", e)
    return os.path.expanduser(os.path.join("~/Library/Caches", MATERIALIZED_ROOT))


def is_path_under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def same_file(src: str, dst: str) -> bool:
    try:
        src_stat = os.stat(src)
        dst_stat = os.stat(dst)
    except OSError:
        return False
    return src_stat.st_size == dst_stat.st_size and int(src_stat.st_mtime_ns) == int(dst_stat.st_mtime_ns)


def clone_or_copy(src: str, dst: str) -> None:
    try:
        os.unlink(dst)
    except FileNotFoundError:
        pass
    if _clonefile:
        src_b = os.fsencode(src)
        dst_b = os.fsencode(dst)
        if _clonefile(src_b, dst_b, CLONE_NOFOLLOW) == 0:
            return
        err = ctypes.get_errno()
        if err not in CLONE_FALLBACK_ERRNOS:
            log("clonefile(%s, %s) failed with errno=%s, falling back to copy", src, dst, err)
    shutil.copy2(src, dst)


def attachment_name(value: str) -> str:
    return os.path.basename(unquote(value or "")).lower()


def get_png_attachment_map(pasteboard) -> dict[str, list[bytes]]:
    attributed = get_attributed_string(pasteboard)
    return get_rich_text_image_map(attributed, "image/png")


def pop_png_attachment(png_map, path: str) -> bytes:
    if not png_map:
        return b""
    if not is_tiff_path(path):
        return b""
    images = png_map.get(attachment_name(path))
    return images.pop(0) if images else b""


def is_tiff_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".tif", ".tiff")


def get_rich_text_image(pasteboard, target: str) -> bytes | None:
    attributed = get_attributed_string(pasteboard)
    images = get_rich_text_images(attributed, target)
    return images[0] if images else None


def get_rich_text_image_map(attributed, target: str) -> dict[str, list[bytes]]:
    if target not in ("image/png", "image/jpeg", "image/tiff"):
        return {}
    if not attributed:
        return {}
    images: dict[str, list[bytes]] = {}
    length = attributed.length()
    for offset in range(length):
        attachment = attributed.attribute_atIndex_effectiveRange_(NSAttachmentAttributeName, offset, None)
        attachment = first_result(attachment)
        if not attachment:
            continue
        for name, data in get_images_from_file_wrapper(attachment.fileWrapper(), target):
            if name:
                images.setdefault(attachment_name(name), []).append(data)
    return images


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
    images = get_images_from_file_wrapper(file_wrapper, target)
    return images[0][1] if images else None


def get_images_from_file_wrapper(file_wrapper, target: str) -> list[tuple[str, bytes]]:
    if not file_wrapper:
        return []
    if file_wrapper.isDirectory():
        images = []
        for child in (file_wrapper.fileWrappers() or {}).values():
            images += get_images_from_file_wrapper(child, target)
        return images
    data = file_wrapper.regularFileContents()
    if not data:
        return []
    filename = str(file_wrapper.preferredFilename() or file_wrapper.filename() or "")
    lower_filename = filename.lower()
    raw = nsdata_to_bytes(data)
    if not raw:
        return []
    if lower_filename.endswith(".png"):
        src_dtype = "image/png"
    elif lower_filename.endswith((".jpg", ".jpeg")):
        src_dtype = "image/jpeg"
    elif lower_filename.endswith((".tif", ".tiff")):
        src_dtype = "image/tiff"
    elif lower_filename.endswith(".gif"):
        src_dtype = "image/gif"
    else:
        src_dtype = guess_image_type(raw)
    if not src_dtype:
        log("file wrapper image type not recognized: filename=%s", filename)
        return []
    if src_dtype == target:
        log("get_image_from_file_wrapper() found %s image %i bytes", src_dtype, len(raw))
        return [(filename, raw)]
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
        return [(filename, img_data)]
    except Exception:
        log("get_image_from_file_wrapper()", exc_info=True)
        return []


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
