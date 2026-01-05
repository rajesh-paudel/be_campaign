from rest_framework import serializers
from .models import (
    Campaign, CampaignRecipient, CampaignImage,
    UserEmailFooterSettings, EmailUnsubscribe,
)
from .utils import SubjectLinePlaceholderHandler
import sys


class CampaignRecipientSerializer(serializers.ModelSerializer):
    full_name = serializers.ReadOnlyField()
    
    class Meta:
        model = CampaignRecipient
        fields = [
            'id', 'person_id', 'email', 'first_name', 'last_name', 'phone',
            'status', 'full_name', 'email_sent_at', 'email_delivered_at',
            'email_opened_at', 'email_clicked_at', 'email_bounced_at',
            'error_message', 'retry_count', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'email_sent_at', 'email_delivered_at', 'email_opened_at',
            'email_clicked_at', 'email_bounced_at', 'created_at', 'updated_at'
        ]


class CampaignListSerializer(serializers.ModelSerializer):
    recipient_count = serializers.IntegerField(source='annotated_recipient_count', read_only=True)
    can_edit = serializers.ReadOnlyField()
    tag_ids = serializers.SerializerMethodField()
    design_started = serializers.BooleanField(read_only=True)
    design_source = serializers.CharField(read_only=True)
    emails_sent = serializers.IntegerField(read_only=True)
    emails_delivered = serializers.IntegerField(read_only=True)
    emails_opened = serializers.IntegerField(read_only=True)
    emails_clicked = serializers.IntegerField(read_only=True)
    emails_bounced = serializers.IntegerField(read_only=True)
    unsubscribed_count = serializers.SerializerMethodField()
    emails_failed = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = Campaign
        fields = [
            'id', 'name', 'status', 'created_at', 'updated_at','launched_at',
            'recipient_count', 'can_edit', 'tag_ids', 
            'design_started', 'design_source',
            'emails_sent', 'emails_delivered', 'emails_opened', 'emails_clicked',
            'emails_bounced', 'emails_failed', 'unsubscribed_count'
        ]
        read_only_fields = [
            'id', 'recipient_count', 'created_at', 'updated_at'
        ]
    
    def get_tag_ids(self, obj):
     return obj.tags or []

    def get_unsubscribed_count(self, obj):
        try:
            return obj.recipients.filter(status='unsubscribed').count()
        except Exception:
            return 0


class CampaignDetailSerializer(serializers.ModelSerializer):
    recipients = CampaignRecipientSerializer(many=True, read_only=True)
    recipient_count = serializers.ReadOnlyField()
    open_rate = serializers.ReadOnlyField()
    click_rate = serializers.ReadOnlyField()
    delivery_rate = serializers.ReadOnlyField()
    can_launch = serializers.ReadOnlyField()
    can_edit = serializers.ReadOnlyField()
    user_id = serializers.UUIDField(source='user', read_only=True)
    recipient_person_ids = serializers.SerializerMethodField()
    group_ids = serializers.SerializerMethodField()
    current_recipients = serializers.SerializerMethodField()
    tag_ids = serializers.SerializerMethodField()
    attachments = serializers.JSONField(required=False)
    components = serializers.JSONField(required=False)
    design_started = serializers.BooleanField(read_only=True)
    design_source = serializers.CharField(read_only=True)
    class Meta:
        model = Campaign
        fields = [
            'id', 'name', 'subject', 'message', 'components', 'campaign_type', 'status',
            'scheduled_at', 'launched_at', 'completed_at', 'total_recipients',
            'emails_sent', 'emails_delivered', 'emails_opened', 'emails_clicked',
            'emails_bounced', 'emails_failed', 'recipients', 'recipient_count',
            'open_rate', 'click_rate', 'delivery_rate', 'can_launch', 'can_edit', 'user_id',
            'recipient_person_ids', 'group_ids', 'current_recipients',
            'tag_ids', 'design_started', 'design_source',
            'attachments',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'launched_at', 'completed_at', 'emails_sent', 'emails_delivered',
            'emails_opened', 'emails_clicked', 'emails_bounced', 'emails_failed',
            'created_at', 'updated_at'
        ]
    
    def get_recipient_person_ids(self, obj):
        """Return list of person IDs that are recipients of this campaign"""
        return list(obj.recipients.values_list('person_id', flat=True))

    
    def get_group_ids(self, obj):
        """Return list of group IDs (deprecated - system uses tags for recipient selection)"""
        return []
    
    def get_current_recipients(self, obj):
        """Return simplified recipient info for display"""
        return [
            {
                'id': recipient.id,
                'email': recipient.email,
                'full_name': recipient.full_name,
                'person_id': recipient.person_id
            }
            for recipient in obj.recipients.all()
        ]
    
    def get_tag_ids(self, obj):
     return obj.tags or []


