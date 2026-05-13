# This file is part of Xpra/Catlink.
# Copyright (C) 2026 Lazycat
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
from urllib.parse import quote, unquote

from xpra.log import Logger

log = Logger("clipboard")


def get_clientfs_prefix() -> str:
    client_id = os.getenv("LZC_CLIENT_ID")
    uid = os.getenv("LZC_CDE_UID")
    if uid:
        return os.path.join("/lzcapp/clientfs/", uid, ".byid", client_id or "")
    log.warn("Warning: LZC_CDE_UID is not set, using legacy clientfs clipboard path but may not work")
    return os.path.join("/lzcapp/clientfs/", client_id or "")


def file_uri_to_clientfs_uri(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    return file_path_to_clientfs_uri(unquote(uri[len("file://"):]))


def file_path_to_clientfs_uri(path: str) -> str:
    file_path = path.replace("\\", "/")
    uri_path = quote(f"{get_clientfs_prefix()}/{file_path}", safe="/")
    return f"file://{uri_path}"
