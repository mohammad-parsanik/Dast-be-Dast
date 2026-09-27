"""Create or refresh the ``operator`` group (PLAN §4.7). Safe to run repeatedly."""

from typing import Any

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

OPERATOR = "operator"

# Operators do all day-to-day work. What can break the data or the ledger stays with the superuser:
# deleting anything, fund accounts, opening balances, users and roles. Core terms of an active agreement
# are locked in the admin for non-superusers.
SUPERUSER_ONLY = {"add_fundaccount", "change_fundaccount", "record_opening_balance"}


def operator_permissions() -> list[Permission]:
    perms = Permission.objects.filter(content_type__app_label="fund").exclude(codename__startswith="delete_")
    return [p for p in perms if p.codename not in SUPERUSER_ONLY]


class Command(BaseCommand):
    help = "Create or refresh the operator group's permissions."

    def handle(self, *args: Any, **options: Any) -> None:
        group, _ = Group.objects.get_or_create(name=OPERATOR)
        perms = operator_permissions()
        group.permissions.set(perms)
        self.stdout.write(self.style.SUCCESS(f"'{OPERATOR}' group has {len(perms)} permissions."))