class CampaignCreateSerializer(serializers.ModelSerializer):
    recipient_person_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="List of Person IDs to add as recipients"
    )
    tag_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="List of Tag IDs to select people as recipients"
    )
    attachments = serializers.JSONField(required=False)
    components = serializers.JSONField(required=False)
    design_started = serializers.BooleanField(required=False, default=False)
    design_source = serializers.ChoiceField(choices=['', 'scratch', 'template'], required=False, default='')
    can_edit = serializers.ReadOnlyField()
    recipient_count = serializers.ReadOnlyField()
    tag_ids = serializers.SerializerMethodField()
    
    class Meta:
        model = Campaign
        fields = [
            'id', 'name', 'subject', 'message', 'components', 'campaign_type', 'status',
            'scheduled_at', 'recipient_person_ids', 'tag_ids', 'attachments',
            'design_started', 'design_source', 'can_edit', 'recipient_count'
        ]
        read_only_fields = ['id', 'can_edit', 'recipient_count']
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make only name required for creation
        if self.context.get('request') and self.context['request'].method == 'POST':
            self.fields['subject'].required = False
            self.fields['message'].required = False
    
    def validate(self, data):
        # Allow creating campaigns without recipients; recipients will be added at launch
        # Keep update behavior unchanged
        
        # Validate subject line placeholders if subject is provided
        subject = data.get('subject')
        if subject:
            is_valid, invalid_placeholders = SubjectLinePlaceholderHandler.validate_placeholders(subject)
            if not is_valid:
                raise serializers.ValidationError({
                    'subject': f'Invalid placeholders found: {", ".join(invalid_placeholders)}. Available placeholders: {", ".join(SubjectLinePlaceholderHandler.get_available_placeholders().keys())}'
                })
        
        return data
    
    def create(self, validated_data):
        recipient_person_ids = validated_data.pop('recipient_person_ids', [])
        tag_ids = validated_data.pop('tag_ids', [])
        attachments = validated_data.pop('attachments', None)
        components = validated_data.pop('components', None)
        # design flags (already validated via fields)
        design_started = validated_data.get('design_started', False)
        design_source = validated_data.get('design_source', '')
        
        # Set default values for subject and message if not provided
        # Keep subject optional; if only name is given, leave subject blank to treat as 'not designed yet'
        if not validated_data.get('subject'):
            validated_data['subject'] = ''
        # Do not auto-fill message; empty message means design not started
        if not validated_data.get('message'):
            validated_data['message'] = ''
        
        
        campaign = super().create(validated_data)
        if attachments is not None:
            campaign.attachments = attachments
            campaign.save()
            
        if components is not None:
            
            campaign.components = components
            # If components provided, mark design started if not already
            if isinstance(components, list) and len(components) > 0:
                campaign.design_started = True
                if not campaign.design_source:
                    campaign.design_source = 'scratch'
            campaign.save()
        # If explicit flags provided, persist them
        if design_started:
            campaign.design_started = True
            if design_source in ['scratch', 'template']:
                campaign.design_source = design_source
            campaign.save()
        if tag_ids:
            campaign.tags.set(tag_ids)
        self._add_recipients_from_person_ids(campaign, recipient_person_ids)
        self._add_recipients_from_tags(campaign, tag_ids)
        # Update total_recipients
        campaign.total_recipients = campaign.recipients.count()
        campaign.save()
        return campaign
    
    def _add_recipients_from_person_ids(self, campaign, person_ids):
        if not person_ids:
            return
        for pid in person_ids:
            CampaignRecipient.objects.create(
                campaign=campaign,
                person_id=pid,   
                email='',        
                first_name='',
                last_name='',
                phone='',
                status='pending'
            )
    
   
    
    def update(self, instance, validated_data):
        recipient_person_ids = validated_data.pop('recipient_person_ids', None)
        tag_ids = validated_data.pop('tag_ids', None)
        attachments = validated_data.pop('attachments', None)
        components = validated_data.pop('components', None)
        design_started = validated_data.pop('design_started', None)
        design_source = validated_data.pop('design_source', None)
        
        
        # Update basic fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        # Update attachments if provided
        if attachments is not None:
            instance.attachments = attachments
            
        
        # Update components if provided
        if components is not None:
            
            instance.components = components
            # Update flags based on components content
            if isinstance(components, list) and len(components) > 0:
                instance.design_started = True
                if not instance.design_source:
                    instance.design_source = 'scratch'
        # Update design flags if provided explicitly
        if design_started is not None:
            instance.design_started = bool(design_started)
        if design_source is not None and design_source in ['scratch', 'template', '']:
            instance.design_source = design_source
        
        # Update tags if provided
        if tag_ids is not None:
            instance.tags.set(tag_ids)
        
        # Update recipients if provided
        if recipient_person_ids is not None or tag_ids is not None:
            # Only delete existing recipients if we're updating the recipient list
            if recipient_person_ids is not None or tag_ids is not None:
                instance.recipients.all().delete()
                
                # Add recipients from person IDs
                if recipient_person_ids:
                    self._add_recipients_from_person_ids(instance, recipient_person_ids)
                
                # Add recipients from tags
                if tag_ids:
                    self._add_recipients_from_tags(instance, tag_ids)
                
                # Update total_recipients
                instance.total_recipients = instance.recipients.count()
        
        instance.save()
        return instance
    
    def get_tag_ids(self, obj):
     return obj.tags or []

