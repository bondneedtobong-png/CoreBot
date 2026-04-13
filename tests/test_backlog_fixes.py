from types import SimpleNamespace

from bot.handlers.accounts.groups import _render_template
from bot.handlers.clients import _clients_list_page_from_data
from bot.handlers.mailing import _mailing_list_page_from_data


def test_clients_list_page_from_data():
    assert _clients_list_page_from_data("clients_list") == 0
    assert _clients_list_page_from_data("clients_list_p_3") == 3
    assert _clients_list_page_from_data("other") == 0


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
