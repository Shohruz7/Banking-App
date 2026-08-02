"""The Celery application (ADR-0019).

One app, named for the project package, configured entirely from Django settings under the
``CELERY_`` namespace so there is a single place configuration lives (ADR-0004). Tasks are
autodiscovered from every installed app's ``tasks`` module.

``config/__init__.py`` imports this so that ``manage.py``, ``celery -A config worker`` and
``celery -A config beat`` all bind to the same instance — without that import, a task registered
during a web request would be invisible to the worker.
"""

import os

from celery import Celery

# The worker has no manage.py to set this for it. dev is the right default for the same reason
# manage.py defaults to it: production is selected explicitly, never fallen into.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
