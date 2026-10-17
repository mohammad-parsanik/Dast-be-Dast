from datetime import date

import pytest

from dastbedast.bot.dispatch import Bot
from dastbedast.bot.models import BotAdmin
from dastbedast.fund import services
from tests.fund.conftest import account, borrower, jat, lender, opened, operator, superuser  # noqa: F401

ADMIN_ID = 5_000_000_001  # Bale ids can exceed 2^31
APPLICANT_ID = 7_000_000_002
TODAY = date(2026, 10, 17)  # 1405/07/25


class FakeApi:
    def __init__(self):
        self.calls = []
        self._next_id = 100

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self._next_id += 1
            return {"message_id": self._next_id}
        return True

    def sent(self, chat_id=None):
        return [p for m, p in self.calls if m in ("sendMessage", "editMessageText") and chat_id in (None, p["chat_id"])]

    def last_text(self, chat_id=None):
        return self.sent(chat_id)[-1]["text"]

    def buttons(self, chat_id=None):
        markup = self.sent(chat_id)[-1].get("reply_markup") or {}
        return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(services, "today", lambda: TODAY)


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def bot(api, db):
    return Bot(api)


@pytest.fixture
def bot_admin(operator):  # noqa: F811 - fixture imported from tests.fund
    BotAdmin.objects.create(user=operator, bale_user_id=ADMIN_ID)
    return operator


_ids = iter(range(1, 10**6))


def message(user_id, text=None, contact=None):
    msg = {"message_id": next(_ids), "from": {"id": user_id}, "chat": {"id": user_id, "type": "private"}}
    if text is not None:
        msg["text"] = text
    if contact is not None:
        msg["contact"] = contact
    return {"update_id": next(_ids), "message": msg}


def callback(user_id, data, message_id=55):
    return {
        "update_id": next(_ids),
        "callback_query": {
            "id": f"5{next(_ids)}",
            "from": {"id": user_id},
            "message": {"message_id": message_id, "chat": {"id": user_id, "type": "private"}},
            "data": data,
        },
    }
