from typing import List, Dict
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.conf import settings
from html import escape
import resend
import logging
import requests
from .models import Campaign, CampaignRecipient, CampaignMessage, EmailUnsubscribe
from .html_generator import generate_full_email_html
from .utils import (
    SubjectLinePlaceholderHandler,
    get_recipient_data_for_subject,
    replace_body_placeholders,
    format_sender_identity,
    fetch_person,
)

logger = logging.getLogger(__name__)


def build_html_for_recipient(campaign: Campaign, recipient: CampaignRecipient, base_html: str,token) -> str:
    """
    Build personalized HTML for a specific recipient using cached base HTML.
    Replaces dynamic placeholders (name, email, unsubscribe link) efficiently.
    
    Args:
        campaign: Campaign instance
        recipient: Recipient instance
        base_html: Pre-generated base HTML (required for performance)
    
    Returns:
        Personalized HTML ready to send
    """
   
    # Validate base_html
    if not base_html or len(base_html.strip()) < 50:
        return ""
    
    # Get recipient data for placeholder replacement
    recipient_data = get_recipient_data_for_subject(recipient,token)
   
   # Use recipient.email if present, otherwise fall back to recipient_data['email']
    email_to_use = recipient.email or recipient_data.get('email', '')

    # Generate unique unsubscribe URL for this recipient
    try:
        from django.core.signing import TimestampSigner
        signer = TimestampSigner()
        token = signer.sign(f"{campaign.user_email}:{email_to_use}")
        fe_base = getattr(settings, 'FRONTEND_BASE_URL', 'https://salesmonk.ca').rstrip('/')
        unsub_url = f"{fe_base}/unsubscribe?t={token}"
    except Exception:
        unsub_url = ""
    
    # Single-pass replacement of all placeholders including unsubscribe_url
    html_message = replace_body_placeholders(base_html, recipient_data, unsub_url)
    
    # Ensure we return valid HTML
    if not html_message or len(html_message.strip()) < 50:
        return ""
    
    return html_message



def send_email_mailgun(from_email: str, from_name: str, to_email: str, subject: str, html: str, reply_to: str, campaign_id: str, recipient_id: str):
    """
    Send a single email via Mailgun.
    """
    MAILGUN_DOMAIN = settings.MAILGUN_DOMAIN
    SANDBOX_DOMAIN = settings.SANDBOX_DOMAIN
    MAILGUN_API_KEY = settings.MAILGUN_API_KEY
    
    print("before send")
    resp = requests.post(
        f"{MAILGUN_DOMAIN}/v3/{SANDBOX_DOMAIN}/messages",
        auth=("api", MAILGUN_API_KEY),
        data={
            "from": f"Rajesh Paudel < rajesh@salesmonk.ca >",
            "to": to_email,
            "subject": subject,
            "html": html,
            "h:Reply-To": reply_to,
            "h:X-Campaign-ID": campaign_id,  
            "h:X-Recipient-ID": recipient_id ,
            "h:X-User-Email":reply_to,
            
        },
     
        timeout=10
    )
    
    return resp.json() if resp.status_code == 200 else {"error": resp.text}


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=5, retry_backoff_max=300, retry_jitter=True, max_retries=5)
def send_campaign_task(self, campaign_id: str,token):
   
    try:
        campaign = Campaign.objects.get(pk=campaign_id)
    except Campaign.DoesNotExist:
        logger.error(f"Campaign {campaign_id} does not exist")
        return


    if not settings.MAILGUN_API_KEY or not settings.MAILGUN_DOMAIN:
        logger.error("MAILGUN_API_KEY or MAILGUN_DOMAIN missing; aborting campaign %s", campaign_id)
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
    batch_size = 30
    
   
    # Process in batches
    for i in range(0, len(recipients), batch_size):
        batch = recipients[i:i+batch_size]
        
        # Lock recipients
        with transaction.atomic():
            acquired = CampaignRecipient.objects.filter(pk__in=[r.pk for r in batch], status='pending').update(status='sending')
        if acquired == 0:
            continue
        
        for r in batch:
            try:
                
                html = build_html_for_recipient(campaign, r, base_html,token)
                
                result = send_email_mailgun(
                    from_email=campaign.user_email,
                    from_name=campaign.user.full_name if hasattr(campaign, 'user') else campaign.user_email,
                    to_email=r.email,
                    subject=campaign.subject or "Email Campaign",
                    html=html,
                    reply_to=campaign.user_email,
                    campaign_id=str(campaign.id),
                    recipient_id=str(r.id)
                )
               
                # Handle Mailgun response
                if result.get("id"):
                    r.status = "sent"
                    r.email_sent_at = timezone.now()
                    CampaignMessage.objects.create(
                        campaign=campaign,
                        recipient=r,
                        provider='mailgun',
                        message_id=result.get("id"),
                        status='sent'
                    )
                    sent_count += 1
                else:
                    r.status = "failed"
                    r.error_message = result.get("error", "Unknown error")
                    failed_count += 1
                
                r.save(update_fields=['status', 'email_sent_at', 'error_message', 'updated_at'])
            
            except Exception as e:
                r.status = "failed"
                r.error_message = str(e)
                r.save(update_fields=['status', 'error_message', 'updated_at'])
                failed_count += 1
    
    # Update campaign stats
    campaign.emails_sent = sent_count
    campaign.emails_failed = failed_count
    campaign.total_recipients = campaign.recipients.count()
    campaign.completed_at = timezone.now()
    
    if failed_count == 0 and sent_count > 0:
        campaign.status = 'sent'
    elif sent_count > 0:
        campaign.status = 'completed_with_errors'
    else:
        campaign.status = 'failed'
    
    campaign.save(update_fields=['status', 'emails_sent', 'emails_failed', 'total_recipients', 'completed_at', 'updated_at'])
    


