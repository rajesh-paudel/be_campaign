from typing import List, Dict
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.conf import settings
from html import escape
import json
import resend
import logging
import requests
from .models import Campaign, CampaignRecipient, CampaignMessage, EmailUnsubscribe
from .html_generator import generate_full_email_html
from .utils import (
    SubjectLinePlaceholderHandler,
    get_recipient_data_for_subject,
    format_sender_identity,
    fetch_person,
    replace_body_placeholders_for_mailgun,
)

logger = logging.getLogger(__name__)




def send_mailgun_batch(sender_identity:str, recipients:List[CampaignRecipient], subject: str, html: str, reply_to: str, campaign:Campaign,):
    """
    Send a single email via Mailgun.
    """
    
    
    MAILGUN_DOMAIN = settings.MAILGUN_DOMAIN
    SANDBOX_DOMAIN = settings.SANDBOX_DOMAIN
    MAILGUN_API_KEY = settings.MAILGUN_API_KEY
    url=f"{MAILGUN_DOMAIN}/v3/{SANDBOX_DOMAIN}/messages"
    to_emails = []
    recipient_vars: Dict[str, Dict] = {}

    from django.core.signing import TimestampSigner
    signer = TimestampSigner()
    fe_base = getattr(settings, "FRONTEND_BASE_URL", "https://salesmonk.ca").rstrip("/")
    for r in recipients:
        if not r.email:
            continue

        to_emails.append(r.email)

        token = signer.sign(f"{campaign.user_email}:{r.email}")
        unsubscribe_url = f"{fe_base}/unsubscribe?t={token}"
        html_message=replace_body_placeholders_for_mailgun(html)
        recipient_vars[r.email] = {
            "first_name": r.first_name or "",
            "last_name": r.last_name or "",
            "full_name":r.full_name or "",
            "email":r.email or "",
           
            "unsubscribe_url": unsubscribe_url,
        }
       
        return 

    if not to_emails:
        raise Exception("No valid recipient emails in batch")
    
    data = {
        "from": sender_identity,
        "to": to_emails,
        "subject": subject,
        "html": html_message,
        "h:Reply-To": reply_to,
        "recipient-variables": json.dumps(recipient_vars),
        "o:tracking": "yes",
        "o:tracking-opens": "yes",
        "o:tracking-clicks": "yes",
        "o:tag": f"campaign-{campaign.id}",
    }

    resp = requests.post(
        url,
        auth=("api", MAILGUN_API_KEY),
        data=data,
        timeout=15,
    )

    if resp.status_code != 200:
        raise Exception(resp.text)
    
    return resp.json()


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=5, retry_backoff_max=300, retry_jitter=True, max_retries=5)
def send_campaign_task(self, campaign_id: str,token,user_info):
   
    try:
        campaign = Campaign.objects.get(pk=campaign_id)
    except Campaign.DoesNotExist:
        logger.error(f"Campaign {campaign_id} does not exist")
        return


    if not settings.MAILGUN_API_KEY or not settings.MAILGUN_DOMAIN:
        logger.error("MAILGUN config missing; aborting campaign %s", campaign_id)
        return

    
    # Mark status
    if campaign.status not in ['sending', 'scheduled', 'paused']:
        campaign.status = 'sending'
        campaign.launched_at = campaign.launched_at or timezone.now()
        campaign.save(update_fields=['status', 'launched_at', 'updated_at'])

    # ===== PERFORMANCE OPTIMIZATION: Use cached HTML or generate once =====
    # Check if we have cached HTML that's still valid
    base_html = campaign.rendered_html if campaign.rendered_html else ""
    
    # If no cached HTML or it's empty, generate and cache it now
    if not base_html or len(base_html.strip()) < 50:
        try:
            base_html = campaign.generate_and_cache_html()
        except Exception as e:
            # Mark campaign as failed if HTML generation fails
            campaign.status = 'failed'
            campaign.completed_at = timezone.now()
            campaign.save(update_fields=['status', 'completed_at', 'updated_at'])
            return
        
        # Fallback to message if HTML is still empty
        if not base_html or len(base_html.strip()) < 50:
            base_html = campaign.message or ""
    
    # Final fallback: create minimal HTML if still empty
    if not base_html or len(base_html.strip()) < 50 or '<' not in base_html:
        base_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(campaign.subject)}</title>
  <style>
    body {{ 
      font-family: Arial, sans-serif; 
      margin: 0; 
      padding: 0; 
      background-color: #f5f5f5; 
    }}
    .email-container {{ 
      max-width: 600px; 
      margin: 0 auto; 
      padding: 20px; 
      background-color: #ffffff; 
      box-shadow: 0 0 10px rgba(0,0,0,0.1);
    }}
  </style>
