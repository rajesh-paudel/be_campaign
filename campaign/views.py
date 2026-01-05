from rest_framework import generics, status, permissions, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import AuthenticationFailed
from django.utils import timezone
from .utils import validate_token 
from django.db.models import Q, Count
from django.conf import settings
import requests
import time
import logging
from .models import (
    Campaign, CampaignRecipient,
    CampaignImage, UserEmailFooterSettings, EmailUnsubscribe
)
from .serializers import (
    CampaignListSerializer, CampaignDetailSerializer, CampaignCreateSerializer,
    CampaignLaunchSerializer,
    CampaignRecipientSerializer, SubjectLinePlaceholderSerializer,
    SubjectLinePreviewSerializer, CampaignImageSerializer,
    UserEmailFooterSettingsSerializer, EmailUnsubscribeSerializer
)
# from activity.models import People
from .utils import (
    SubjectLinePlaceholderHandler, get_recipient_data_for_subject,
    format_sender_identity, replace_body_placeholders, generate_footer_html
)
from .html_generator import generate_full_email_html
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.db import transaction
from .models import CampaignMessage

from django.core.signing import TimestampSigner, BadSignature, SignatureExpired

# Set up logging
logger = logging.getLogger(__name__)

# Direct Resend API configuration
RESEND_API_KEY = getattr(settings, 'RESEND_API_KEY', None)
RESEND_FROM_EMAIL = getattr(settings, 'RESEND_FROM_EMAIL', 'noreply@yourdomain.com')


