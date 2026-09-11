# This file is part of Xpra.
# Copyright (C) 2026 Catlink contributors

import unittest
from unittest.mock import Mock

from xpra.server.source.client_connection import ClientConnection


class ClientConnectionEncodeQueueTest(unittest.TestCase):

    def test_end_marker_follows_deferred_cleanup(self) -> None:
        connection = ClientConnection.__new__(ClientConnection)
        cleanup = Mock()
        queued = []
        connection.encode_at_end = [(cleanup, ())]
        connection.queue_encode = queued.append

        connection.stop_encode_thread()

        self.assertEqual(queued, [(False, cleanup, ()), None])
        self.assertEqual(connection.encode_at_end, [])

    def test_end_marker_is_posted_without_deferred_cleanup(self) -> None:
        connection = ClientConnection.__new__(ClientConnection)
        queued = []
        connection.encode_at_end = []
        connection.queue_encode = queued.append

        connection.stop_encode_thread()

        self.assertEqual(queued, [None])


if __name__ == "__main__":
    unittest.main()
