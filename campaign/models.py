from django.db import models
import uuid
from .storage_backends import PublicMediaStorage
import os
import mimetypes

class Campaign(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('sending', 'Sending'),
        ('sent', 'Sent'),
        ('paused', 'Paused'),
        ('cancelled', 'Cancelled'),
        ('completed_with_errors', 'Completed with Errors'),
        ('failed', 'Failed'),
    ]
    
    CAMPAIGN_TYPE_CHOICES = [
        ('email', 'Email'),
        ('sms', 'SMS'),
        ('both', 'Email & SMS'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # External references (CRM-owned)
    user =models.UUIDField(db_index=True)
    organization = models.UUIDField(null=True, blank=True, db_index=True)
    
    # Campaign Details
    name = models.CharField(max_length=255)
    subject = models.CharField(max_length=255, help_text="Email subject line")
    message = models.TextField(help_text="Campaign message content")
    campaign_type = models.CharField(max_length=10, choices=CAMPAIGN_TYPE_CHOICES, default='email')
    
    # Status and Scheduling
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='draft')
    scheduled_at = models.DateTimeField(null=True, blank=True)
    launched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    # Statistics
    total_recipients = models.PositiveIntegerField(default=0)
    emails_sent = models.PositiveIntegerField(default=0)
    emails_delivered = models.PositiveIntegerField(default=0)
    emails_opened = models.PositiveIntegerField(default=0)
    emails_clicked = models.PositiveIntegerField(default=0)
    emails_bounced = models.PositiveIntegerField(default=0)
    emails_failed = models.PositiveIntegerField(default=0)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Recipients selection
    tags = models.JSONField(blank=True, default=list)  # list of tag IDs from CRM
    attachments = models.JSONField(blank=True, null=True, help_text="List of attachments (base64 or URLs)")
    components = models.JSONField(blank=True, null=True, help_text="Email builder components JSON")
    # Design state flags
    design_started = models.BooleanField(default=False, help_text="Has the design/editor been started?")
    design_source = models.CharField(max_length=20, blank=True, default='', help_text="'scratch' or 'template'")
    
    # HTML Caching for Performance
    rendered_html = models.TextField(blank=True, default='', help_text="Cached rendered HTML (without personalization)")
    html_generated_at = models.DateTimeField(null=True, blank=True, help_text="When HTML was last generated")
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Campaign'
        verbose_name_plural = 'Campaigns'
    
    def __str__(self):
        # Avoid f-string formatting with potentially SafeString objects
        name = str(self.name)
        status = str(self.status)
        return "{} - {}".format(name, status)
    
    @property
    def recipient_count(self):
        return self.recipients.count()
    
    @property
    def open_rate(self):
        if self.emails_sent == 0:
            return 0
        return (self.emails_opened / self.emails_sent) * 100
    
    @property
    def click_rate(self):
        if self.emails_sent == 0:
            return 0
        return (self.emails_clicked / self.emails_sent) * 100
    
    @property
    def delivery_rate(self):
        if self.emails_sent == 0:
            return 0
        return (self.emails_delivered / self.emails_sent) * 100

    def can_launch(self):
        # Allow launch only once; disallow relaunch after sent/completed/failed
        if self.status in ['sent', 'completed_with_errors', 'failed']:
            return False
        return self.status in ['draft', 'paused', 'cancelled'] and bool(self.subject)
    
    def can_edit(self):
        return self.status in ['draft', 'paused', 'cancelled', 'sent', 'completed_with_errors', 'failed']
    
    def generate_and_cache_html(self):
        """
        Generate HTML from components and cache it for efficient sending.
        This should be called once when campaign is launched.
        Returns the generated HTML.
        """
        from django.utils import timezone
        from .html_generator import generate_full_email_html
        from .models import UserEmailFooterSettings
        
        # Get footer settings once
        footer_settings = UserEmailFooterSettings.objects.filter(user=self.user).first()
        
        # Generate base HTML from components
        if self.components and isinstance(self.components, list) and len(self.components) > 0:
            # Ensure subject is a string (handle None case)
            campaign_subject = str(self.subject) if self.subject else None
            html = generate_full_email_html(self.components, self.user, footer_settings, campaign_subject)
        else:
            # Fallback to message field if no components
            html = self.message or ''
        
        # Cache the generated HTML
        self.rendered_html = html
        self.html_generated_at = timezone.now()
        self.save(update_fields=['rendered_html', 'html_generated_at', 'updated_at'])
        
        return html


class CampaignRecipient(models.Model):
    """Individual recipients of campaigns"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sending', 'Sending'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('opened', 'Opened'),
        ('clicked', 'Clicked'),
        ('bounced', 'Bounced'),
        ('failed', 'Failed'),
        ('unsubscribed', 'Unsubscribed'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='recipients')

    # External references (CRM-owned)
    person_id = models.UUIDField(db_index=True)  # ID of the person in old CRM
    
    # Contact details (snapshot at time of campaign)
    email = models.EmailField()
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    
    # Campaign status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # Email tracking
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_delivered_at = models.DateTimeField(null=True, blank=True)
    email_opened_at = models.DateTimeField(null=True, blank=True)
    email_clicked_at = models.DateTimeField(null=True, blank=True)
    email_bounced_at = models.DateTimeField(null=True, blank=True)
    
    # Error tracking
    error_message = models.TextField(blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['campaign', 'person']
        ordering = ['-created_at']
        verbose_name = 'Campaign Recipient'
        verbose_name_plural = 'Campaign Recipients'
        indexes = [
            models.Index(fields=['campaign', 'status'], name='cr_campaign_status_idx'),
            models.Index(fields=['email'], name='cr_email_idx'),
            models.Index(fields=['status', 'created_at'], name='cr_status_created_idx'),
        ]
    
    def __str__(self):
        # Avoid f-string formatting with potentially SafeString objects
        email = str(self.email)
        campaign_name = str(self.campaign.name)
        status = str(self.status)
        return "{} - {} ({})".format(email, campaign_name, status)
    
    @property
    def full_name(self):
        first_name = str(self.first_name) if self.first_name else ""
        last_name = str(self.last_name) if self.last_name else ""
        full_name = "{} {}".format(first_name, last_name).strip()
        return full_name if full_name else str(self.email)


class CampaignMessage(models.Model):
    """Minimal per-send log storing provider message id for analytics mapping."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='messages')
    recipient = models.ForeignKey(CampaignRecipient, on_delete=models.CASCADE, related_name='messages')

    provider = models.CharField(max_length=50, default='resend')
    message_id = models.CharField(max_length=255, db_index=True)

    status = models.CharField(max_length=30, default='sent')  # sent, delivered, opened, clicked, bounced, failed
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['message_id']),
            models.Index(fields=['campaign', 'recipient']),
        ]
        verbose_name = 'Campaign Message'
        verbose_name_plural = 'Campaign Messages'

    def __str__(self):
        return "{}:{} -> {}".format(self.provider, self.message_id, str(self.recipient.email))


