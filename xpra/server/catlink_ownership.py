from collections import deque

from xpra.util.env import envint


RETIRED_UUIDS_LIMIT = max(0, envint("XPRA_CATLINK_RETIRED_PROJECTION_UUIDS_LIMIT", 64))


def will_share_with(server_sharing: bool | None, new_client_share: bool,
                    existing_client_share: bool, same_client: bool) -> bool:
    if same_client:
        return False
    if server_sharing is True:
        return True
    if server_sharing is False:
        return False
    return new_client_share and existing_client_share


class CatlinkProjectionOwnership:
    """Fence reconnects from Catlink projections superseded by a newer launch."""

    __slots__ = ("owner_uuid", "retired_uuids", "_retired_uuid_set", "retired_uuids_limit")

    def __init__(self, retired_uuids_limit: int = RETIRED_UUIDS_LIMIT):
        self.owner_uuid = ""
        self.retired_uuids: deque[str] = deque()
        self._retired_uuid_set: set[str] = set()
        self.retired_uuids_limit = max(0, retired_uuids_limit)

    def is_retired(self, candidate_uuid: str) -> bool:
        return bool(candidate_uuid and candidate_uuid in self._retired_uuid_set)

    def accept(self, candidate_uuid: str) -> str:
        if not candidate_uuid or candidate_uuid == self.owner_uuid:
            return ""
        previous_owner = self.owner_uuid
        if previous_owner and self.retired_uuids_limit:
            self.retired_uuids.append(previous_owner)
            self._retired_uuid_set.add(previous_owner)
            while len(self.retired_uuids) > self.retired_uuids_limit:
                expired_uuid = self.retired_uuids.popleft()
                self._retired_uuid_set.discard(expired_uuid)
        self.owner_uuid = candidate_uuid
        return previous_owner
