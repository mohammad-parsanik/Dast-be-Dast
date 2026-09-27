from django.conf import settings
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from dastbedast.fund.admin import dashboard_view, payout_export_view

if settings.OTP_REQUIRED:
    from django_otp.admin import OTPAdminSite

    admin.site.__class__ = OTPAdminSite

admin.site.site_header = "دست به دست"
admin.site.site_title = "دست به دست"
admin.site.index_title = "مدیریت صندوق"

urlpatterns = [
    path("admin/fund/dashboard/", admin.site.admin_view(dashboard_view), name="fund_dashboard"),
    path("admin/fund/payouts.csv", admin.site.admin_view(payout_export_view), name="fund_payout_export"),
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
]
