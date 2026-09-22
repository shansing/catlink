#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

# pylint: disable=line-too-long

import unittest
from threading import Event, Thread, current_thread, local
from unittest.mock import Mock, patch

from xpra.os_util import WIN32, POSIX, OSX
from xpra.util.objects import AdHocStruct
from xpra.client.subsystem.audio import AudioClient
from xpra.audio.common import AUDIO_DATA_PACKET
from xpra.net.common import Packet
from xpra.audio.gstreamer_util import CODEC_ORDER
from unit.client.subsystem.clientmixintest_util import ClientMixinTest


def default_audio_options() -> AdHocStruct:
    opts = AdHocStruct()
    opts.av_sync = True
    opts.speaker = "no"
    opts.microphone = "no"
    opts.audio_source = ""
    opts.speaker_codec = []
    opts.microphone_codec = []
    opts.tray_icon = ""
    return opts


class AudioClientTestUtil(ClientMixinTest):

    @classmethod
    def setUpClass(cls):
        ClientMixinTest.setUpClass()
        from xpra.net import packet_encoding
        packet_encoding.init_all()
        from xpra.net import compression
        compression.init_all()

    def _test_audio(self, opts, caps):
        return self._test_mixin_class(AudioClient, opts, caps)


class AudioClientSendTestUtil(AudioClientTestUtil):

    def do_test_audio_send(self, auto_start=True):
        opts = default_audio_options()
        opts.microphone = "on" if auto_start else "off"
        self._test_audio(opts, {
            "audio": {
                "receive": True,
                "decoders": CODEC_ORDER,
            },
        })
        if not self.mixin.microphone_codecs:
            print("no microphone codecs, test skipped")
            return

        def check_packets():
            if len(self.packets) < 5:
                return True
            self.mixin.stop_sending_audio()
            self.main_loop.quit()
            return False
        if not auto_start:
            def request_start():
                self.mixin.start_sending_audio()
            self.glib.timeout_add(500, request_start)
        self.glib.timeout_add(100, check_packets)
        self.glib.timeout_add(5000, self.main_loop.quit)
        self.main_loop.run()
        assert len(self.packets) > 2
        self.verify_packet(0, (AUDIO_DATA_PACKET, ))
        assert self.packets[0][3].get("start-of-stream"), "start-of-stream not found"
        self.verify_packet(-1, (AUDIO_DATA_PACKET, ))
        assert self.packets[-1][3].get("end-of-stream"), "end-of-stream not found"


class AudioClientSendAuto(AudioClientSendTestUtil):

    def test_audio_send_auto(self):
        self.do_test_audio_send(True)


class AudioClientSendRequest(AudioClientSendTestUtil):

    def test_audio_send_request(self):
        self.do_test_audio_send(False)


