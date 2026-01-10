from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CampaignViewSet,
    # CampaignGroupViewSet,
    # CampaignTemplateViewSet,
    CampaignRecipientViewSet,
    MailgunWebhookView,
    # PeopleListView,
    # DebugView,
)

router = DefaultRouter()
router.register(r"campaigns", CampaignViewSet, basename="campaign")

# router.register(r"groups", CampaignGroupViewSet, basename="campaign-group")
# router.register(r"templates", CampaignTemplateViewSet, basename="campaign-template")
router.register(r"recipients", CampaignRecipientViewSet, basename="campaign-recipient")

urlpatterns = [
    path("", include(router.urls)),
    path("webhooks/mailgun/", MailgunWebhookView.as_view(), name="mailgun-webhook"),
    # path("debug/", DebugView.as_view(), name="debug"),
]
