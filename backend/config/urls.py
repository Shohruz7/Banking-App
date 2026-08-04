"""Root URL configuration.

Everything API lives under /api/v1/ (ADR-0006). The auth surface is Week 4's: registration, a
two-step MFA login, rotating refresh with reuse detection, and logout that actually revokes
(ADR-0012, ADR-0013). None of the stock SimpleJWT views survive — each is subclassed to bind
tokens to a revocable session.
"""

from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from accounts.views import AccountViewSet
from common.views import HealthView
from identity.views import (
    LoginView,
    LogoutView,
    MeView,
    MfaConfirmView,
    MfaDisableView,
    MfaEnrollView,
    MFAVerifyView,
    RegisterView,
    SessionRefreshView,
)
from ledger.views import AccountTransactionsView, TransferView
from markets.views import InstrumentPricesView, InstrumentViewSet
from statements.views import StatementDownloadView, StatementListView
from trading.views import (
    HoldingsView,
    OrderCancelView,
    OrderDetailView,
    OrderListCreateView,
    PortfolioView,
)

router = DefaultRouter()
router.register("accounts", AccountViewSet, basename="account")
router.register("instruments", InstrumentViewSet, basename="instrument")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    # Auth: register → token (→ mfa) → refresh → logout, plus MFA management.
    path("api/v1/auth/register/", RegisterView.as_view(), name="register"),
    path("api/v1/auth/me/", MeView.as_view(), name="me"),
    path("api/v1/auth/token/", LoginView.as_view(), name="token_obtain_pair"),
    path("api/v1/auth/token/mfa/", MFAVerifyView.as_view(), name="token_mfa_verify"),
    path("api/v1/auth/token/refresh/", SessionRefreshView.as_view(), name="token_refresh"),
    path("api/v1/auth/logout/", LogoutView.as_view(), name="logout"),
    path("api/v1/auth/mfa/enroll/", MfaEnrollView.as_view(), name="mfa-enroll"),
    path("api/v1/auth/mfa/confirm/", MfaConfirmView.as_view(), name="mfa-confirm"),
    path("api/v1/auth/mfa/disable/", MfaDisableView.as_view(), name="mfa-disable"),
    # Declared ahead of the router so the account-scoped history route is unambiguous.
    path(
        "api/v1/accounts/<uuid:pk>/transactions/",
        AccountTransactionsView.as_view(),
        name="account-transactions",
    ),
    path("api/v1/transfers/", TransferView.as_view(), name="transfer-create"),
    # Declared ahead of the router so the instrument-scoped price route is unambiguous, the same
    # reason the account-scoped history route is declared above.
    path(
        "api/v1/instruments/<str:symbol>/prices/",
        InstrumentPricesView.as_view(),
        name="instrument-prices",
    ),
    # Brokerage: place and manage orders, and read the positions they produced (Week 5).
    path("api/v1/orders/", OrderListCreateView.as_view(), name="order-list-create"),
    path("api/v1/orders/<uuid:pk>/", OrderDetailView.as_view(), name="order-detail"),
    path("api/v1/orders/<uuid:pk>/cancel/", OrderCancelView.as_view(), name="order-cancel"),
    path("api/v1/holdings/", HoldingsView.as_view(), name="holdings"),
    # Reporting (Week 6): what everything is worth now, and what a closed month looked like.
    path("api/v1/portfolio/", PortfolioView.as_view(), name="portfolio"),
    path("api/v1/statements/", StatementListView.as_view(), name="statement-list"),
    path(
        "api/v1/statements/<uuid:pk>/download/",
        StatementDownloadView.as_view(),
        name="statement-download",
    ),
    path("api/v1/", include(router.urls)),
]
