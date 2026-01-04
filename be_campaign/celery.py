import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "be_campaign.settings")

app = Celery("be_campaign")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
