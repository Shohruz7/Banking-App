"""ASGI entrypoint — HTTP and WebSocket on one application (ADR-0022).

``get_asgi_application()`` is called *before* the routing import, and the order is load-bearing: it
is what populates the app registry, and ``realtime.routing`` reaches a consumer that imports
models. Importing routing first fails with ``AppRegistryNotReady``.

``AllowedHostsOriginValidator`` checks the handshake's ``Origin`` against ``ALLOWED_HOSTS``. Without
it any page on the internet could open an authenticated socket against this server from a visitor's
browser — the WebSocket equivalent of having no CSRF protection at all.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

django_asgi_application = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from realtime.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_application,
        # No AuthMiddlewareStack: sessions and cookies authenticate nothing here. The socket
        # authenticates itself with a bearer token in its first frame (ADR-0022).
        "websocket": AllowedHostsOriginValidator(URLRouter(websocket_urlpatterns)),
    }
)
