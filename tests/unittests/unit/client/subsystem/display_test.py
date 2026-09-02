#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest

from xpra.util.objects import AdHocStruct
from unit.test_util import silence_info
from unit.process_test_util import DisplayContext
from unit.client.subsystem.clientmixintest_util import ClientMixinTest


class DisplayClientTest(ClientMixinTest):

    def test_screen_topology_signature_ignores_workarea(self):
        from xpra.client.subsystem.display import DisplayClient

        settings = (
            3840, 2160,
            (("screen", 3840, 2160, 700, 400,
              (("monitor", 0, 0, 3840, 2160, 700, 400, 0, 60, 3840, 1988),),
              0, 60, 3840, 1988),),
            1, (), 3840, 2160, 144, 144, 60,
            {0: {"geometry": (0, 0, 3840, 2160), "scale-factor": 2}},
        )
        dock_resized = settings[:2] + (
            (("screen", 3840, 2160, 700, 400,
              (("monitor", 0, 0, 3840, 2160, 700, 400, 0, 60, 3840, 1990),),
              0, 60, 3840, 1990),),
        ) + settings[3:]
        self.assertEqual(
            DisplayClient._screen_topology_signature(settings),
            DisplayClient._screen_topology_signature(dock_resized),
        )

    def test_screen_topology_signature_detects_geometry_change(self):
        from xpra.client.subsystem.display import DisplayClient

        settings = (
            3840, 2160,
            (("screen", 3840, 2160, 700, 400,
              (("monitor", 0, 0, 1920, 1080, 700, 400),
               ("monitor-2", 1920, 0, 1920, 1080, 700, 400)),
              0, 0, 3840, 2160),),
            1, (), 3840, 2160, 144, 144, 60,
            {0: {"geometry": (0, 0, 3840, 2160), "scale-factor": 2}},
        )
        moved = settings[:2] + (
            (("screen", 3840, 2160, 700, 400,
              (("monitor", 0, 0, 1280, 1080, 700, 400),
               ("monitor-2", 1280, 0, 2560, 1080, 700, 400)),
              0, 0, 3840, 2160),),
        ) + settings[3:]
        self.assertNotEqual(
            DisplayClient._screen_topology_signature(settings),
            DisplayClient._screen_topology_signature(moved),
        )

    def test_display(self):
        with DisplayContext():
            from xpra.client.subsystem import display  # pylint: disable=import-outside-toplevel
            def _DisplayClient():
                dc = display.DisplayClient()
                def get_root_size():
                    return 1024, 768
                dc.get_root_size = get_root_size
                def get_screen_sizes(*_args):
                    return ((1024, 768),)
                dc.get_screen_sizes = get_screen_sizes
                return dc
            opts = AdHocStruct()
            opts.desktop_fullscreen = False
            opts.desktop_scaling = False
            opts.dpi = 144
            opts.refresh_rate = "20"
            with silence_info(display):
                self._test_mixin_class(_DisplayClient, opts, {
                    "display" : ":999",
                    "desktop_size" : (1024, 768),
                    "max_desktop_size" : (3840, 2160),
                    "actual_desktop_size" : (1024, 768),
                    "resize_screen" : True,
                    })

def main():
    unittest.main()


if __name__ == '__main__':
    main()
