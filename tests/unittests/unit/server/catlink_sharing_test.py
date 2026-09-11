#!/usr/bin/env python3

import unittest

from xpra.common import ConnectionMessage, disconnect_is_an_error
from xpra.server.catlink_ownership import CatlinkProjectionOwnership, will_share_with


class CatlinkSharingTest(unittest.TestCase):

    def test_superseded_projection_cannot_reclaim_session(self):
        ownership = CatlinkProjectionOwnership()

        self.assertEqual(ownership.accept("projection-a"), "")
        self.assertFalse(ownership.is_retired("projection-a"))
        self.assertEqual(ownership.accept("projection-b"), "projection-a")
        self.assertEqual(ownership.owner_uuid, "projection-b")
        self.assertTrue(ownership.is_retired("projection-a"))

    def test_current_projection_can_reconnect(self):
        ownership = CatlinkProjectionOwnership()
        ownership.accept("projection-b")

        self.assertEqual(ownership.accept("projection-b"), "")
        self.assertEqual(ownership.owner_uuid, "projection-b")
        self.assertEqual(tuple(ownership.retired_uuids), ())

    def test_legacy_client_keeps_existing_behavior(self):
        ownership = CatlinkProjectionOwnership()

        self.assertEqual(ownership.accept(""), "")
        self.assertEqual(ownership.owner_uuid, "")
        self.assertEqual(tuple(ownership.retired_uuids), ())

    def test_retired_owners_are_bounded_and_expire_fifo(self):
        ownership = CatlinkProjectionOwnership(retired_uuids_limit=2)

        ownership.accept("projection-a")
        ownership.accept("projection-b")
        ownership.accept("projection-c")
        ownership.accept("projection-d")

        self.assertEqual(ownership.owner_uuid, "projection-d")
        self.assertEqual(tuple(ownership.retired_uuids), ("projection-b", "projection-c"))
        self.assertFalse(ownership.is_retired("projection-a"))
        self.assertTrue(ownership.is_retired("projection-b"))
        self.assertTrue(ownership.is_retired("projection-c"))

    def test_zero_limit_disables_retired_history(self):
        ownership = CatlinkProjectionOwnership(retired_uuids_limit=0)

        ownership.accept("projection-a")
        ownership.accept("projection-b")

        self.assertEqual(ownership.owner_uuid, "projection-b")
        self.assertEqual(tuple(ownership.retired_uuids), ())
        self.assertFalse(ownership.is_retired("projection-a"))

    def test_superseded_disconnect_does_not_trigger_reconnect(self):
        reason = ConnectionMessage.PROJECTION_SUPERSEDED.value

        self.assertEqual(reason, "projection superseded")
        self.assertFalse(disconnect_is_an_error(reason))

    def test_effective_sharing_matches_server_policy(self):
        self.assertTrue(will_share_with(True, False, False, False))
        self.assertFalse(will_share_with(False, True, True, False))
        self.assertTrue(will_share_with(None, True, True, False))
        self.assertFalse(will_share_with(None, False, True, False))
        self.assertFalse(will_share_with(None, True, False, False))
        self.assertFalse(will_share_with(True, True, True, True))


def main():
    unittest.main()


if __name__ == "__main__":
    main()
