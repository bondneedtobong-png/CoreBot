from types import SimpleNamespace
import re

from bot.handlers.accounts.groups import _render_template
from bot.handlers.mailing import _mailing_list_page_from_data
from workers.manager import WorkerManager
from utils.neuro_sampling import (
    merge_sampling_for_request,
    parse_sampling_user_input,
)
from utils.neuro_prompts import apply_neuro_prompt_placeholders


def test_mailing_list_page_from_data():
    assert _mailing_list_page_from_data("mailing_list") == 0
    assert _mailing_list_page_from_data("mailing_list_p_2") == 2
    assert _mailing_list_page_from_data("other") == 0


def test_render_template_with_known_fields():
    account = SimpleNamespace(
        id=7,
        phone="+1234567890",
        username="john_doe",
        first_name="John",
        last_name="Doe",
    )
    out = _render_template(
        "acc_{i}_{group}_{username}_{first_name}_{last_name}_{phone}_{id}",
        account,
        4,
        "COL",
    )
    assert out == "acc_4_COL_john_doe_John_Doe_+1234567890_7"


def test_render_template_unknown_placeholder_becomes_empty():
    account = SimpleNamespace(
        id=1,
        phone="100",
        username="u",
        first_name="A",
        last_name="B",
    )
    out = _render_template("x_{unknown}_y", account, 1, "G")
    assert out == "x__y"


def test_parse_sampling_user_input_key_value():
    d = parse_sampling_user_input("temperature=0.5\ntop_p=0.9")
    assert abs(d["temperature"] - 0.5) < 1e-9
    assert abs(d["top_p"] - 0.9) < 1e-9


def test_merge_sampling_defaults_and_override():
    m = merge_sampling_for_request({"top_p": 0.88})
    assert m["max_tokens"] == 0
    assert abs(m["temperature"] - 0.7) < 1e-9
    assert abs(m["top_p"] - 0.88) < 1e-9
    assert m["top_k"] == 40


def test_neuro_prompt_placeholders_account_and_peer():
    acc = SimpleNamespace(
        id=3,
        phone="+100",
        username="mgr",
        first_name="Иван",
        last_name="Петров",
    )
    mailing = SimpleNamespace(id=9, name="Кампания A")
    client = SimpleNamespace(username="lead_user")
    peer = SimpleNamespace(first_name="Анна", last_name="Ли", username="anna")
    raw = "Менеджер {first_name} {last_name} {username} id={account_id} | {mailing} #{mailing_id} | {link} | peer {peer_first_name} {peer_username}"
    out = apply_neuro_prompt_placeholders(
        raw,
        link="https://x.example/join",
        account=acc,
        mailing=mailing,
        client=client,
        peer_sender=peer,
    )
    assert "Иван" in out and "Петров" in out
    assert "@mgr" in out
    assert "id=3" in out
    assert "Кампания A" in out and "#9" in out
    assert "https://x.example/join" in out
    assert "Анна" in out and "@anna" in out


def test_apply_template_timezone_placeholders():
    wm = WorkerManager()
    out = wm.apply_template(
        "{username} {timezone} {time+3} {datetime-2} {date+1} {timezone-1} {fullname}",
        "demo_user",
    )
    assert "@demo_user" in out
    assert re.search(r"UTC[+-]\d{2}:00", out)
    assert "{time+3}" not in out
    assert "{datetime-2}" not in out
    assert "{date+1}" not in out
    assert "{timezone-1}" not in out