def campaign_image_upload_path(instance, filename):
    base, ext = os.path.splitext(filename or '')
    ext = ext.lstrip('.') or 'jpg'
    return "campaign_images/{}/{}.{}".format(instance.user.id, uuid.uuid4(), ext)


class CampaignImage(models.Model):
    """Reusable images for campaign email editor.
    Supports either an uploaded image (stored in Spaces/local MEDIA) or an external URL.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # User reference (UUID from CRM)
    user = models.UUIDField(db_index=True)
    organization = models.UUIDField(null=True, blank=True, db_index=True)

    name = models.CharField(max_length=255, blank=True)

    # Either store file or reference an external URL
    file = models.ImageField(upload_to=campaign_image_upload_path, storage=PublicMediaStorage(), blank=True, null=True)
    source_url = models.URLField(blank=True, default="")

    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    file_size = models.BigIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Campaign Image'
        verbose_name_plural = 'Campaign Images'

    def __str__(self):
        display = self.name or os.path.basename(self.file.name) if self.file else self.source_url
        return str(display)

    @property
    def url(self):
        # Prefer explicit source_url when present; otherwise use stored file URL
        if self.source_url:
            return self.source_url
        return self.file.url if self.file else ""

    def save(self, *args, **kwargs):
        # Populate metadata for uploaded files
        if self.file and hasattr(self.file, 'size'):
            self.file_size = self.file.size or 0
            mime, _ = mimetypes.guess_type(self.file.name)
            self.mime_type = mime or (getattr(self.file, 'content_type', '') or '')
        super().save(*args, **kwargs)


class UserEmailFooterSettings(models.Model):
    """User-specific email footer settings that apply to all campaigns"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
   
   # User reference (UUID from CRM)
    user = models.UUIDField(unique=True, db_index=True)
    
    # Footer content
    custom_html = models.TextField(blank=True, default="")
    
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'User Email Footer Settings'
        verbose_name_plural = 'User Email Footer Settings'
    
    def __str__(self):
        return "Footer settings for {}".format(str(self.user.email))
    
    @property
    def has_content(self):
        """Check if footer has any content to display"""
        return bool(
            self.custom_html
        )
    

class EmailUnsubscribe(models.Model):
    """Global email suppression per user.
    If a recipient unsubscribes, this prevents future sends from this user to that email.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
   
    # User reference from CRM
    user = models.UUIDField(db_index=True)

    email = models.EmailField(db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    source_campaign = models.ForeignKey('Campaign', null=True, blank=True, on_delete=models.SET_NULL, related_name='unsubscribes')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['user', 'email']
        indexes = [
            models.Index(fields=['user', 'email']),
        ]
        verbose_name = 'Email Unsubscribe'
        verbose_name_plural = 'Email Unsubscribes'

    def __str__(self):
        return "{} -> {}".format(str(self.user_id), str(self.email))