</head>
<body>
  <div class="email-container">
    <h2>{escape(campaign.subject)}</h2>
    <p>Hello {{{{first_name}}}},</p>
    <p>{escape(campaign.name)}</p>
  </div>
</body>
</html>"""
    # ===== END PERFORMANCE OPTIMIZATION =====

    # Acquire pending recipients
    # Exclude unsubscribed emails for this user
    suppressed_emails = list(EmailUnsubscribe.objects.filter(user_email=campaign.user_email).values_list('email', flat=True))
    
    # Reset any recipients stuck in 'sending' status from previous failed attempts
    campaign.recipients.filter(status='sending').exclude(email__in=suppressed_emails).update(status='pending', email_sent_at=None)
    
    recipients = list(
        campaign.recipients.filter(status='pending').exclude(email__in=suppressed_emails).order_by('created_at')
    )
    
    if not recipients:
        logger.warning("Campaign %s has no recipients.", campaign_id)
        # Check if all recipients are already processed
        total_recipients = campaign.recipients.count()
        if total_recipients == 0:
            campaign.status = 'failed'
        else:
            sent_count = campaign.recipients.filter(status__in=['sent', 'delivered', 'opened', 'clicked']).count()
            failed_count = campaign.recipients.filter(status='failed').count()
            if sent_count > 0:
                campaign.status = 'completed_with_errors' if failed_count > 0 else 'sent'
            else:
                campaign.status = 'failed'
        campaign.completed_at = timezone.now()
        campaign.save(update_fields=['status', 'completed_at', 'updated_at'])
        return
    
    # if recipient only has person ids and not other fields
    for recipient in recipients:
     if not recipient.email and recipient.person_id:
        person_data = fetch_person(recipient.person_id, token)
        recipient.email = person_data.get('email', '')
        recipient.first_name = person_data.get('first_name', recipient.first_name)
        recipient.last_name = person_data.get('last_name', recipient.last_name)
        recipient.save(update_fields=['email', 'first_name', 'last_name'])

    sent_count = campaign.emails_sent or 0
    failed_count = campaign.emails_failed or 0
    batch_size = 50
    
    
    for i in range(0, len(recipients), batch_size):
        batch = recipients[i : i + batch_size]
        
        with transaction.atomic():
            acquired = CampaignRecipient.objects.filter(
                pk__in=[r.pk for r in batch], status="pending"
            ).update(status="sending")

        if acquired == 0:
            continue

        try:
           
            sender_identity=format_sender_identity(user_info),
            
            
            resp_data=send_mailgun_batch(
                sender_identity=sender_identity,
                recipients=batch,
                subject=campaign.subject or "Email Campaign",
                html=base_html,
                reply_to=campaign.user_email,
                campaign=campaign,
            )

            now = timezone.now()
            for r in batch:
                r.status = "sent"
                r.email_sent_at = now
                r.error_message = ""

                CampaignMessage.objects.create(
                    campaign=campaign,
                    recipient=r,
                    provider="mailgun",
                    status="sent",
                )

            CampaignRecipient.objects.bulk_update(
                batch, ["status", "email_sent_at", "error_message"]
            )

            sent_count += len(batch)

        except Exception as e:
            for r in batch:
                r.status = "failed"
                r.error_message = str(e)

            CampaignRecipient.objects.bulk_update(batch, ["status", "error_message"])
            failed_count += len(batch)

    campaign.emails_sent = sent_count
    campaign.emails_failed = failed_count
    campaign.total_recipients = campaign.recipients.count()
    campaign.completed_at = timezone.now()

    if failed_count == 0 and sent_count > 0:
        campaign.status = "sent"
    elif sent_count > 0:
        campaign.status = "completed_with_errors"
    else:
        campaign.status = "failed"

    campaign.save(
        update_fields=[
            "status",
            "emails_sent",
            "emails_failed",
            "total_recipients",
            "completed_at",
            "updated_at",
        ]
    )

