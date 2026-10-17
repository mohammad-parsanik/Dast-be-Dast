import pytest

from dastbedast.core import bot_flow as bf
from dastbedast.core.bot_flow import Incoming

IBAN = "IR160120000000001234567890"  # made-up, valid checksum


def run(answers):
    out = bf.begin()
    for answer in answers:
        out = bf.handle(out.step, out.data, answer if isinstance(answer, Incoming) else Incoming(text=answer))
    return out


HAPPY = [
    "Test Applicant",
    Incoming(phone="989121234567", own_contact=True),
    "Friend C",
    "colleague",
    "۲۰ میلیون",
    "درمان",
    "surgery costs",
    "ir16 0120 0000 0000 1234 5678 90",
    bf.SAME_NAME,
    bf.REPAYMENTS["default"],
    bf.SKIP,
]


def test_happy_path_reaches_review_then_submits():
    out = run(HAPPY)
    assert out.step == "review"
    assert "20,000,000 تومان" in out.replies[-1].text
    done = bf.handle(out.step, out.data, Incoming(text=bf.SUBMIT))
    app = done.submitted
    assert app is not None and done.step == ""
    assert app.phone == "+989121234567"
    assert app.amount == 200_000_000  # rials
    assert app.purpose == "medical"
    assert app.iban == IBAN
    assert app.holder_name == "Test Applicant"
    assert app.referrer_text == "Friend C — colleague"
    assert app.notes == ""


def test_phone_must_come_from_own_contact():
    out = run(HAPPY[:1])
    assert out.step == "phone" and out.replies[-1].ask_contact
    typed = bf.handle(out.step, out.data, Incoming(text="09121234567"))
    assert typed.step == "phone"
    forwarded = bf.handle(out.step, out.data, Incoming(phone="989121234567", own_contact=False))
    assert forwarded.step == "phone"
    landline = bf.handle(out.step, out.data, Incoming(phone="982188888888", own_contact=True))
    assert landline.step == "phone"


def test_bad_iban_is_asked_again():
    out = run(HAPPY[:7])
    assert out.step == "iban"
    again = bf.handle(out.step, out.data, Incoming(text=IBAN[:-1] + "3"))
    assert again.step == "iban" and "شبا" in again.replies[0].text


@pytest.mark.parametrize(
    ("typed", "rials"),
    [
        ("20,000,000", 200_000_000),
        ("۲۰٬۰۰۰٬۰۰۰ تومان", 200_000_000),
        ("1.5 میلیون", 15_000_000),
        ("500 هزار", 5_000_000),
    ],
)
def test_parse_tomans(typed, rials):
    assert bf.parse_tomans(typed) == rials


@pytest.mark.parametrize("typed", ["0", "abc", "-5", "99999999999"])
def test_parse_tomans_rejects(typed):
    with pytest.raises(ValueError):
        bf.parse_tomans(typed)


def test_cancel_anywhere():
    out = run(HAPPY[:4])
    cancelled = bf.handle(out.step, out.data, Incoming(text=bf.CANCEL))
    assert cancelled.cancelled and cancelled.step == "" and cancelled.data == {}


def test_restart_from_review_and_custom_holder_and_notes():
    answers = [*HAPPY[:8], "Other Holder", bf.REPAYMENTS["contact"], "call evenings"]
    out = run(answers)
    assert out.step == "review"
    assert out.data["holder"] == "Other Holder" and out.data["repayment"] == "contact"
    restarted = bf.handle(out.step, out.data, Incoming(text=bf.RESTART))
    assert restarted.step == "name" and restarted.data == {}


def test_commands_are_not_accepted_as_answers():
    out = bf.begin()
    again = bf.handle(out.step, out.data, Incoming(text="/help"))
    assert again.step == "name"
