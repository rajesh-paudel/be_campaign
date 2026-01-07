from typing import List, Dict
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.conf import settings
from html import escape
import resend
import logging

from .models import Campaign, CampaignRecipient, CampaignMessage, EmailUnsubscribe
from .html_generator import generate_full_email_html
from .utils import (
    SubjectLinePlaceholderHandler,
    get_recipient_data_for_subject,
    replace_body_placeholders,
    format_sender_identity,
)

logger = logging.getLogger(__name__)


def _build_html_for_recipient(campaign: Campaign, recipient: CampaignRecipient, base_html: str) -> str:
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
    recipient_data = get_recipient_data_for_subject(recipient)
    
    # Generate unique unsubscribe URL for this recipient
    try:
        from django.core.signing import TimestampSigner
        signer = TimestampSigner()
        token = signer.sign(f"{campaign.user_email}:{recipient.email}")
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


def _build_params_for_batch(campaign: Campaign, recipients: List[CampaignRecipient], base_html: str) -> List[Dict]:
    """
    Build email parameters for a batch of recipients.
    Efficiently personalizes cached HTML for each recipient.
    
    Args:
        campaign: Campaign instance
        recipients: List of recipients
        base_html: Pre-generated base HTML (shared template for all recipients)
    
    Returns:
        List of email parameter dictionaries ready for Resend batch API
    """
    params: List[Dict] = []
    sender_identity = format_sender_identity(campaign.user_email)
    
    # Extract sender email for unsubscribe header
    sender_email = None
    if '<' in sender_identity and '>' in sender_identity:
        try:
            sender_email = sender_identity.split('<', 1)[1].split('>', 1)[0].strip()
        except Exception:
            pass
    
    # Build parameters for each recipient
    for r in recipients:
        recipient_data = get_recipient_data_for_subject(r)
        
        # Personalize subject line - ensure it's not empty
        subject = campaign.subject or "Email Campaign"
        if subject:
            subject = SubjectLinePlaceholderHandler.replace_placeholders(subject, recipient_data)
        if not subject or not subject.strip():
            subject = "Email Campaign"
        
        # Personalize HTML content (single efficient pass)
        html = _build_html_for_recipient(campaign, r, base_html)
        
        # Ensure HTML is not empty - use base_html as fallback
        if not html or len(html.strip()) < 50:
            html = base_html  # Fallback to base HTML if personalization fails
        
        # Generate unique unsubscribe URL for headers
        try:
            from django.core.signing import TimestampSigner
            signer = TimestampSigner()
            token = signer.sign(f"{campaign.user_id}:{r.email}")
            fe_base = getattr(settings, 'FRONTEND_BASE_URL', 'https://salesmonk.ca').rstrip('/')
            unsub_url = f"{fe_base}/unsubscribe?t={token}"
            
            # Build List-Unsubscribe header
            mailto = f"mailto:{sender_email}?subject=unsubscribe" if sender_email else None
            list_unsub = f"<{unsub_url}>{', ' + f'<{mailto}>' if mailto else ''}"
        except Exception:
            unsub_url = None
            list_unsub = None
        
        # Validate required fields
        if not r.email or not r.email.strip():
            continue  # Skip recipients without email
        
        # Build email parameters
        email_params = {
            "from": sender_identity,
            "to": [r.email.strip()],
            "subject": subject.strip() if subject else "Email Campaign",
            "html": html,
            "reply_to": campaign.user_email if getattr(campaign.user, 'email', None) else None,
            "headers": {
                "X-Campaign-ID": str(campaign.id),
                "X-Recipient-ID": str(r.id),
                "X-User-ID": str(campaign.user_email),
                "X-Campaign-Name": str(campaign.name) if campaign.name else "",
            }
        }
        
        # Add optional headers
        if list_unsub:
            email_params["headers"]["List-Unsubscribe"] = list_unsub
            email_params["headers"]["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        
        # Add attachments if present
        if campaign.attachments:
            email_params["attachments"] = campaign.attachments
        
        params.append(email_params)
    
    return params


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=5, retry_backoff_max=300, retry_jitter=True, max_retries=5)
def send_campaign_task(self, campaign_id: str):
    campaign = Campaign.objects.get(pk=campaign_id)
    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY missing; aborting campaign %s", campaign_id)
        return

    resend.api_key = settings.RESEND_API_KEY

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
    suppressed_emails = list(EmailUnsubscribe.objects.filter(user=campaign.user).values_list('email', flat=True))
    
    # Reset any recipients stuck in 'sending' status from previous failed attempts
    campaign.recipients.filter(status='sending').exclude(email__in=suppressed_emails).update(status='pending', email_sent_at=None)
    
    recipients = list(
        campaign.recipients.filter(status='pending').exclude(email__in=suppressed_emails).order_by('created_at')
    )
    
    if not recipients:
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

    sent_count = campaign.emails_sent or 0
    failed_count = campaign.emails_failed or 0

    # Process in batches of up to 100
    batch_size = 100
    idx = 0
    while idx < len(recipients):
        batch = recipients[idx: idx + batch_size]
        idx += batch_size

        # lock: set status to 'sending' to avoid double-send
        with transaction.atomic():
            acquired = CampaignRecipient.objects.filter(pk__in=[r.pk for r in batch], status='pending').update(status='sending')
        if acquired == 0:
            continue

        # Validate base_html before sending
        if not base_html or len(base_html.strip()) < 50:
            for r in batch:
                r.status = 'failed'
                r.error_message = 'Invalid HTML content'
                r.save(update_fields=['status', 'error_message', 'updated_at'])
                failed_count += 1
            continue

        # Pass pre-generated base_html to avoid regenerating for each recipient
        params = _build_params_for_batch(campaign, batch, base_html)
        
        # Validate params before sending
        if not params or len(params) == 0:
            for r in batch:
                r.status = 'failed'
                r.error_message = 'Failed to build email parameters'
                r.save(update_fields=['status', 'error_message', 'updated_at'])
                failed_count += 1
            continue
        
        try:
            result = resend.Batch.send(params)  # returns list of results with ids
            # According to resend python client, returns {'data': [{'id': '...', ...}, ...]} or list
            items = []
            if isinstance(result, dict) and 'data' in result:
                items = result['data']
            elif isinstance(result, list):
                items = result

            # Map responses back to recipients by order
            for i, r in enumerate(batch):
                try:
                    resp = items[i] if i < len(items) else {}
                    
                    # Check for error in response
                    if isinstance(resp, dict) and 'error' in resp:
                        r.status = 'failed'
                        r.error_message = str(resp.get('error', 'Unknown error'))
                        r.save(update_fields=['status', 'error_message', 'updated_at'])
                        failed_count += 1
                        continue
                    
                    message_id = resp.get('id') if isinstance(resp, dict) else None
                    if message_id:
                        CampaignMessage.objects.create(
                            campaign=campaign,
                            recipient=r,
                            provider='resend',
                            message_id=message_id,
                            status='sent'
                        )
                        r.status = 'sent'
                        r.email_sent_at = timezone.now()
                        r.error_message = ''
                        r.save(update_fields=['status', 'email_sent_at', 'error_message', 'updated_at'])
                        sent_count += 1
                    else:
                        r.status = 'failed'
                        error_msg = resp.get('message', resp.get('error', 'No message id returned')) if isinstance(resp, dict) else 'No message id returned'
                        r.error_message = str(error_msg)
                        r.save(update_fields=['status', 'error_message', 'updated_at'])
                        failed_count += 1
                except Exception as e:
                    r.status = 'failed'
                    r.error_message = str(e)
                    r.save(update_fields=['status', 'error_message', 'updated_at'])
                    failed_count += 1

        except Exception as e:
            # Mark this batch recipients failed
            for r in batch:
                try:
                    r.status = 'failed'
                    r.error_message = f"Batch error: {str(e)}"
                    r.save(update_fields=['status', 'error_message', 'updated_at'])
                    failed_count += 1
                except Exception:
                    pass
            # Don't re-raise - continue with other batches and mark campaign as failed at end
            # This prevents campaigns from getting stuck in retry loops
        finally:
            campaign.emails_sent = sent_count
            campaign.emails_failed = failed_count
            campaign.total_recipients = campaign.recipients.count()
            campaign.save(update_fields=['emails_sent', 'emails_failed', 'total_recipients', 'updated_at'])

    # Finalize status
    if failed_count == 0 and sent_count > 0:
        campaign.status = 'sent'
    elif sent_count > 0:
        campaign.status = 'completed_with_errors'
    else:
        # If no emails were sent at all, mark as failed
        campaign.status = 'failed'
    campaign.completed_at = timezone.now()
    campaign.save(update_fields=['status', 'completed_at', 'updated_at'])


