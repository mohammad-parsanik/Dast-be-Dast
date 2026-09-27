from datetime import datetime

import jdatetime
import pytest
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.utils import timezone

from dastbedast.fund import services
from dastbedast.fund.models import FundAccount, Person, Transaction

M = 1_000_000


def jat(y: int, m: int, d: int, hh: int = 12) -> datetime:
    g = jdatetime.datetime(y, m, d, hh, 0).togregorian()
    return timezone.make_aware(g)


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("owner", password="x")


@pytest.fixture
def operator(db):
    call_command("setup_roles", verbosity=0)
    user = User.objects.create_user("op", password="x", is_staff=True)
    user.groups.add(Group.objects.get(name="operator"))
    return User.objects.get(pk=user.pk)  # fresh instance: no cached permissions


@pytest.fixture
def account(db):
    return FundAccount.objects.create(bank="pasargad", label="Main", account_hint="201.8000")


@pytest.fixture
def opened(account, superuser):
    tx = Transaction.objects.create(fund_account=account, kind="opening", amount=10 * M, occurred_at=jat(1405, 7, 1))
    services.confirm(tx, superuser)
    return account


@pytest.fixture
def lender(db):
    return Person.objects.create(name="Lender A")


@pytest.fixture
def borrower(db):
    return Person.objects.create(name="Borrower B", referrer_text="Friend C")
