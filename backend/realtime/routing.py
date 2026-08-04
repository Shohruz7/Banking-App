"""WebSocket routes. One endpoint, versioned like the HTTP API (ADR-0006)."""

from django.urls import path

from .consumers import StreamConsumer

websocket_urlpatterns = [
    path("ws/v1/stream/", StreamConsumer.as_asgi(), name="stream"),
]
