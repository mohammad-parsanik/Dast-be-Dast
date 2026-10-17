from django.contrib import admin
from django.http import HttpRequest

from .models import BotAdmin


@admin.register(BotAdmin)
class BotAdminAdmin(admin.ModelAdmin):
    """Who may use the bot's admin menu: users and roles are superuser-only (PLAN §4.7)."""

    list_display = ("user", "bale_user_id", "is_active", "receives_digest")
    autocomplete_fields = ("user",)

    def has_module_permission(self, request: HttpRequest) -> bool:
        return request.user.is_superuser

    def has_view_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return request.user.is_superuser

    def has_add_permission(self, request: HttpRequest) -> bool:
        return request.user.is_superuser

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return request.user.is_superuser

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return request.user.is_superuser
