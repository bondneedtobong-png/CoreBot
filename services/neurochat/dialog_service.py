from __future__ import annotations

from database.repositories import NeuroChatRepository


async def get_history_for_llm(session, account_id: int, peer_uid: int):
    return await NeuroChatRepository.get_messages_for_llm(session, account_id, peer_uid)


async def append_user_message(session, account_id: int, peer_uid: int, text: str):
    await NeuroChatRepository.append(session, account_id, peer_uid, "user", text)


async def append_assistant_message(session, account_id: int, peer_uid: int, text: str):
    await NeuroChatRepository.append(session, account_id, peer_uid, "assistant", text)