class AudioClientReceiveTest(AudioClientTestUtil):

    def test_audio_receive(self):
        opts = default_audio_options()
        opts.speaker = "yes"
        x = self._test_audio(opts, {
            "audio": {
                "send": True,
                "encoders": CODEC_ORDER,
            },
        })

        def stop() -> None:
            x.stop_receiving_audio()
            self.stop()
        if not self.mixin.speaker_codecs:
            stop()
            print("no speaker codecs, test skipped")
        if "opus" not in self.mixin.speaker_codecs:
            stop()
            print("'opus' speaker codec missing, test skipped")
            return

        packet_data = [
            ('audio-data', 'opus', b'', {'start-of-stream': True, 'codec': b'opus'}),
            ('audio-data', 'opus', b'fc60fddad634b19a5baa6dd0ae26e05d15106df1135c84590fa2ab85d9945dd504a1d7b54d3ea189b276b36909ee33d34f038fd0d4aa25baa6abd1cd6b896bbfab52e02b9b18bc260fc4441bc44a65b2e0428431aafeabc3cc4974c9a4afe02df92638c51c1eb36292662b710ee971fb16361692e6fb819a1ef7bc66a6badd04d71160c16b249aaf497e79cf56c622cffeb6bcfd83027954132d5d90500104a4',
             {b'duration': 13500000, b'timestamp': 0, b'time': 159963539, 'sequence': 0}),
            ('audio-data', 'opus', b'fc6e8120f7b82efd4dec6f067d0f4af7ee27220307090f8595517139761252409ea93e4ba08d59b009a96efabc9a99f5895c2b084db0ab0ccc9665dc54e58a3a1dcacb6156fab84b6031c74b4f353ffb16c68c0959510e63d1f632a95358ee673d969c210f18b1dd765204a857c631e281960b9988d4821a34bf5d0729cf4696d8a00dfa08115cdd6de84b9bc8f216d6924e071018ebb6d4ac3c3d8298cb1813',
             {b'duration': 20000000, b'timestamp': 13500000, b'time': 159963558, 'sequence': 0}),
            ('audio-data', 'opus', b'fc6dd5d0f08acc6ae38aca6dc1038b2da49ca1fa69220405c8556f553e6352680ad0c403b94285a32259dd9455d157090ab602ac2256b4426779083ed9f119fe261de3fb7bafd3e1508cf38b47a7ff62d8d15443c095bd8ca63d152a6b1dcce71d70c79763163c2ed5e804eafffa0eb267b59bb35a1a34bdd7074a8f1696d8a406f0b4c457377ebc25cdadb789b5a49381c4063a44c7b6d4ac2bebebef316d77',
             {b'duration': 20000000, b'timestamp': 33500000, b'time': 159963576, 'sequence': 0}),
            ('audio-data', 'opus', b'fc4d2dfba53a9041a76a560e128ed23c27e6ba1a5dd7aaf9fa0ef2c801e748dc8a7262ca5821ccee59a209ccd97be931a62b02651d11af09e7d26aedbbb52a490a75463dccc52843b488e9ffd8b45ff6e5fe3cbcb4e872170cee673a82d953af2e3ce570ca82176af40df8db59735cdcdecfe0444d0faeebce583aa70241c05d1406f0c06d7bddd6defadc6b6de26d6924e071017dbfdb25645f0055f3d569a5',
             {b'duration': 20000000, b'timestamp': 53500000, b'time': 159963596, 'sequence': 0}),
            ('audio-data', 'opus', b'fc4d2e0df8779efc983d56a4881571c36a50ce90597b0be6a8d4d9194d5620c2c0bd0b9d7ecb0b8e724e7a9acbb9e9cf1d5ec31635f1bfa99ec410c49fc09bed96b9dd5a9b372c0e09a9ffd8ab8bfe3cbcdb97f2170cd3a1ddcce7505b2824c81aefefec32a085dabd037e36d65f5fd652f9c33d96673b2bcedf4d2a21d7d5dd1406f0c06d7bddd6defadc6b6de26d6924e071017d8fdb25645f50105c730413',
             {b'duration': 20000000, b'timestamp': 73500000, b'time': 159963616, 'sequence': 0}),
            ('audio-data', 'opus', b'', {'end-of-stream': True, 'sequence': 0}),
        ]
        L = len(packet_data)

        def feed_data() -> bool:
            packet = packet_data.pop(0)
            self.handle_packet(packet)
            if packet_data:
                return True
            self.mixin.stop_receiving_audio()
            self.main_loop.quit()
            return False

        def check_start() -> bool:
            if not self.packets:
                return True
            self.verify_packet(0, ("audio-control", "start", "opus"))
            self.glib.timeout_add(100, feed_data)
            return False

        self.glib.timeout_add(100, check_start)
        self.glib.timeout_add(5000, stop)
        # self.debug_all()
        self.main_loop.run()
        assert len(packet_data) < L, "none of the data was fed to the receiver"
        assert not packet_data, "not all the data was fed to the receiver: remains: %s" % len(packet_data)
        self.verify_packet(0, ("audio-control", "start", "opus"))
        self.verify_packet(1, ("audio-control", "new-sequence", 1))
        # assert not self.packets, "sent some unexpected packets: %s" % (self.packets,)
        assert self.mixin.audio_sink is None, "sink is still active: %s" % self.mixin.audio_sink


