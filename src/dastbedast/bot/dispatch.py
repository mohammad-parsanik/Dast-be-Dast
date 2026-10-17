"""Route Bale updates to the applicant or admin conversation. Only private chats are served."""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth.models import AbstractBaseUser

from dastbedast.core import bot_flow
from dastbedast.core.bot_flow import Incoming, Reply

from . import admin_flow, applicant, ui
from .bale import BaleError, BotApi
from .models import BotAdmin, BotSession

log = logging.getLogger(__name__)

APPLY = "apply"
HELP = "برای ثبت درخواست وام /start و برای دیدن وضعیت درخواست /status را بزنید."


class Bot:
    def __init__(self, api: BotApi) -> None:
        self.api = api

    def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.on_callback(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])

    @staticmethod
    def admin_user(bale_user_id: int) -> AbstractBaseUser | None:
        link = BotAdmin.objects.select_related("user").filter(bale_user_id=bale_user_id, is_active=True).first()
        if link is None or not link.user.is_active or not link.user.is_staff:
            return None
        return link.user

    @staticmethod
    def session_for(bale_user_id: int, chat_id: int) -> BotSession:
        session, _ = BotSession.objects.get_or_create(bale_user_id=bale_user_id, defaults={"chat_id": chat_id})
        if session.chat_id != chat_id:
            session.chat_id = chat_id
            session.save(update_fields=["chat_id"])
        if session.expired():
            session.reset()
        return session

    def on_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if chat.get("type") != "private" or "id" not in sender:
            return
        user_id, chat_id = int(sender["id"]), int(chat["id"])
        text = msg.get("text")
        if text and text.strip() == "/whoami":
            ui.send(self.api, chat_id, f"شناسه بله شما: {user_id}")
            return
        session = self.session_for(user_id, chat_id)
        user = self.admin_user(user_id)
        if user is not None:
            chat_ = admin_flow.AdminChat(self.api, session, user, chat_id)
            if text and text.strip().startswith("/start"):
                chat_.on_start()
            elif text:
                chat_.on_text(text)
            return
        self.on_applicant(session, chat_id, user_id, msg)

    def on_applicant(self, session: BotSession, chat_id: int, user_id: int, msg: dict[str, Any]) -> None:
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            pending = applicant.open_request(user_id)
            if pending is not None:
                session.reset()
                ui.send(self.api, chat_id, Reply(applicant.status_text(pending), remove_keyboard=True))
                return
            out = bot_flow.begin()
        elif text == "/status":
            person = applicant.person_for(user_id)
            latest = person.loan_requests.order_by("-created_at").first() if person else None
            ui.send(self.api, chat_id, applicant.status_text(latest) if latest else "درخواستی از شما ثبت نشده است.")
            return
        elif session.flow == APPLY:
            out = bot_flow.handle(session.step, session.data, incoming(msg, user_id))
        else:
            ui.send(self.api, chat_id, HELP)
            return
        if out.submitted is not None:
            if applicant.open_request(user_id) is not None:
                session.reset()
                ui.send(self.api, chat_id, Reply("درخواست دیگری از شما در حال بررسی است.", remove_keyboard=True))
                return
            request = applicant.submit(out.submitted, user_id)
            session.reset()
            ui.send(self.api, chat_id, Reply(applicant.status_text(request), remove_keyboard=True))
            log.info("loan request %s submitted via bot", request.pk)
            admin_flow.notify_new_request(self.api, request)
            return
        if out.step:
            session.go(APPLY, out.step, out.data)
        else:
            session.reset()
        for reply in out.replies:
            ui.send(self.api, chat_id, reply)

    def on_callback(self, query: dict[str, Any]) -> None:
        sender = query.get("from") or {}
        message = query.get("message") or {}
        try:
            user_id = int(sender["id"])
            user = self.admin_user(user_id)
            if user is not None and query.get("data"):
                chat_id = int((message.get("chat") or {}).get("id", user_id))
                session = self.session_for(user_id, chat_id)
                chat = admin_flow.AdminChat(self.api, session, user, chat_id)
                chat.on_callback(query["data"], message.get("message_id"))
        finally:
            # Bale keeps the button spinning until the query is answered; old clients cannot be answered.
            try:
                self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            except BaleError:
                log.info("callback query not answered")


def incoming(msg: dict[str, Any], user_id: int) -> Incoming:
    contact = msg.get("contact")
    if contact:
        return Incoming(phone=str(contact.get("phone_number", "")), own_contact=contact.get("user_id") == user_id)
    return Incoming(text=msg.get("text"))
