# This file is part of Xpra.
# Copyright (C) 2011 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.


def get_backend_module() -> str:
    # Keep the production sender (including GTK bitmap conversion/clientfs).
    # Remote downloads are published through its provider, not a new backend.
    return "xpra.gtk.clipboard.GTK_Clipboard"