class AudioDeviceRestartTest(unittest.TestCase):
    def setUp(self):
        self.client = AudioClient()
        self.client.exit_code = None
        self.client.server_audio_send = True
        self.client.send = Mock()
        self.client.emit = Mock()
        self.client._speaker_requested = True
        self.client.speaker_enabled = True
        self.client.speaker_codecs = ["opus"]
        self.client.server_audio_encoders = ["opus"]
        self.sink = Mock(sequence=0, codec="opus")
        self.sink.get_state.return_value = "active"
        self.client.audio_sink = self.sink
        self.timers = {}
        self.delays = {}
        self.next_timer = 0

        def timeout(delay, callback):
            self.next_timer += 1
            self.timers[self.next_timer] = callback
            self.delays[self.next_timer] = delay
            return self.next_timer

        def remove(timer):
            self.timers.pop(timer)
            self.delays.pop(timer)

        self.timeout_patch = patch("xpra.client.subsystem.audio.GLib.timeout_add", side_effect=timeout)
        self.remove_patch = patch("xpra.client.subsystem.audio.GLib.source_remove",
                                  side_effect=remove)
        self.timeout_patch.start()
        self.remove_patch.start()

    def tearDown(self):
        self.client.cleanup()
        self.remove_patch.stop()
        self.timeout_patch.stop()

    def fire_timer(self):
        timer, callback = self.timers.popitem()
        self.delays.pop(timer)
        callback()

    def test_change_coalesces_and_keeps_monitor(self):
        monitor = self.client._audio_device_monitor = Mock()
        self.client._audio_device_changed()
        self.assertIsNone(self.client.audio_sink)
        self.assertEqual(self.client.audio_sink_sequence, 1)
        self.assertEqual(len(self.timers), 1)
        self.client._audio_device_changed()
        self.assertEqual(len(self.timers), 1)
        monitor.stop.assert_not_called()
        restart = self.client.start_receiving_audio = Mock()
        self.fire_timer()
        restart.assert_called_once_with()

    def test_manual_stop_cancels_restart(self):
        self.client._audio_device_changed()
        self.client.stop_receiving_audio()
        self.assertFalse(self.client._speaker_requested)
        self.assertFalse(self.timers)

    def test_cleanup_cancels_restart_and_monitor(self):
        monitor = self.client._audio_device_monitor = Mock()
        self.client._audio_device_changed()
        self.client.cleanup()
        self.assertFalse(self.timers)
        monitor.stop.assert_called_once_with()

    def test_unavailable_device_waits_for_next_change(self):
        restart = self.client.start_receiving_audio = Mock()
        self.client._audio_device_changed()
        self.fire_timer()
        self.assertEqual(list(self.delays.values()), [2000])
        self.client._audio_device_changed()
        self.assertEqual(list(self.delays.values()), [1000])
        self.fire_timer()
        self.assertEqual(restart.call_count, 2)

    def test_generic_error_during_recovery_keeps_monitor(self):
        monitor = self.client._audio_device_monitor = Mock()
        self.client._audio_device_changed()
        self.client.audio_sink = self.sink
        self.client.speaker_enabled = True
        self.client.audio_sink_error(self.sink, "output temporarily unavailable")
        self.assertTrue(self.client._speaker_requested)
        self.assertTrue(self.client._audio_device_recovering)
        self.assertIsNone(self.client.audio_sink)
        self.assertEqual(list(self.delays.values()), [2000])
        monitor.stop.assert_not_called()

    def test_retries_back_off_and_stop_on_manual_disable(self):
        self.client.start_receiving_audio = Mock(
            side_effect=lambda: self.client.audio_sink_error(None, "AUDIO_DEVICE_CHANGED"))
        self.client._audio_device_changed()
        for delay in (1000, 2000, 4000, 8000, 16000, 30000):
            self.assertEqual(list(self.delays.values()), [delay])
            self.fire_timer()
        self.assertEqual(list(self.delays.values()), [30000])
        self.client.stop_receiving_audio()
        self.assertFalse(self.timers)
        self.assertFalse(self.client._audio_device_recovering)

    def test_generic_errors_notify_then_wait_for_device_change(self):
        monitor = self.client._audio_device_monitor = Mock()
        notify = self.client.may_notify_audio = Mock()
        self.client._audio_device_changed()
        for attempt in range(3):
            self.client.audio_sink = self.sink
            self.client.speaker_enabled = True
            self.client.audio_sink_error(self.sink, "decoder failure")
            self.assertEqual(self.client._audio_generic_failures, attempt + 1)
        self.assertFalse(self.client._audio_device_recovering)
        self.assertTrue(self.client._speaker_requested)
        self.assertFalse(self.timers)
        notify.assert_called_once_with("Speaker forwarding error", "decoder failure")
        monitor.stop.assert_not_called()
        self.client._audio_device_changed()
        self.assertEqual(self.client._audio_generic_failures, 0)
        self.assertEqual(list(self.delays.values()), [1000])

    def test_failed_subprocess_start_is_bounded(self):
        self.client.may_notify_audio = Mock()
        self.client.start_receiving_audio = Mock()
        self.client._audio_device_changed()
        for _ in range(3):
            self.fire_timer()
        self.assertFalse(self.timers)
        self.assertFalse(self.client._audio_device_recovering)
        self.client.may_notify_audio.assert_called_once()

    def test_process_exit_during_recovery_is_bounded(self):
        self.client.may_notify_audio = Mock()
        self.client._audio_device_changed()
        for _ in range(3):
            self.client.audio_sink = self.sink
            self.client.audio_process_stopped(self.sink)
        self.assertFalse(self.timers)
        self.client.may_notify_audio.assert_called_once()

    def test_stable_playback_ends_recovery(self):
        def restart():
            self.client.audio_sink = self.sink
            self.client.speaker_enabled = True

        self.client.start_receiving_audio = Mock(side_effect=restart)
        self.client._audio_device_changed()
        self.fire_timer()
        self.assertEqual(list(self.delays.values()), [15000])
        self.fire_timer()
        self.assertFalse(self.client._audio_device_recovering)
        self.client.audio_sink_error(self.sink, "unrelated decoder failure")
        self.assertFalse(self.timers)
        self.assertFalse(self.client._speaker_requested)

    def test_paused_recovery_waits_for_first_audio_data(self):
        def restart():
            self.client.audio_sink = self.sink
            self.client.speaker_enabled = True

        self.client.start_receiving_audio = Mock(side_effect=restart)
        self.sink.get_state.return_value = "paused"
        self.client._audio_device_changed()
        self.fire_timer()
        for _ in range(3):
            self.assertEqual(list(self.delays.values()), [15000])
            self.fire_timer()
            self.assertIs(self.client.audio_sink, self.sink)
            self.assertTrue(self.client._audio_device_recovering)
            self.assertEqual(self.client._audio_generic_failures, 0)
        self.sink.get_state.return_value = "active"
        self.fire_timer()
        self.assertFalse(self.client._audio_device_recovering)
        self.assertFalse(self.timers)

    def test_paused_recovery_with_audio_data_retries(self):
        def restart():
            self.client.audio_sink = self.sink
            self.client.speaker_enabled = True

        self.client.start_receiving_audio = Mock(side_effect=restart)
        self.sink.get_state.return_value = "paused"
        self.client.av_sync = False
        self.client._audio_device_changed()
        self.fire_timer()
        self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"data", {
            "sequence": self.client.audio_sink_sequence}))
        self.sink.add_data.assert_called_once()
        self.fire_timer()
        self.assertIsNone(self.client.audio_sink)
        self.assertEqual(self.client._audio_generic_failures, 1)
        self.assertEqual(list(self.delays.values()), [2000])

    def test_ignored_old_audio_does_not_fail_paused_recovery(self):
        def restart():
            self.client.audio_sink = self.sink
            self.client.speaker_enabled = True

        self.client.start_receiving_audio = Mock(side_effect=restart)
        self.sink.get_state.return_value = "paused"
        self.client._audio_device_changed()
        self.fire_timer()
        self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"old data", {
            "sequence": self.client.audio_sink_sequence - 1}))
        self.sink.add_data.assert_not_called()
        self.fire_timer()
        self.assertIs(self.client.audio_sink, self.sink)
        self.assertEqual(self.client._audio_generic_failures, 0)

    def test_stuck_sink_is_retried(self):
        def restart():
            self.client.audio_sink = self.sink
            self.client.speaker_enabled = True

        self.client.start_receiving_audio = Mock(side_effect=restart)
        self.sink.get_state.return_value = "starting"
        self.client._audio_device_changed()
        self.fire_timer()
        self.fire_timer()
        self.assertEqual(list(self.delays.values()), [2000])
        self.assertIsNone(self.client.audio_sink)
        self.assertTrue(self.client._speaker_requested)

    def test_suspend_cancels_recovery_without_active_sink(self):
        self.client._audio_device_changed()
        self.client.suspend()
        self.assertTrue(self.client.audio_resume_restart)
        self.assertFalse(self.timers)
        self.assertFalse(self.client._speaker_requested)

    def test_stale_sink_error_does_not_restart(self):
        self.client.audio_sink_error(Mock(), "AUDIO_DEVICE_CHANGED")
        self.assertFalse(self.timers)
        self.assertIs(self.client.audio_sink, self.sink)

    def test_windows_device_invalidation(self):
        with patch("xpra.client.subsystem.audio.WIN32", True):
            self.client.audio_sink_error(self.sink, "AUDCLNT_E_DEVICE_INVALIDATED (0x88890004)")
        self.assertIsNone(self.client.audio_sink)
        self.assertEqual(len(self.timers), 1)
        self.assertTrue(self.client._speaker_requested)

    def test_monitor_unregisters_on_manual_stop(self):
        with patch("xpra.client.subsystem.audio.OSX", True), \
                patch("xpra.audio.device_monitor.AudioDeviceMonitor") as monitor_class:
            self.client._start_device_monitor()
            monitor_class.return_value.start.assert_called_once()
            self.client.stop_receiving_audio()
            monitor_class.return_value.stop.assert_called_once()

    def test_pending_unregister_does_not_stop_reenabled_monitor(self):
        monitor = self.client._audio_device_monitor = Mock()
        callbacks = []
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=False), \
                patch("xpra.client.subsystem.audio.GLib.idle_add",
                      side_effect=lambda callback, *args: callbacks.append((callback, args)) or 17):
            self.client.stop_receiving_audio()
            self.client._speaker_requested = True
            self.client._start_device_monitor()
            callback, args = callbacks[0]
            self.assertFalse(callback(*args))
        monitor.stop.assert_not_called()
        self.assertIs(self.client._audio_device_monitor, monitor)

    def test_failed_unregister_is_rearmed_on_next_start(self):
        monitor = self.client._audio_device_monitor = Mock()
        monitor.stop.return_value = False
        self.client.stop_receiving_audio()
        self.assertIs(self.client._audio_device_monitor, monitor)
        self.client._speaker_requested = True
        with patch("xpra.client.subsystem.audio.WIN32", True):
            self.client._start_device_monitor()
        monitor.start.assert_called_once_with(self.client._audio_device_changed)

    def test_failed_registration_retries_while_speaker_is_requested(self):
        self.client._audio_device_monitor = None
        monitor = Mock()
        monitor.start.side_effect = (False, True)
        monitor.monitor = None
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.audio.device_monitor.AudioDeviceMonitor", return_value=monitor):
            self.client._start_device_monitor()
            self.assertIsNone(self.client._audio_device_monitor)
            self.assertEqual(list(self.delays.values()), [1000])
            self.fire_timer()
            self.assertIs(self.client._audio_device_monitor, monitor)
            self.assertFalse(self.timers)
            self.assertEqual(self.client._audio_monitor_retry_delay, 1000)
            self.assertEqual(monitor.start.call_count, 2)

    def test_manual_stop_cancels_failed_registration_retry(self):
        self.client._audio_device_monitor = None
        monitor = Mock()
        monitor.start.return_value = False
        monitor.monitor = None
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.audio.device_monitor.AudioDeviceMonitor", return_value=monitor):
            self.client._start_device_monitor()
            self.assertEqual(list(self.delays.values()), [1000])
            self.client.stop_receiving_audio()
        self.assertFalse(self.timers)
        monitor.start.assert_called_once()

    def test_monitor_registers_before_audio_sink(self):
        from xpra.audio.device_monitor import AudioDeviceMonitor as Monitor
        calls = []
        self.client.audio_sink = None
        self.client.audio_loop_check = Mock(return_value=True)
        self.client.start_audio_sink = Mock(side_effect=lambda _codec: calls.append("sink") or True)
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=True), \
                patch.object(Monitor, "start", side_effect=lambda _self, _cb: calls.append("monitor") or True,
                             autospec=True):
            self.client.start_receiving_audio()
        self.assertEqual(calls, ["monitor", "sink"])

    def test_failed_sink_start_clears_request_and_monitor(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.audio_loop_check = Mock(return_value=True)
        self.client.start_audio_sink = Mock(return_value=False)
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=True), \
                patch("xpra.audio.device_monitor.AudioDeviceMonitor") as monitor_class:
            self.client.start_receiving_audio()
            monitor_class.return_value.start.assert_called_once()
            monitor_class.return_value.stop.assert_called_once()
        self.assertFalse(self.client.speaker_enabled)
        self.assertFalse(self.client._speaker_requested)
        self.assertIsNone(self.client._audio_device_monitor)
        self.assertFalse(self.timers)

    def test_failed_sink_start_cancels_monitor_registration_retry(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.audio_loop_check = Mock(return_value=True)
        self.client.start_audio_sink = Mock(return_value=False)
        monitor = Mock()
        monitor.monitor = None
        monitor.start.return_value = False
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=True), \
                patch("xpra.audio.device_monitor.AudioDeviceMonitor", return_value=monitor):
            self.client.start_receiving_audio()
        self.assertFalse(self.client._speaker_requested)
        self.assertFalse(self.timers)

    def test_failed_sink_start_during_recovery_keeps_retry(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.audio_loop_check = Mock(return_value=True)
        self.client.start_audio_sink = Mock(return_value=False)
        monitor = self.client._audio_device_monitor = Mock()
        self.client._audio_device_recovering = True
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=True):
            self.client._restart_after_device_change()
        self.assertTrue(self.client._speaker_requested)
        self.assertTrue(self.client._audio_device_recovering)
        self.assertEqual(list(self.delays.values()), [1000])
        monitor.stop.assert_not_called()

    def test_failed_server_requested_sink_start_clears_monitor(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.speaker_allowed = True
        self.client.start_audio_sink = Mock(return_value=False)
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=True), \
                patch("xpra.audio.device_monitor.AudioDeviceMonitor") as monitor_class:
            self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"", {
                "start-of-stream": True, "codec": "opus"}))
            monitor_class.return_value.stop.assert_called_once()
        self.assertFalse(self.client._speaker_requested)
        self.assertFalse(self.client.speaker_enabled)
        self.assertIsNone(self.client._audio_device_monitor)

    def test_queued_start_is_invalidated_by_manual_stop(self):
        self.client.audio_sink = None
        self.client.start_audio_sink = Mock()
        callbacks = []

        def idle(callback, *args):
            callbacks.append((callback, args))
            return 17

        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=False), \
                patch("xpra.client.subsystem.audio.GLib.idle_add", side_effect=idle), \
                patch("xpra.client.subsystem.audio.GLib.source_remove"):
            self.client.start_receiving_audio()
            self.client.start_audio_sink.assert_not_called()
            self.client.stop_receiving_audio()
            callback, args = callbacks[0]
            self.assertFalse(callback(*args))
        self.client.start_audio_sink.assert_not_called()

    def test_stop_after_queued_start_check_closes_new_sink(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.audio_loop_check = Mock(return_value=True)
        sink = Mock(codec="opus", sequence=0)
        callbacks = []
        callback_thread = [None]
        starting = Event()
        stopping = Event()
        stopped = Event()
        resume = Event()
        errors = []
        original_stop = self.client.stop_receiving_audio

        def start_sink(_codec):
            starting.set()
            if not resume.wait(2):
                errors.append("timed out waiting for concurrent stop")
            self.client.audio_sink = sink
            return True

        def stop():
            stopping.set()
            original_stop()
            stopped.set()

        self.client.start_audio_sink = start_sink
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread",
                      side_effect=lambda: current_thread() is callback_thread[0]), \
                patch("xpra.client.subsystem.audio.GLib.idle_add",
                      side_effect=lambda callback, *args: callbacks.append((callback, args)) or 17), \
                patch("xpra.audio.device_monitor.AudioDeviceMonitor"):
            self.client.start_receiving_audio()
            callback, args = callbacks[0]
            callback_thread[0] = Thread(target=lambda: callback(*args))
            callback_thread[0].start()
            stopper = None
            try:
                self.assertTrue(starting.wait(2))
                stopper = Thread(target=stop)
                stopper.start()
                self.assertTrue(stopping.wait(2))
                self.assertFalse(stopped.wait(0.05))
            finally:
                resume.set()
                callback_thread[0].join(2)
                if stopper:
                    stopper.join(2)
        self.assertFalse(callback_thread[0].is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertFalse(errors)
        self.assertTrue(stopped.is_set())
        self.assertIsNone(self.client.audio_sink)
        self.assertFalse(self.client._speaker_requested)
        sink.cleanup.assert_called_once()

    def test_early_idle_callback_does_not_block_next_start(self):
        self.client.audio_sink = None
        self.client.audio_loop_check = Mock(return_value=True)
        start_sink = self.client.start_audio_sink = Mock(return_value=False)
        on_main = [False]

        def idle(callback, *args):
            on_main[0] = True
            try:
                callback(*args)
            finally:
                on_main[0] = False
            return 17

        from xpra.audio.device_monitor import AudioDeviceMonitor as Monitor
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", side_effect=lambda: on_main[0]), \
                patch("xpra.client.subsystem.audio.GLib.idle_add", side_effect=idle), \
                patch.object(Monitor, "start", return_value=True):
            self.client.start_receiving_audio()
            self.assertEqual(self.client._audio_start_idle, 0)
            self.client.start_receiving_audio()
            self.assertEqual(self.client._audio_start_idle, 0)
        self.assertEqual(start_sink.call_count, 2)

    def test_concurrent_idle_callback_and_source_id_assignment(self):
        self.client.audio_sink = None
        self.client.audio_loop_check = Mock(return_value=True)
        start_sink = self.client.start_audio_sink = Mock(return_value=False)
        thread_state = local()
        callback_entered = Event()
        callback_threads = []

        def idle(callback, *args):
            def run_callback():
                thread_state.on_main = True
                callback_entered.set()
                callback(*args)

            thread = Thread(target=run_callback)
            callback_threads.append(thread)
            thread.start()
            self.assertTrue(callback_entered.wait(2))
            return 17

        from xpra.audio.device_monitor import AudioDeviceMonitor as Monitor
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread",
                      side_effect=lambda: getattr(thread_state, "on_main", False)), \
                patch("xpra.client.subsystem.audio.GLib.idle_add", side_effect=idle), \
                patch.object(Monitor, "start", return_value=True):
            worker = Thread(target=self.client.start_receiving_audio)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            callback_threads[0].join(2)
            self.assertFalse(callback_threads[0].is_alive())
            self.assertEqual(self.client._audio_start_idle, 0)
            callback_entered.clear()
            worker = Thread(target=self.client.start_receiving_audio)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            callback_threads[1].join(2)
            self.assertFalse(callback_threads[1].is_alive())
        self.assertEqual(start_sink.call_count, 2)

    def test_server_start_packets_register_monitor_before_sink_and_preserve_order(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.speaker_allowed = True
        self.client.av_sync = False
        calls = []
        idle_callbacks = []
        main_thread = [False]
        sink = Mock(codec="opus")
        sink.get_state.return_value = "active"

        def start_sink(_codec):
            calls.append("sink")
            self.client.audio_sink = sink
            return True

        def add_data(data, _metadata, _packet_metadata):
            calls.append(data)
            if data == b"first":
                main_thread[0] = False
                self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"second", {}))
                main_thread[0] = True

        sink.add_data.side_effect = add_data
        self.client.start_audio_sink = Mock(side_effect=start_sink)
        from xpra.audio.device_monitor import AudioDeviceMonitor as Monitor
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", side_effect=lambda: main_thread[0]), \
                patch("xpra.client.subsystem.audio.GLib.idle_add",
                      side_effect=lambda callback, *args: idle_callbacks.append((callback, args)) or 17), \
                patch.object(Monitor, "start", side_effect=lambda _monitor, _cb: calls.append("monitor") or True,
                             autospec=True):
            self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"", {
                "start-of-stream": True, "codec": "opus"}))
            self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"first", {}))
            self.assertEqual(len(idle_callbacks), 1)
            main_thread[0] = True
            callback, args = idle_callbacks.pop(0)
            self.assertFalse(callback(*args))
            self.assertEqual(calls, ["monitor", "sink", b"first", b"second"])

    def test_queued_eos_preserves_next_stream(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.speaker_allowed = True
        self.client.av_sync = False
        callbacks = []
        on_main = [False]
        sinks = []

        def start_sink(_codec):
            sink = Mock(codec="opus", sequence=self.client.audio_sink_sequence)
            sink.get_state.return_value = "active"
            sinks.append(sink)
            self.client.audio_sink = sink
            return True

        self.client.start_audio_sink = Mock(side_effect=start_sink)
        packets = (
            Packet(AUDIO_DATA_PACKET, "opus", b"", {"start-of-stream": True, "codec": "opus", "sequence": 0}),
            Packet(AUDIO_DATA_PACKET, "opus", b"first", {"sequence": 0}),
            Packet(AUDIO_DATA_PACKET, "opus", b"", {"end-of-stream": True, "sequence": 0}),
            Packet(AUDIO_DATA_PACKET, "opus", b"", {"start-of-stream": True, "codec": "opus", "sequence": 1}),
            Packet(AUDIO_DATA_PACKET, "opus", b"second", {"sequence": 1}),
        )
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", side_effect=lambda: on_main[0]), \
                patch("xpra.client.subsystem.audio.GLib.idle_add",
                      side_effect=lambda callback, *args: callbacks.append((callback, args)) or 17), \
                patch("xpra.audio.device_monitor.AudioDeviceMonitor"):
            for packet in packets:
                self.client._process_audio_data(packet)
            self.assertEqual(len(callbacks), 1)
            on_main[0] = True
            callback, args = callbacks.pop()
            self.assertFalse(callback(*args))
        self.assertEqual(len(sinks), 2)
        sinks[0].add_data.assert_called_once()
        sinks[0].cleanup.assert_called_once()
        sinks[1].add_data.assert_called_once_with(b"second", {"sequence": 1}, ())
        self.assertIs(self.client.audio_sink, sinks[1])
        self.assertTrue(self.client.speaker_enabled)
        self.assertFalse(self.client._speaker_start_packets)
        self.assertFalse(self.client._speaker_start_pending)

    def test_queue_overflow_retains_both_stream_boundaries(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        callbacks = []
        from xpra.client.subsystem.audio import MAX_SPEAKER_START_PACKETS
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=False), \
                patch("xpra.client.subsystem.audio.GLib.idle_add",
                      side_effect=lambda callback, *args: callbacks.append((callback, args)) or 17):
            first = Packet(AUDIO_DATA_PACKET, "opus", b"", {"start-of-stream": True})
            eos = Packet(AUDIO_DATA_PACKET, "opus", b"", {"end-of-stream": True})
            second = Packet(AUDIO_DATA_PACKET, "opus", b"", {"start-of-stream": True})
            for packet in (first, eos, second):
                self.client._process_audio_data(packet)
            for _ in range(300):
                self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"data", {}))
            self.assertEqual(len(self.client._speaker_start_packets), MAX_SPEAKER_START_PACKETS)
            self.assertEqual(list(self.client._speaker_start_packets)[:3], [first, eos, second])
            self.assertEqual(len(callbacks), 1)

    def test_stopped_server_start_packet_cannot_reopen_sink(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.start_audio_sink = Mock()
        callbacks = []
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=False), \
                patch("xpra.client.subsystem.audio.GLib.idle_add",
                      side_effect=lambda callback, *args: callbacks.append((callback, args)) or 17):
            self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"", {
                "start-of-stream": True, "codec": "opus"}))
            self.client.stop_receiving_audio()
            callback, args = callbacks[0]
            self.assertFalse(callback(*args))
        self.client.start_audio_sink.assert_not_called()

    def test_stopped_after_dequeue_cannot_reopen_sink(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.speaker_allowed = True
        self.client.start_audio_sink = Mock()
        callbacks = []
        dequeued = Event()
        resume = Event()
        original_process = self.client._process_audio_data
        errors = []
        drain_thread = [None]

        def process(packet, generation=None):
            if generation is not None:
                dequeued.set()
                if not resume.wait(2):
                    errors.append("timed out waiting to resume drain")
                    return
            original_process(packet, generation)

        self.client._process_audio_data = process
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread",
                      side_effect=lambda: current_thread() is drain_thread[0]), \
                patch("xpra.client.subsystem.audio.GLib.idle_add",
                      side_effect=lambda callback, *args: callbacks.append((callback, args)) or 17):
            self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"", {
                "start-of-stream": True, "codec": "opus"}))
            callback, args = callbacks[0]
            drain = Thread(target=lambda: callback(*args))
            drain_thread[0] = drain
            drain.start()
            try:
                self.assertTrue(dequeued.wait(2))
                self.client.stop_receiving_audio()
            finally:
                resume.set()
                drain.join(2)
        self.assertFalse(drain.is_alive())
        self.assertFalse(errors)
        self.client.start_audio_sink.assert_not_called()
        self.assertFalse(self.client.speaker_enabled)

    def test_stop_during_sink_creation_closes_new_sink(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        self.client.speaker_allowed = True
        self.client.av_sync = False
        sink = Mock(codec="opus", sequence=0)
        entered = Event()
        stopping = Event()
        resume = Event()
        errors = []
        original_cancel = self.client._cancel_audio_start

        def cancel():
            stopping.set()
            original_cancel()

        def start_sink(_codec):
            entered.set()
            if not resume.wait(2):
                errors.append("timed out waiting to finish sink startup")
            self.client.audio_sink = sink
            return True

        self.client._cancel_audio_start = cancel
        self.client.start_audio_sink = start_sink
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=True), \
                patch.object(self.client, "_start_device_monitor"):
            start = Thread(target=lambda: self.client._process_audio_data(Packet(
                AUDIO_DATA_PACKET, "opus", b"", {"start-of-stream": True, "codec": "opus"})))
            start.start()
            try:
                self.assertTrue(entered.wait(2))
                stop = Thread(target=self.client.stop_receiving_audio)
                stop.start()
                self.assertTrue(stopping.wait(2))
            finally:
                resume.set()
                start.join(2)
                if stopping.is_set():
                    stop.join(2)
        self.assertFalse(start.is_alive())
        self.assertFalse(stop.is_alive())
        self.assertFalse(errors)
        self.assertIsNone(self.client.audio_sink)
        sink.cleanup.assert_called_once()

    def test_server_start_queue_is_bounded_and_keeps_header_and_recent_data(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        callbacks = []
        from xpra.client.subsystem.audio import MAX_SPEAKER_START_BYTES, MAX_SPEAKER_START_PACKETS
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=False), \
                patch("xpra.client.subsystem.audio.GLib.idle_add",
                      side_effect=lambda callback, *args: callbacks.append((callback, args)) or 17):
            header = Packet(AUDIO_DATA_PACKET, "opus", b"", {"start-of-stream": True, "codec": "opus"})
            self.client._process_audio_data(header)
            for i in range(400):
                self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", bytes([i % 256]), {}))
            self.assertEqual(len(self.client._speaker_start_packets), MAX_SPEAKER_START_PACKETS)
            self.assertIs(self.client._speaker_start_packets[0], header)
            self.assertEqual(self.client._speaker_start_packets[-1].get_buffer(2), bytes([399 % 256]))
            for _ in range(100):
                self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"x" * 65536, {}))
            self.assertLessEqual(self.client._speaker_start_bytes, MAX_SPEAKER_START_BYTES)
            self.assertIs(self.client._speaker_start_packets[0], header)
            self.assertEqual(len(callbacks), 1)
            self.client.stop_receiving_audio()
        self.assertFalse(self.client._speaker_start_packets)
        self.assertEqual(self.client._speaker_start_bytes, 0)

    def test_oversized_start_packet_does_not_bypass_queue_limit(self):
        self.client.audio_sink = None
        self.client.speaker_enabled = False
        callbacks = []
        from xpra.client.subsystem.audio import MAX_SPEAKER_START_BYTES
        with patch("xpra.client.subsystem.audio.WIN32", True), \
                patch("xpra.client.subsystem.audio.is_main_thread", return_value=False), \
                patch("xpra.client.subsystem.audio.GLib.idle_add",
                      side_effect=lambda callback, *args: callbacks.append((callback, args)) or 17):
            oversized = b"x" * (MAX_SPEAKER_START_BYTES + 1)
            self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", oversized, {
                "start-of-stream": True, "codec": "opus"}))
            self.assertFalse(callbacks)
            self.assertFalse(self.client._speaker_start_packets)
            self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", b"data", {}))
            self.assertFalse(self.client._speaker_start_packets)
            header = Packet(AUDIO_DATA_PACKET, "opus", b"", {"start-of-stream": True, "codec": "opus"})
            self.client._process_audio_data(header)
            self.client._process_audio_data(Packet(AUDIO_DATA_PACKET, "opus", oversized, {}))
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(list(self.client._speaker_start_packets), [header])
            self.assertEqual(self.client._speaker_start_bytes, 0)


def main():
    if WIN32:
        return
    if POSIX and not OSX:
        # verify that pulseaudio is running:
        # otherwise the tests will fail
        # ie: during rpmbuild
        from subprocess import getstatusoutput
        if getstatusoutput("pactl info")[0]!=0:
            return
    unittest.main()


if __name__ == '__main__':
    main()
