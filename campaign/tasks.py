import time
import requests
from celery import shared_task
from django.utils import timezone
from django.conf import settings
from .models import EmailCampaign, EmailRecipient

MAILGUN_API_KEY = settings.MAILGUN_API_KEY
MAILGUN_DOMAIN = settings.MAILGUN_DOMAIN

@shared_task
def send_campaign(campaign_id):
    campaign = EmailCampaign.objects.get(id=campaign_id)
    recipients = campaign.recipients.filter(status='pending')
    campaign.status = 'sending'
    campaign.launched_at = timezone.now()
    campaign.save()

    success_count = 0
    failed_count = 0

    for recipient in recipients:
        try:
            response = requests.post(
                f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data={
                    "from": campaign.sender_email,
                    "to": recipient.email,
                    "subject": campaign.subject,
                    "html": campaign.message,
                    "h:Reply-To": campaign.sender_email,
                },
                timeout=30
            )
            if response.status_code == 200:
                recipient.status = 'sent'
                success_count += 1
            else:
                recipient.status = 'failed'
                recipient.error_message = response.text[:200]
                failed_count += 1
            recipient.save()
            time.sleep(1)  # simple throttling
        except Exception as e:
            recipient.status = 'failed'
            recipient.error_message = str(e)
            recipient.save()
            failed_count += 1

    campaign.emails_sent = success_count
    campaign.emails_failed = failed_count
    campaign.status = 'sent' if failed_count == 0 else 'failed'
    campaign.save()
