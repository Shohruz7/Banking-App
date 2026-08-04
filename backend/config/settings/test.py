"""Test settings — the fourth member of the ADR-0004 split, selected by pytest.

Identical to dev except for two things the suite needs and dev must not have:

* a fast password hasher, because Week 4 gave ``UserFactory`` a real password and Django 5.2's
  default PBKDF2 runs ~1.2M iterations per ``set_password`` call, which across the whole suite is
  seconds of pure waste;
* an explicitly-named local-memory cache, so throttle state (ADR-0015) never depends on a Redis
  being up and can be cleared between tests by the autouse fixture in ``tests/conftest.py``;
* an in-process channel layer, for the same reason — and with the same caveat as eager Celery: it
  proves the consumer's protocol, not that Redis fans out across processes.
"""

import tempfile

from .dev import *

# Test-only: fast and deliberately insecure. Never reachable from dev.py or prod.py.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "banking-tests",
    }
}

# Celery runs inline (ADR-0019): `.delay()` executes in-process, so the suite needs no worker and
# CI needs no Redis service. Note that `transaction.on_commit` callbacks still do not fire inside
# an ordinary `django_db` test — the enclosing transaction never commits — so a test that asserts
# on a chained task must use the `django_capture_on_commit_callbacks` fixture.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_STORE_EAGER_RESULT = True

# The socket's protocol is testable in-process; the Redis fan-out is not, and is verified by hand
# (ADR-0022). `WebsocketCommunicator` talks to the consumer directly, so this layer only carries
# group_send traffic between a publisher and a consumer inside one test.
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Statement PDFs go to a throwaway directory: the suite writes real files through the storage API
# (that is the code path prod uses) and none of them should survive the run.
MEDIA_ROOT = tempfile.mkdtemp(prefix="banking-test-media-")
