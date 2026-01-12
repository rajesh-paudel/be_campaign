from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CampaignViewSet,
    UnsubscribeDetailView,UnsubscribeView,UnsubscribeListView,
    CampaignRecipientViewSet,
    MailgunWebhookView,
    UserEmailFooterSettingsView,
    DebugView,
)

router = DefaultRouter()
router.register(r"campaigns", CampaignViewSet, basename="campaign")
router.register(r"recipients", CampaignRecipientViewSet, basename="campaign-recipient")

urlpatterns = [
    path("", include(router.urls)),
     # Explicit analytics route to ensure resolution without relying solely on router action binding
    path(
        "campaigns/<uuid:pk>/analytics/",
        CampaignViewSet.as_view({"get": "analytics"}),
        name="campaign-analytics",
    ),
    path("webhooks/mailgun/", MailgunWebhookView.as_view(), name="mailgun-webhook"),
    path("campaigns/debug/", DebugView.as_view(), name="debug"),
    path(
        "campaigns/footer-settings/",
        UserEmailFooterSettingsView.as_view(),
        name="footer-settings",
    ),
    # Unsubscribe endpoints
    path("unsubscribe/", UnsubscribeView.as_view(), name="unsubscribe"),
    path("unsubscribes/", UnsubscribeListView.as_view(), name="unsubscribe-list"),
    path(
        "unsubscribes/<uuid:pk>/",
        UnsubscribeDetailView.as_view(),
        name="unsubscribe-detail",
    ),
]