class CampaignListPagination(PageNumberPagination):
    """Pagination for campaign list to improve performance"""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class CampaignViewSet(ModelViewSet):
    """ViewSet for managing campaigns"""
   
    pagination_class = CampaignListPagination
    
    def initial(self, request, *args, **kwargs):
        """
        Called before any action.
        Validate the token using the existing utils.validate_token function.
        """
        super().initial(request, *args, **kwargs)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise AuthenticationFailed("Authorization token required")

        token = auth_header.split("Bearer ")[1]
        user_info = validate_token(token)

        if not user_info:
            raise AuthenticationFailed("Invalid or expired token")

        # Fake user object so request.user works in the rest of the ViewSet
        class FakeUser:
            def __init__(self, user_id):
                self.id = user_id
                
        request.user = FakeUser(user_info["id"])

    def get_object_or_404(self):
        return get_object_or_404(Campaign, pk=self.kwargs["pk"], user=self.request.user)
    
    # def get_queryset(self):
    #     return Campaign.objects.filter(user=self.request.user).select_related('user').prefetch_related('tags').annotate(
    #         annotated_recipient_count=Count('recipients')
    #     ).order_by('-created_at')
    
    def get_serializer_class(self):
        if self.action == 'list':
            return CampaignListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return CampaignCreateSerializer
        elif self.action in ['launch', 'schedule']:
            return CampaignLaunchSerializer
        return CampaignDetailSerializer

    @action(detail=True, methods=['get'], permission_classes=[])
    def analytics(self, request, pk=None):
        """Get campaign analytics and statistics (public)."""
        try:
            campaign = Campaign.objects.get(pk=pk)
        except Campaign.DoesNotExist:
            return Response({'detail': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        analytics_data = {
            'campaign_id': str(campaign.id),
            'campaign_name': campaign.name,
            'status': campaign.status,
            'created_at': campaign.created_at,
            'launched_at': campaign.launched_at,
            'completed_at': campaign.completed_at,
            'statistics': {
                'total_recipients': campaign.total_recipients,
                'emails_sent': campaign.emails_sent,
                'emails_delivered': campaign.emails_delivered,
                'emails_opened': campaign.emails_opened,
                'emails_clicked': campaign.emails_clicked,
                'emails_bounced': campaign.emails_bounced,
                'emails_failed': campaign.emails_failed,
            },
            'rates': {
                'delivery_rate': campaign.delivery_rate,
                'open_rate': campaign.open_rate,
                'click_rate': campaign.click_rate,
            },
            'recipient_breakdown': {
                'pending': campaign.recipients.filter(status='pending').count(),
                'sent': campaign.recipients.filter(status='sent').count(),
                'delivered': campaign.recipients.filter(status='delivered').count(),
                'opened': campaign.recipients.filter(status='opened').count(),
                'clicked': campaign.recipients.filter(status='clicked').count(),
                'bounced': campaign.recipients.filter(status='bounced').count(),
                'failed': campaign.recipients.filter(status='failed').count(),
            }
        }
        return Response(analytics_data)

    @action(detail=True, methods=['get'], url_path='unsubscribe-link')
    def unsubscribe_link(self, request, pk=None):
        """Return a signed unsubscribe link for a sample email (for preview/testing)."""
        campaign = self.get_object_or_404()
        email = request.query_params.get('email') or request.user.email
        signer = TimestampSigner()
        token = signer.sign(f"{campaign.user_id}:{email}")
        # Default to salesmonk.ca frontend unsubscribe page
        fe_base = getattr(settings, 'FRONTEND_BASE_URL', 'https://salesmonk.ca').rstrip('/')
        url = f"{fe_base}/unsubscribe?t={token}"
        return Response({'unsubscribe_url': url})
    
    def perform_create(self, serializer):
        serializer.save(
            user=self.request.user,
            attachments=self.request.data.get('attachments', []),
            components=self.request.data.get('components', None),
        )
    
    def perform_update(self, serializer):
        instance = serializer.instance
        if not instance.can_edit():
            raise serializers.ValidationError(
                f"Cannot edit campaign in '{instance.status}' status. Only draft, paused, and cancelled campaigns can be edited."
            )
        serializer.save(
            attachments=self.request.data.get('attachments', []),
            components=self.request.data.get('components', None),
        )
    
    
    # @action(detail=True, methods=['post'])
    # def launch(self, request, pk=None):
    #     """Launch a campaign immediately or schedule it"""
    #     campaign = self.get_object_or_404()
        
    #     # Validate serializer
    #     serializer = CampaignLaunchSerializer(data=request.data)
    #     if not serializer.is_valid():
    #         return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
    #     # Check if campaign can be launched
    #     if not campaign.can_launch():
    #         return Response(
    #             {'error': 'Campaign cannot be launched. Check status and ensure subject is set.'},
    #             status=status.HTTP_400_BAD_REQUEST
    #         )
        
    #     # Validate campaign has required content
    #     if not campaign.subject or not campaign.subject.strip():
    #         return Response(
    #             {'error': 'Campaign must have a subject line.'},
    #             status=status.HTTP_400_BAD_REQUEST
    #         )
        
    #     # Check if campaign has content (either message HTML or components)
    #     has_message = campaign.message and campaign.message.strip()
    #     has_components = campaign.components and isinstance(campaign.components, list) and len(campaign.components) > 0
        
    #     if not has_message and not has_components:
    #         return Response(
    #             {'error': 'Campaign must have message content or components.'},
    #             status=status.HTTP_400_BAD_REQUEST
    #         )
        
    #     validated_data = serializer.validated_data
        
    #     # Rebuild recipients from tags every launch
    #     # tag_ids = list(campaign.tags.values_list('id', flat=True))
    #     # if tag_ids:
    #     #     from activity.models import People
    #     #     tagged_people = People.objects.filter(user=campaign.user, tags__id__in=tag_ids).distinct()
    #     #     for person in tagged_people:
    #     #         # Skip if person has no email
    #     #         if not person.email or not person.email.strip():
    #     #             continue
                    
    #     #         if not CampaignRecipient.objects.filter(campaign=campaign, person=person).exists():
    #     #             CampaignRecipient.objects.create(
    #     #                 campaign=campaign,
    #     #                 person=person,
    #     #                 email=person.email,
    #     #                 first_name=person.first_name or '',
    #     #                 last_name=person.last_name or '',
    #     #                 phone=person.phone or '',
    #     #                 status='pending'
    #     #             )

    #     # Check if we have any recipients after rebuilding
    #     recipient_count = campaign.recipients.count()
    #     if recipient_count == 0:
    #         return Response(
    #             {'error': 'Campaign has no recipients. Please add tags or recipients before launching.'},
    #             status=status.HTTP_400_BAD_REQUEST
    #         )

    #     # Always reset recipients to pending before sending
    #     campaign.recipients.all().update(status='pending', email_sent_at=None)

    #     # Reset campaign counters so re-send is clean
    #     campaign.emails_sent = 0
    #     campaign.emails_failed = 0
    #     campaign.emails_delivered = 0
    #     campaign.emails_opened = 0
    #     campaign.emails_clicked = 0
    #     campaign.emails_bounced = 0
    #     campaign.launched_at = None
    #     campaign.completed_at = None
    #     campaign.total_recipients = recipient_count
    #     campaign.save()

    #     # ===== PERFORMANCE: Generate and cache HTML once before sending =====
    #     try:
    #         logger.info(f"Campaign {campaign.id}: Generating and caching HTML before launch")
    #         campaign.generate_and_cache_html()
    #         logger.info(f"Campaign {campaign.id}: HTML cached successfully")
    #     except Exception as e:
    #         logger.error(f"Campaign {campaign.id}: Failed to cache HTML: {str(e)}")
    #         # Continue anyway - task will handle fallback
    #     # ===== END PERFORMANCE OPTIMIZATION =====

    #     if validated_data.get('send_immediately', True):
    #         # Enqueue immediate send via Celery
    #         campaign.status = 'sending'
    #         campaign.launched_at = timezone.now()
    #         campaign.save(update_fields=['status', 'launched_at', 'updated_at'])
    #         send_campaign_task.delay(str(campaign.id))
    #         return Response({
    #             'campaign_id': str(campaign.id),
    #             'status': campaign.status,
    #             'message': 'Campaign enqueued for sending'
    #         })
    #     else:
    #         # Schedule via Celery ETA
    #         scheduled_at = validated_data['scheduled_at']
    #         campaign.scheduled_at = scheduled_at
    #         campaign.status = 'scheduled'
    #         campaign.save(update_fields=['scheduled_at', 'status', 'updated_at'])
    #         send_campaign_task.apply_async(args=[str(campaign.id)], eta=scheduled_at)
    #         return Response({
    #             'message': 'Campaign scheduled successfully',
    #             'scheduled_at': scheduled_at,
    #             'campaign_id': str(campaign.id)
    #         })
    
    @action(detail=False, methods=['get'])
    def subject_placeholders(self, request):
        """Get available subject line placeholders with suggestions"""
        current_text = request.query_params.get('current_text', '')
        suggestions = SubjectLinePlaceholderHandler.get_placeholder_suggestions(current_text)
        
        serializer = SubjectLinePlaceholderSerializer(suggestions, many=True)
        return Response({
            'placeholders': serializer.data,
            'available_placeholders': SubjectLinePlaceholderHandler.get_available_placeholders()
        })
    
    @action(detail=False, methods=['post'])
    def subject_preview(self, request):
        """Preview subject line with sample data"""
        serializer = SubjectLinePreviewSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        subject_line = serializer.validated_data['original_subject']
        
        # Validate placeholders
        is_valid, invalid_placeholders = SubjectLinePlaceholderHandler.validate_placeholders(subject_line)
        
        # Extract placeholders found
        placeholders_found = SubjectLinePlaceholderHandler.extract_placeholders(subject_line)
        
        # Create preview
        preview_subject = SubjectLinePlaceholderHandler.create_preview(subject_line)
        
        return Response({
            'original_subject': subject_line,
            'preview_subject': preview_subject,
            'placeholders_found': placeholders_found,
            'is_valid': is_valid,
            'invalid_placeholders': invalid_placeholders
        })
    

# ===== WEBHOOK AND UNSUBSCRIBE VIEWS =====

@method_decorator(csrf_exempt, name='dispatch')
class ResendWebhookView(generics.GenericAPIView):
    """Webhook endpoint to receive Resend events and update aggregate counters.

    Expected JSON: {"type": "delivered|opened|clicked|bounced|complained", "created_at": ts, "data": {"email": {...}, "delivered": {...}, "clicked": {...}}, "id": message_id}
    We only rely on message_id and type.
    """
    permission_classes = []

    def post(self, request):
        # Accept Resend style payloads, e.g. type: "email.delivered", data.email_id, headers[]
        payload = request.data if isinstance(request.data, dict) else {}
        event_type = str(payload.get('type') or '').lower()
        data = payload.get('data') or {}

        # message id from Resend
        message_id = data.get('email_id') or data.get('id')

        # Normalize headers array to dict if present
        headers_list = data.get('headers') or []
        headers_dict = {}
        try:
            if isinstance(headers_list, list):
                headers_dict = {h.get('name'): h.get('value') for h in headers_list if isinstance(h, dict)}
        except Exception:
            headers_dict = {}

        if not event_type:
            return Response({'detail': 'Invalid webhook payload'}, status=status.HTTP_400_BAD_REQUEST)

        # Check if this is a daily reminder email (not a campaign email)
        email_type = headers_dict.get('X-Email-Type', '').lower()
        is_daily_reminder = email_type == 'daily-reminder'
        user_id_from_header = headers_dict.get('X-User-ID')
        
        # Resolve CampaignMessage primarily by message_id; fallback to X-Recipient-ID
        msg = None
        recipient = None
        campaign = None
        if message_id:
            msg = CampaignMessage.objects.select_related('campaign', 'recipient').filter(message_id=message_id).first()
        if not msg and headers_dict.get('X-Recipient-ID'):
            recipient_id = headers_dict.get('X-Recipient-ID')
            try:
                recipient = CampaignRecipient.objects.select_related('campaign').get(pk=recipient_id)
                campaign = recipient.campaign
            except CampaignRecipient.DoesNotExist:
                pass
        if msg:
            campaign = msg.campaign
            recipient = msg.recipient
        
        # Handle daily reminder emails (if not a campaign email)
        if is_daily_reminder and not (msg or recipient):
            # This is a daily reminder email bounce/failure
            # Extract recipient email from webhook data
            # Resend webhook structure: data.email or data.to or payload.to
            recipient_email = ''
            if data.get('email'):
                email_obj = data.get('email')
                if isinstance(email_obj, dict):
                    recipient_email = email_obj.get('to') or email_obj.get('email') or ''
                elif isinstance(email_obj, str):
                    recipient_email = email_obj
            elif data.get('to'):
                recipient_email = data.get('to')
            elif payload.get('to'):
                recipient_email = payload.get('to')
            
            if isinstance(recipient_email, list):
                recipient_email = recipient_email[0] if recipient_email else ''
            recipient_email = str(recipient_email).strip()
            
            # Handle bounce events for daily reminders
            if event_type in ['email.complained', 'complained', 'email.bounced', 'bounced', 'email.failed', 'failed']:
                if not recipient_email:
                    logger.warning(f"Daily reminder bounce event but no recipient email found. Message ID: {message_id}")
                    return Response({'detail': 'ok'}, status=status.HTTP_200_OK)
                
                from notification.models import BouncedEmail
                from django.contrib.auth import get_user_model
                User = get_user_model()
                
                try:
                    user = None
                    if user_id_from_header:
                        try:
                            user = User.objects.get(pk=user_id_from_header)
                        except User.DoesNotExist:
                            pass
                    
                    if not user and recipient_email:
                        # Try to find user by email
                        user = User.objects.filter(email=recipient_email).first()
                    
                    # Create or update bounced email record
                    bounce_type = 'bounced' if 'bounced' in event_type or 'complained' in event_type else 'failed'
                    bounce_reason = data.get('bounce_reason') or data.get('error') or data.get('message') or ''
                    
                    BouncedEmail.objects.update_or_create(
                        email=recipient_email,
                        source='daily-reminder',
                        defaults={
                            'user': user,
                            'bounce_type': bounce_type,
                            'bounce_reason': str(bounce_reason)[:500] if bounce_reason else '',
                            'message_id': message_id or '',
                        }
                    )
                    
                    logger.info(
                        f"Daily reminder email bounced: {recipient_email} "
                        f"(User: {user.id if user else 'unknown'}, Type: {bounce_type}, Message ID: {message_id})"
                    )
                    
                except Exception as e:
                    logger.error(f"Error recording daily reminder bounce: {str(e)}", exc_info=True)
            
            return Response({'detail': 'ok'}, status=status.HTTP_200_OK)
        
        # If not a daily reminder and no campaign message found, return
        if not (msg or recipient):
            return Response({'detail': 'message not found'}, status=status.HTTP_200_OK)

        # Map Resend event types to our categories
        # email.delivered -> delivered
        # email.opened -> opened
        # email.clicked -> clicked (if provided)
        # email.complained -> bounced
        # email.failed -> failed
        # email.sent / email.scheduled / email.received / email.delivery_delayed -> ignore

        # Map events to updates
        now = timezone.now()
        with transaction.atomic():
            if event_type in ['email.delivered', 'delivered']:
                if recipient.status in ['sent', 'sending', 'pending', 'failed']:
                    recipient.status = 'delivered'
                    recipient.email_delivered_at = recipient.email_delivered_at or now
                    recipient.save(update_fields=['status', 'email_delivered_at', 'updated_at'])
                # idempotent increments: only increment if this is the first time we mark delivered
                if msg and msg.status != 'delivered':
                    campaign.emails_delivered = (campaign.emails_delivered or 0) + 1
                if msg:
                    msg.status = 'delivered'
                campaign.save(update_fields=['emails_delivered', 'updated_at'])
                if msg:
                    msg.save(update_fields=['status', 'updated_at'])
            elif event_type in ['email.opened', 'opened']:
                if recipient.status in ['delivered', 'sent', 'opened', 'clicked']:
                    if recipient.status != 'opened' and recipient.status not in ['clicked']:
                        recipient.status = 'opened'
                    recipient.email_opened_at = recipient.email_opened_at or now
                    recipient.save(update_fields=['status', 'email_opened_at', 'updated_at'])
                if (not msg) or (msg.status not in ['opened', 'clicked']):
                    campaign.emails_opened = (campaign.emails_opened or 0) + 1
                if msg:
                    msg.status = 'opened' if msg.status != 'clicked' else msg.status
                campaign.save(update_fields=['emails_opened', 'updated_at'])
                if msg:
                    msg.save(update_fields=['status', 'updated_at'])
            elif event_type in ['email.clicked', 'clicked']:
                if recipient.status in ['delivered', 'sent', 'opened', 'clicked']:
                    recipient.status = 'clicked'
                    recipient.email_clicked_at = recipient.email_clicked_at or now
                    recipient.save(update_fields=['status', 'email_clicked_at', 'updated_at'])
                if (not msg) or (msg.status != 'clicked'):
                    campaign.emails_clicked = (campaign.emails_clicked or 0) + 1
                if msg:
                    msg.status = 'clicked'
                campaign.save(update_fields=['emails_clicked', 'updated_at'])
                if msg:
                    msg.save(update_fields=['status', 'updated_at'])
            elif event_type in ['email.complained', 'complained', 'email.bounced', 'bounced']:
                recipient.status = 'bounced'
                recipient.email_bounced_at = recipient.email_bounced_at or now
                recipient.save(update_fields=['status', 'email_bounced_at', 'updated_at'])
                if (not msg) or (msg.status != 'bounced'):
                    campaign.emails_bounced = (campaign.emails_bounced or 0) + 1
                if msg:
                    msg.status = 'bounced'
                campaign.save(update_fields=['emails_bounced', 'updated_at'])
                if msg:
                    msg.save(update_fields=['status', 'updated_at'])
            elif event_type in ['email.failed', 'failed']:
                # mark failure, increment emails_failed
                if recipient.status not in ['delivered', 'opened', 'clicked']:
                    recipient.status = 'failed'
                    recipient.save(update_fields=['status', 'updated_at'])
                campaign.emails_failed = (campaign.emails_failed or 0) + 1
                if msg:
                    msg.status = 'failed'
                campaign.save(update_fields=['emails_failed', 'updated_at'])
                if msg:
                    msg.save(update_fields=['status', 'updated_at'])
            else:
                # ignore others
                pass

        return Response({'detail': 'ok'})
    

class UnsubscribeView(generics.GenericAPIView):
    """Public endpoint to handle unsubscribe token links.
    Accepts GET with ?t=token and records global suppression for user:email.
    Returns a minimal JSON or HTML confirmation.
    """
    permission_classes = []

    def get(self, request):
        token = request.query_params.get('t', '')
        if not token:
            return Response({'detail': 'missing token'}, status=status.HTTP_400_BAD_REQUEST)
        signer = TimestampSigner()
        try:
            value = signer.unsign(token, max_age=60 * 60 * 24 * 60)  # 60 days
            user_id_str, email = value.split(':', 1)
            from django.contrib.auth import get_user_model
            User = get_user_model()
            try:
                user = User.objects.get(pk=user_id_str)
            except User.DoesNotExist:
                return Response({'detail': 'invalid token user'}, status=status.HTTP_400_BAD_REQUEST)

            # Create suppression if not exists
            EmailUnsubscribe.objects.get_or_create(user=user, email=email)

            # Also update existing recipients for that email to unsubscribed
            CampaignRecipient.objects.filter(campaign__user=user, email=email).update(status='unsubscribed')

            # Plain confirmation
            return Response({'detail': 'You have been unsubscribed', 'email': email})
        except SignatureExpired:
            return Response({'detail': 'token expired'}, status=status.HTTP_400_BAD_REQUEST)
        except BadSignature:
            return Response({'detail': 'invalid token'}, status=status.HTTP_400_BAD_REQUEST)


class UnsubscribeListView(generics.ListCreateAPIView):
    """Manage unsubscribes for the authenticated user.
    GET: list unsubscribed emails. POST: add an email to suppression.
    """
    serializer_class = EmailUnsubscribeSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return EmailUnsubscribe.objects.filter(user=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class UnsubscribeDetailView(generics.DestroyAPIView):
    """Remove an email from suppression list (resubscribe)."""
    serializer_class = EmailUnsubscribeSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return EmailUnsubscribe.objects.filter(user=self.request.user)
    
    # (analytics and unsubscribe_link actions belong to CampaignViewSet; removed here)


# class PeopleListView(generics.ListAPIView):
#     """List people for campaign recipient selection"""
#     permission_classes = [permissions.IsAuthenticated]
    
#     def get_queryset(self):
#         return People.objects.filter(user=self.request.user).exclude(
#             email__isnull=True
#         ).exclude(email='')
    
#     def get_serializer_class(self):
#         # Simple serializer for person selection
#         from rest_framework import serializers
        
#         class PersonSelectionSerializer(serializers.ModelSerializer):
#             full_name = serializers.SerializerMethodField()
            
#             class Meta:
#                 model = People
#                 fields = ['id', 'first_name', 'last_name', 'email', 'phone', 'full_name']
            
#             def get_full_name(self, obj):
#                 first_name = str(obj.first_name) if obj.first_name else ""
#                 last_name = str(obj.last_name) if obj.last_name else ""
#                 full_name = "{} {}".format(first_name, last_name).strip()
#                 return full_name if full_name else str(obj.email)
        
#         return PersonSelectionSerializer
    
#     def list(self, request, *args, **kwargs):
#         """List people with search functionality"""
#         queryset = self.get_queryset()
        
#         # Apply search filter
#         search = request.query_params.get('search', '')
#         if search:
#             queryset = queryset.filter(
#                 Q(first_name__icontains=search) |
#                 Q(last_name__icontains=search) |
#                 Q(email__icontains=search) |
#                 Q(phone__icontains=search)
#             )
        
#         # Paginate
#         page = self.paginate_queryset(queryset)
#         if page is not None:
#             serializer = self.get_serializer(page, many=True)
#             return self.get_paginated_response(serializer.data)
        
#         serializer = self.get_serializer(queryset, many=True)
#         return Response(serializer.data)


class CampaignRecipientViewSet(ModelViewSet):
    """ViewSet for managing campaign recipients"""
    serializer_class = CampaignRecipientSerializer
    # permission_classes = [permissions.IsAuthenticated]

    def get_object_or_404(self):
        return get_object_or_404(CampaignRecipient, pk=self.kwargs["pk"], campaign__user=self.request.user)
        
    def get_queryset(self):
        # Only allow access to recipients of campaigns owned by the user
        return CampaignRecipient.objects.filter(campaign__user=self.request.user)
    
    def list(self, request, *args, **kwargs):
        # Override list to return empty - we don't want to list all recipients
        return Response({'detail': 'Listing all recipients is not allowed'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    def create(self, request, *args, **kwargs):
        # Override create to return error - recipients are created when campaigns are created
        return Response({'detail': 'Creating recipients directly is not allowed'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    def destroy(self, request, *args, **kwargs):
        # Override destroy to return error - recipients should not be deleted directly
        return Response({'detail': 'Deleting recipients directly is not allowed'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class CampaignImageViewSet(ModelViewSet):
    """Upload or list reusable campaign images. Accepts file or source_url."""
    serializer_class = CampaignImageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return CampaignImage.objects.filter(user=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class UserEmailFooterSettingsView(generics.RetrieveUpdateAPIView):
    """View for user email footer settings - get or update current user's footer"""
    serializer_class = UserEmailFooterSettingsSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_object(self):
        """Get or create footer settings for the current user"""
        footer_settings, created = UserEmailFooterSettings.objects.get_or_create(
            user=self.request.user
        )
        return footer_settings


class DebugView(generics.GenericAPIView):
    """Debug endpoint to check data"""
    permission_classes = []  # Allow access without authentication for debugging
    
    def get(self, request):
        # Check Resend API configuration
        resend_configured = bool(RESEND_API_KEY)
        
        # Basic system info
        debug_info = {
            'resend_configured': resend_configured,
            'resend_from_email': RESEND_FROM_EMAIL,
            'django_debug': settings.DEBUG,
            'system_status': 'OK'
        }
        
        # If user is authenticated, provide user-specific debug info
        if request.user and request.user.is_authenticated:
            user = request.user
            
            # Count people
            people_count = People.objects.filter(user=user).count()
            people_with_email = People.objects.filter(user=user).exclude(
                email__isnull=True
            ).exclude(email='').count()
            
            # Count campaigns
            campaigns_count = Campaign.objects.filter(user=user).count()
            
            # Sample people
            sample_people = People.objects.filter(user=user).exclude(
                email__isnull=True
            ).exclude(email='')[:5].values('id', 'first_name', 'last_name', 'email')
            
            debug_info.update({
                'user_id': user.id,
                'user_email': user.email,
                'counts': {
                    'people': people_count,
                    'people_with_email': people_with_email,
                    'campaigns': campaigns_count,
                },
                'samples': {
                    'people': list(sample_people),
                }
            })
        else:
            debug_info.update({
                'user_status': 'Not authenticated',
                'note': 'Authenticate to see user-specific debug info'
            })
        
        return Response(debug_info)
