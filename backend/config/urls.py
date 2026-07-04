"""Root URL configuration.

Everything API lives under /api/v1/ (ADR-0006). The token endpoints are stubs —
SimpleJWT's stock views wired up so the auth surface exists; real auth is Week 4.
"""

from django.contrib import admin
from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from common.views import HealthView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    path("api/v1/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]
