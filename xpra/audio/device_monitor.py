# This file is part of Xpra.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.os_util import WIN32, OSX
from xpra.log import Logger

log = Logger("audio")


class AudioDeviceMonitor:
    def __init__(self):
        self.monitor = None

    def start(self, on_change) -> bool:
        try:
            if OSX:
                from xpra.platform.darwin.audio_device_monitor import AudioDeviceMonitor as Monitor
                self.monitor = Monitor.reusable()
                self.monitor.start(on_change)
                return True
            elif WIN32:
                from xpra.platform.win32 import audio_device_monitor
                audio_device_monitor.start(on_change)
                self.monitor = audio_device_monitor
                return True
        except Exception:
            log.warn("Warning: audio output device monitoring unavailable", exc_info=True)
            self.stop()
        return False

    def stop(self) -> bool:
        monitor = self.monitor
        if monitor:
            try:
                result = monitor.stop()
            except Exception:
                log("audio device monitor cleanup failed", exc_info=True)
                return False
            if result is False:
                return False
            self.monitor = None
        return True