class CampaignLaunchSerializer(serializers.Serializer):
    """Serializer for launching campaigns"""
    send_immediately = serializers.BooleanField(default=True)
    scheduled_at = serializers.DateTimeField(required=False)
    
    def validate(self, data):
        if not data.get('send_immediately') and not data.get('scheduled_at'):
            raise serializers.ValidationError(
                "Either send_immediately must be True or scheduled_at must be provided"
            )
        return data


class SubjectLinePlaceholderSerializer(serializers.Serializer):
    """Serializer for subject line placeholder suggestions and validation"""
    placeholder = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    example = serializers.CharField(read_only=True)
    already_used = serializers.BooleanField(read_only=True)
    key = serializers.CharField(read_only=True)


class SubjectLinePreviewSerializer(serializers.Serializer):
    """Serializer for subject line preview with sample data"""
    original_subject = serializers.CharField()
    preview_subject = serializers.CharField(read_only=True)
    placeholders_found = serializers.ListField(child=serializers.CharField(), read_only=True)
    is_valid = serializers.BooleanField(read_only=True)
    invalid_placeholders = serializers.ListField(child=serializers.CharField(), read_only=True) 


class CampaignImageSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = CampaignImage
        fields = [
            'id', 'name', 'file', 'source_url', 'url', 'width', 'height',
            'file_size', 'mime_type', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'url', 'file_size', 'mime_type', 'created_at', 'updated_at']

    def validate(self, attrs):
        file = attrs.get('file')
        source_url = attrs.get('source_url', '')
        if not file and not source_url:
            raise serializers.ValidationError('Provide either an image file or a source_url.')
        if file and source_url:
            # Allow both, but prefer file by clearing source_url
            attrs['source_url'] = ''
        return attrs

    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user.id
        user = self.context['request'].user
        if hasattr(user, 'primary_organization') and user.primary_organization:
            validated_data['organization'] = user.primary_organization
        return super().create(validated_data)

    def get_url(self, obj):
        # Prefer explicit source_url
        url = obj.source_url or (obj.file.url if obj.file else '')
        request = self.context.get('request')
        if request and url and url.startswith('/'):
            return request.build_absolute_uri(url)
        return url


class UserEmailFooterSettingsSerializer(serializers.ModelSerializer):
    """Serializer for user email footer settings"""
    has_content = serializers.ReadOnlyField()
    # minimal serializer - only custom_html is supported now
    
    class Meta:
        model = UserEmailFooterSettings
        fields = [
            'id', 'custom_html', 'has_content', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'has_content', 'created_at', 'updated_at']
    
    def create(self, validated_data):
        """Create footer settings for the user"""
        validated_data['user'] = self.context['request'].user.id
        return super().create(validated_data)


class EmailUnsubscribeSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailUnsubscribe
        fields = ['id', 'email', 'reason', 'source_campaign', 'created_at']
        read_only_fields = ['id', 'created_at']