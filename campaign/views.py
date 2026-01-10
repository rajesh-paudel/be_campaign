from rest_framework import generics, status, permissions, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.views import APIView
import hmac
import hashlib
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
    format_sender_identity, replace_body_placeholders, generate_footer_html,fetchPeopleByTags,
)

from .html_generator import generate_full_email_html
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.db import transaction
from .models import CampaignMessage
from .tasks import send_campaign_task
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
    

    # for validating user by taking token passing to crm and getting user info back
    def initial(self, request, *args, **kwargs):
        """
        Called before any action.
        Validate the token using the existing utils.validate_token function.
        """
        # Skip auth for public endpoints
        if getattr(self, 'action', None) == 'analytics':
         return super().initial(request, *args, **kwargs)

    
        super().initial(request, *args, **kwargs)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise AuthenticationFailed("Authorization token required")

        token = auth_header.split("Bearer ")[1]
        user_info = validate_token(token)
      
        if not user_info:
            raise AuthenticationFailed("Invalid or expired token")
        
        request.user_id = user_info["id"]
        request.user_email = user_info.get("email")
        request.auth_token = token

    def get_object_or_404(self):
        return get_object_or_404(Campaign, pk=self.kwargs["pk"], user_email=self.request.user_email)
    
    def get_queryset(self):
        return Campaign.objects.filter(user_email=self.request.user_email)\
            .annotate(annotated_recipient_count=Count('recipients'))\
            .order_by('-created_at')
    
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
        email = request.query_params.get('email') or self.request.user_email
        signer = TimestampSigner()
        token = signer.sign(f"{campaign.user_email}:{email}")
        # Default to salesmonk.ca frontend unsubscribe page
        fe_base = getattr(settings, 'FRONTEND_BASE_URL', 'https://salesmonk.ca').rstrip('/')
        url = f"{fe_base}/unsubscribe?t={token}"
        return Response({'unsubscribe_url': url})
    
    def perform_create(self, serializer):
        serializer.save(
            user_email=self.request.user_email,
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
    
    
    @action(detail=True, methods=['post'])
    def launch(self, request, pk=None):
        """Launch a campaign immediately or schedule it"""
        campaign = self.get_object_or_404()
        
        # Validate serializer
        serializer = CampaignLaunchSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if campaign can be launched
        if not campaign.can_launch():
            return Response(
                {'error': 'Campaign cannot be launched. Check status and ensure subject is set.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate campaign has required content
        if not campaign.subject or not campaign.subject.strip():
            return Response(
                {'error': 'Campaign must have a subject line.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if campaign has content (either message HTML or components)
        has_message = campaign.message and campaign.message.strip()
        has_components = campaign.components and isinstance(campaign.components, list) and len(campaign.components) > 0
        
        if not has_message and not has_components:
            return Response(
                {'error': 'Campaign must have message content or components.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        validated_data = serializer.validated_data
        
        # Rebuild recipients from tags every launch
        # tag_ids = campaign.tags
        # if tag_ids:
            
        #     tagged_people = fetchPeopleByTags(tag_ids,request.auth_token)
        #     for person in tagged_people:
        #         # Skip if person has no email
        #         if not person.email or not person.email.strip():
        #             continue
                    
        #         if not CampaignRecipient.objects.filter(campaign=campaign, person=person).exists():
        #             CampaignRecipient.objects.create(
        #                 campaign=campaign,
        #                 person=person,
        #                 email=person.email,
        #                 first_name=person.first_name or '',
        #                 last_name=person.last_name or '',
        #                 phone=person.phone or '',
        #                 status='pending'
        #             )

        # Check if we have any recipients after rebuilding
        recipient_count = campaign.recipients.count()
        if recipient_count == 0:
            return Response(
                {'error': 'Campaign has no recipients. Please add tags or recipients before launching.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Always reset recipients to pending before sending
        campaign.recipients.all().update(status='pending', email_sent_at=None)

        # Reset campaign counters so re-send is clean
        campaign.emails_sent = 0
        campaign.emails_failed = 0
        campaign.emails_delivered = 0
        campaign.emails_opened = 0
        campaign.emails_clicked = 0
        campaign.emails_bounced = 0
        campaign.launched_at = None
        campaign.completed_at = None
        campaign.total_recipients = recipient_count
        campaign.save()

        # ===== PERFORMANCE: Generate and cache HTML once before sending =====
        try:
            logger.info(f"Campaign {campaign.id}: Generating and caching HTML before launch")
            campaign.generate_and_cache_html()
            logger.info(f"Campaign {campaign.id}: HTML cached successfully")
        except Exception as e:
            logger.error(f"Campaign {campaign.id}: Failed to cache HTML: {str(e)}")
            # Continue anyway - task will handle fallback
        # ===== END PERFORMANCE OPTIMIZATION =====

        if validated_data.get('send_immediately', True):
            # Enqueue immediate send via Celery
            campaign.status = 'sending'
            campaign.launched_at = timezone.now()
            campaign.save(update_fields=['status', 'launched_at', 'updated_at'])
            send_campaign_task.delay(str(campaign.id), request.auth_token)
            return Response({
                'campaign_id': str(campaign.id),
                'status': campaign.status,
                'message': 'Campaign enqueued for sending'
            })
        else:
            # Schedule via Celery ETA
            scheduled_at = validated_data['scheduled_at']
            campaign.scheduled_at = scheduled_at
            campaign.status = 'scheduled'
            campaign.save(update_fields=['scheduled_at', 'status', 'updated_at'])
            send_campaign_task.apply_async(args=[str(campaign.id)], eta=scheduled_at)
            return Response({
                'message': 'Campaign scheduled successfully',
                'scheduled_at': scheduled_at,
                'campaign_id': str(campaign.id)
            })
    
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
class MailgunWebhookView(APIView):
    """Webhook endpoint to receive mailgun events and update aggregate counters.

     payload structure of webhook 
    {
        "signature":
        {
            "token": "e5b4b40d54ca855d623088cced792bc0",
            "timestamp": 1646396205,
            "signature": "480fb19984da73c94dd6fc58762cb5b6f8543c1d7ce25fb2393f63e95d350b09"
        }
        “event-data”:
        {
            "event": "clicked",
            "timestamp": 1646396196,
            "id": "OTk6MTA1MDI6Y2xpY2tlZDoxNjQ2Mzk2MjA1",
            // ...
        }
    }
    """
    permission_classes = []

    def post(self, request):
        payload = request.data or {}

          #  verify the webhook is actually coming from mailgun
        # def verify_mailgun_signature(timestamp, token, signature):
        #     msg = f"{timestamp}{token}".encode()
        #     key = settings.MAILGUN_WEBHOOK_SIGNING_KEY.encode() 
        #     expected = hmac.new(key, msg, hashlib.sha256).hexdigest()
        #     return hmac.compare_digest(expected, signature)

         
        # signature = payload.get("signature", {})
        # if not verify_mailgun_signature(
        #     signature.get("timestamp", ""),
        #     signature.get("token", ""),
        #     signature.get("signature", "")
        # ):
        #     logger.warning("Invalid Mailgun webhook signature")
        #     return Response({"detail": "invalid signature"}, status=403)


        #  extract all the necessary data from payload
        event_data = payload.get("event-data", {})
        event = event_data.get("event")
        message_id = event_data.get("id")
        
        headers = event_data.get("message", {}).get("headers", {})
        recipient_id = headers.get("X-Recipient-ID")
         
        if not message_id:
            return Response({"detail": "missing message id"}, status=200)

        # Resolve message + recipient
        msg = CampaignMessage.objects.filter(message_id=message_id).select_related(
            "campaign", "recipient"
        ).first()

        recipient = msg.recipient if msg else None
        campaign = msg.campaign if msg else None

        if not recipient and recipient_id:
            recipient = CampaignRecipient.objects.filter(pk=recipient_id).first()
            campaign = recipient.campaign if recipient else None

        if not recipient:
            return Response({"detail": "recipient not found"}, status=200)

        now = timezone.now()

        with transaction.atomic():
            if event == "delivered":
                recipient.status = "delivered"
                recipient.email_delivered_at = now
                campaign.emails_delivered += 1
                msg.status = "delivered"

            elif event == "opened":
                recipient.status = "opened"
                recipient.email_opened_at = now
                campaign.emails_opened += 1
                msg.status = "opened"

            elif event == "clicked":
                recipient.status = "clicked"
                recipient.email_clicked_at = now
                campaign.emails_clicked += 1
                msg.status = "clicked"

            elif event in ["bounced", "complained", "failed"]:
                recipient.status = "bounced"
                recipient.email_bounced_at = now
                campaign.emails_bounced += 1
                msg.status = "bounced"
            else:
                pass  
            recipient.save()
            msg.save()
            campaign.save()

        return Response({"detail": "ok"}, status=200)
    

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
            user_email, email = value.split(':', 1)
            
            # Create suppression if not exists
            EmailUnsubscribe.objects.get_or_create(user_email=user_email, email=email)

            # Also update existing recipients for that email to unsubscribed
            CampaignRecipient.objects.filter( campaign__user_email=user_email,
                email=email).update(status='unsubscribed')

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
    permission_classes = []
      # for validating user by taking token passing to crm and getting user info back
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
        
        request.user_id = user_info["id"]
        request.user_email = user_info.get("email")


    def get_queryset(self):
        return EmailUnsubscribe.objects.filter(user_email=self.request.user_email).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(user_email=self.request.user_email)


class UnsubscribeDetailView(generics.DestroyAPIView):
    """Remove an email from suppression list (resubscribe)."""
    serializer_class = EmailUnsubscribeSerializer
    permission_classes = []
      # for validating user by taking token passing to crm and getting user info back
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
        
        request.user_id = user_info["id"]
        request.user_email = user_info.get("email")

    def get_queryset(self):
        return EmailUnsubscribe.objects.filter(user_email=self.request.user_email)
    
    # (analytics and unsubscribe_link actions belong to CampaignViewSet; removed here)



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
    permission_classes = []
    
      # for validating user by taking token passing to crm and getting user info back
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
        
        request.user_id = user_info["id"]
        request.user_email = user_info.get("email")



    def get_queryset(self):
        return CampaignImage.objects.filter(user_email=self.request.user_email).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(user_email=self.request.user_email)


class UserEmailFooterSettingsView(generics.RetrieveUpdateAPIView):
    """View for user email footer settings - get or update current user's footer"""
    serializer_class = UserEmailFooterSettingsSerializer
    permission_classes = []
      # for validating user by taking token passing to crm and getting user info back
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
        
        request.user_id = user_info["id"]
        request.user_email = user_info.get("email")


    def get_object(self):
        """Get or create footer settings for the current user"""
        footer_settings, created = UserEmailFooterSettings.objects.get_or_create(
            user_email=self.request.user_email
        )
        return footer_settings


# class DebugView(generics.GenericAPIView):
#     """Debug endpoint to check data"""
#     permission_classes = []  # Allow access without authentication for debugging
    
#     def get(self, request):
#         # Check Resend API configuration
#         resend_configured = bool(RESEND_API_KEY)
        
#         # Basic system info
#         debug_info = {
#             'resend_configured': resend_configured,
#             'resend_from_email': RESEND_FROM_EMAIL,
#             'django_debug': settings.DEBUG,
#             'system_status': 'OK'
#         }
        
#         # If user is authenticated, provide user-specific debug info
#         if request.user and request.user.is_authenticated:
#             user = request.user
            
#             # Count people
#             people_count = People.objects.filter(user=user).count()
#             people_with_email = People.objects.filter(user=user).exclude(
#                 email__isnull=True
#             ).exclude(email='').count()
            
#             # Count campaigns
#             campaigns_count = Campaign.objects.filter(user=user).count()
            
#             # Sample people
#             sample_people = People.objects.filter(user=user).exclude(
#                 email__isnull=True
#             ).exclude(email='')[:5].values('id', 'first_name', 'last_name', 'email')
            
#             debug_info.update({
#                 'user_id': user.id,
#                 'user_email': user.email,
#                 'counts': {
#                     'people': people_count,
#                     'people_with_email': people_with_email,
#                     'campaigns': campaigns_count,
#                 },
#                 'samples': {
#                     'people': list(sample_people),
#                 }
#             })
#         else:
#             debug_info.update({
#                 'user_status': 'Not authenticated',
#                 'note': 'Authenticate to see user-specific debug info'
#             })
        
#         return Response(debug_info)
