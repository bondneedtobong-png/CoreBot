"""The Telegram target guard rejects foreign groups and changed messages."""
import asyncio
from types import SimpleNamespace

import pytest
from telethon.tl.types import Channel, ChannelParticipantCreator, ChannelParticipant

from services.engagement_guard import EngagementTargetError, inspect_managed_message


def _channel(channel_id=123, *, megagroup=True):
    return Channel(id=channel_id, title="Managed discussion", photo=None, date=None,
                   megagroup=megagroup, broadcast=False)


class Client:
    def __init__(self, *, channel_id=123, admin=True, text="Source message"):
        self.channel_id = channel_id
        self.admin = admin
        self.text = text

    async def get_entity(self, _peer):
        return _channel(self.channel_id)

    async def __call__(self, _request):
        member = (ChannelParticipantCreator(user_id=1, admin_rights=None) if self.admin
                  else ChannelParticipant(user_id=1, date=None))
        return SimpleNamespace(participant=member)

    async def get_messages(self, _entity, *, ids):
        return SimpleNamespace(id=ids, raw_text=self.text)


def test_inspection_accepts_admin_and_exact_message():
    result = asyncio.run(inspect_managed_message(
        Client(), "https://t.me/c/123/45", expected_peer_id=123,
        expected_text="Source message",
    ))
    assert result["peer_ref"] == "-100123"
    assert result["source_text"] == "Source message"


@pytest.mark.parametrize("client,reason", [
    (Client(admin=False), "admin_required"),
    (Client(channel_id=999), "target_changed"),
    (Client(text="Edited"), "message_changed"),
])
def test_inspection_rejects_unmanaged_or_changed(client, reason):
    with pytest.raises(EngagementTargetError, match=reason):
        asyncio.run(inspect_managed_message(
            client, "https://t.me/c/123/45", expected_peer_id=123,
            expected_text="Source message",
        ))


def test_telegram_exception_text_is_not_exposed():
    class BrokenClient:
        async def get_entity(self, _peer):
            raise RuntimeError("phone=secret proxy=password")

    with pytest.raises(EngagementTargetError) as caught:
        asyncio.run(inspect_managed_message(BrokenClient(), "https://t.me/c/123/45"))
    assert str(caught.value) == "target_unavailable"
