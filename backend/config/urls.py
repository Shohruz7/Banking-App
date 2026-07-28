"""Root URL configuration.

Everything API lives under /api/v1/ (ADR-0006). The token endpoints are stubs —
SimpleJWT's stock views wired up so the auth surface exists; real auth is Week 4.
"""

from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from accounts.views import AccountViewSet
from common.views import HealthView
from ledger.views import AccountTransactionsView, TransferView

router = DefaultRouter()
router.register("accounts", AccountViewSet, basename="account")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    path("api/v1/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    # Declared ahead of the router so the account-scoped history route is unambiguous.
    path(
        "api/v1/accounts/<uuid:pk>/transactions/",
        AccountTransactionsView.as_view(),
        name="account-transactions",
    ),
    path("api/v1/transfers/", TransferView.as_view(), name="transfer-create"),
    path("api/v1/", include(router.urls)),
]
