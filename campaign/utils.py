"""
Utility functions for campaign subject line placeholder handling.
"""
import re
from typing import Dict, List, Optional, Tuple
from html import escape
from django.core.signing import TimestampSigner
import requests
from django.conf import settings
import logging
class SubjectLinePlaceholderHandler:
    """
    Handles parsing and replacement of placeholders in campaign subject lines.
    
    Supported placeholders:
    - {first_name} - Recipient's first name
    - {last_name} - Recipient's last name
    - {full_name} - Recipient's full name (first_name + last_name)
    - {email} - Recipient's email address
    - {company_name} - Recipient's company/business name (if available)
    """
    
    # Define available placeholders with their descriptions
    AVAILABLE_PLACEHOLDERS = {
        'first_name': 'First Name',
        'last_name': 'Last Name', 
        'full_name': 'Full Name',
        'email': 'Email Address',
        'company_name': 'Company/Business Name',
    }
    
    # Regex pattern to find all placeholders in curly braces
    PLACEHOLDER_PATTERN = re.compile(r'\{([^}]+)\}')
    
    @classmethod
    def get_available_placeholders(cls) -> Dict[str, str]:
        """
        Get list of available placeholders with descriptions.
        
        Returns:
            Dict mapping placeholder key to description
        """
        return cls.AVAILABLE_PLACEHOLDERS.copy()
    
    @classmethod
    def extract_placeholders(cls, subject_line: str) -> List[str]:
        """
        Extract all placeholders from a subject line.
        
        Args:
            subject_line: The subject line to analyze
            
        Returns:
            List of placeholder keys found in the subject line
        """
        if not subject_line:
            return []
        
        # Convert to string in case it's a SafeString
        subject_str = str(subject_line)
        
        # Find all placeholders
        matches = cls.PLACEHOLDER_PATTERN.findall(subject_str)
        return list(set(matches))  # Remove duplicates
    
    @classmethod
    def validate_placeholders(cls, subject_line: str) -> Tuple[bool, List[str]]:
        """
        Validate that all placeholders in subject line are supported.
        
        Args:
            subject_line: The subject line to validate
            
        Returns:
            Tuple of (is_valid, list_of_invalid_placeholders)
        """
        placeholders = cls.extract_placeholders(subject_line)
        invalid_placeholders = [
            placeholder for placeholder in placeholders 
            if placeholder not in cls.AVAILABLE_PLACEHOLDERS
        ]
        return len(invalid_placeholders) == 0, invalid_placeholders
    
    @classmethod
    def replace_placeholders(cls, subject_line: str, recipient_data: Dict[str, Optional[str]]) -> str:
        """
        Replace placeholders in subject line with actual values.
        
        Args:
            subject_line: The subject line with placeholders
            recipient_data: Dictionary containing recipient information
            
        Returns:
            Subject line with placeholders replaced
        """
        if not subject_line:
            return ""
        
        # Convert to string in case it's a SafeString
        subject_str = str(subject_line)
        
        # Prepare replacement data
        replacements = {
            'first_name': recipient_data.get('first_name', '') or '',
            'last_name': recipient_data.get('last_name', '') or '',
            'email': recipient_data.get('email', '') or '',
            'company_name': recipient_data.get('business_name', recipient_data.get('company_name', '')) or '',
        }
        
        # Create full_name from first_name and last_name
        first_name = replacements.get('first_name', '') or ''
        last_name = replacements.get('last_name', '') or ''
        full_name = f"{first_name} {last_name}".strip()
        replacements['full_name'] = full_name if full_name else (replacements.get('email', '') or 'Recipient')
        
        # Replace placeholders
        def replace_placeholder(match):
            placeholder = match.group(1)
            replacement = replacements.get(placeholder, f'{{{placeholder}}}')
            # Handle None values
            if replacement is None:
                replacement = f'{{{placeholder}}}'
            return str(replacement)
        
        result = cls.PLACEHOLDER_PATTERN.sub(replace_placeholder, subject_str)
        
        # Clean up any double spaces that might have been created
        result = re.sub(r'\s+', ' ', result).strip()
        
        return result
    
    @classmethod
    def create_preview(cls, subject_line: str, sample_data: Optional[Dict[str, str]] = None) -> str:
        """
        Create a preview of the subject line with sample data.
        
        Args:
            subject_line: The subject line with placeholders
            sample_data: Optional sample data to use for preview
            
        Returns:
            Preview subject line with sample data
        """
        if not sample_data:
            sample_data = {
                'first_name': 'John',
                'last_name': 'Doe',
                'email': 'john.doe@example.com',
                'business_name': 'Acme Corp',
                'company_name': 'Acme Corp',
            }
        
        return cls.replace_placeholders(subject_line, sample_data)
    
    @classmethod
    def get_placeholder_suggestions(cls, current_text: str = "") -> List[Dict[str, str]]:
        """
        Get placeholder suggestions based on current text and available placeholders.
        
        Args:
            current_text: Current subject line text to provide contextual suggestions
            
        Returns:
            List of suggestion dictionaries with 'placeholder', 'description', and 'example'
        """
        suggestions = []
        
        for placeholder, description in cls.AVAILABLE_PLACEHOLDERS.items():
            # Check if placeholder is already in use
            already_used = f'{{{placeholder}}}' in current_text
            
            # Create example based on placeholder
            examples = {
                'first_name': 'John',
                'last_name': 'Doe',
                'full_name': 'John Doe',
                'email': 'john.doe@example.com',
                'company_name': 'Acme Corp',
            }
            
            suggestions.append({
                'placeholder': f'{{{placeholder}}}',
                'description': description,
                'example': examples[placeholder],
                'already_used': already_used,
                'key': placeholder
            })
        
        return suggestions


def format_sender_identity(user) -> str:
    """
    Format sender identity as 'First Name Last Name <email>' from user object.
    Email is constructed as firstname@salesmonk.ca (in lowercase).
    
    Args:
        user: User instance (campaign.user)
        
    Returns:
        Formatted sender string like 'Vishal Dhakal <vishal@salesmonk.ca>'
    """
    # Get user's first name and last name
    first_name = getattr(user, 'first_name', '') or ''
    last_name = getattr(user, 'last_name', '') or ''
    
    # Create full name from first and last name for display
    if first_name and last_name:
        name = f"{first_name} {last_name}"
    elif first_name:
        name = first_name
    elif last_name:
        name = last_name
    elif hasattr(user, 'username'):
        name = user.username
    else:
        name = 'Sender'
    
    # Construct email as firstname@salesmonk.ca (in lowercase)
    if first_name:
        # Remove any spaces and convert to lowercase
        email_prefix = first_name.strip().replace(' ', '').lower()
    elif hasattr(user, 'username') and user.username:
        email_prefix = user.username.strip().replace(' ', '').lower()
    else:
        email_prefix = 'noreply'
    
    email = f"{email_prefix}@salesmonk.ca"
    
    # Format as 'First Name Last Name <email>'
    return f"{name} <{email}>"


def get_recipient_data_for_subject(recipient,token) -> Dict[str, Optional[str]]:
    """
    Extract recipient data for subject line replacement.
    
    Args:
        recipient: CampaignRecipient instance
        
    Returns:
        Dictionary with recipient data for placeholder replacement
    """
    # Try to get data from recipient first, then fall back to person
    first_name = recipient.first_name
    last_name = recipient.last_name
    business_name = None
    
    # If recipient data is empty, try to get from the person
    if recipient.person_id: 
        person_info = fetch_person(recipient.person_id,token)
        
    if not first_name and recipient.person_id:
        first_name = person_info.get("first_name")
    if not last_name and recipient.person_id:
        last_name = person_info.get("last_name")
    if recipient.person_id:
        business_name =  person_info.get("business_name")
    
    # Build and return the dict used for both subject and body replacements
    return {
        'first_name': first_name or '',
        'last_name': last_name or '',
        'email': person_info.get("email"),
        'business_name': business_name or '',
        'company_name': business_name or '',
    }


# --- Optimized Placeholder Replacement ---
# Pattern handles both {{placeholder}} and {placeholder} formats efficiently
# Matches: {{first_name}}, {first_name}, {{ first_name }}, { first_name }, etc.
# Supported: first_name, last_name, full_name, email, company_name, business_name, unsubscribe_url
_DOUBLE_BRACE_PATTERN = re.compile(
    r"\{\{\s*(first_name|last_name|full_name|email|company_name|business_name|unsubscribe_url)\s*\}\}",
    re.IGNORECASE
)
_SINGLE_BRACE_PATTERN = re.compile(
    r"(?<!\{)\{\s*(first_name|last_name|full_name|email|company_name|business_name|unsubscribe_url)\s*\}(?!\})",
    re.IGNORECASE
)

def replace_body_placeholders(html_content: str, recipient_data: Dict[str, Optional[str]], unsubscribe_url: Optional[str] = None) -> str:
    """
    Replace merge tags in HTML body with recipient data.
    
    Supports both {{placeholder}} and {placeholder} formats (case-insensitive).
    Supported placeholders:
    - first_name, last_name, full_name
    - email
    - company_name, business_name
    - unsubscribe_url (if provided)
    
    Args:
        html_content: HTML content with placeholders
        recipient_data: Dictionary containing recipient information
        unsubscribe_url: Optional unsubscribe URL for this recipient
        
    Returns:
        HTML content with placeholders replaced by actual values
    """
    if not html_content:
        return ""

    content = str(html_content)

    # Build replacement dict with safe fallbacks
    first_name = (recipient_data.get("first_name") or "").strip()
    last_name = (recipient_data.get("last_name") or "").strip()
    email = (recipient_data.get("email") or "").strip()
    company_name = (
        recipient_data.get("business_name")
        or recipient_data.get("company_name")
        or ""
    ).strip()

    # Build full name from first and last name
    full_name = f"{first_name} {last_name}".strip()
    if not full_name:
        full_name = email or "there"

    # Case-insensitive replacement map
    replacements: Dict[str, str] = {
        "first_name": first_name or "there",
        "last_name": last_name or "",
        "full_name": full_name,
        "email": email or "",
        "company_name": company_name or "",
        "business_name": company_name or "",
        "unsubscribe_url": unsubscribe_url or "",
    }

    def _replace(match: re.Match) -> str:
        """Replace matched placeholder with actual value"""
        key = match.group(1).lower()
        value = replacements.get(key, "")
        
        # Don't escape unsubscribe_url as it's already a valid URL
        if key == "unsubscribe_url":
            return value
        
        # Escape other values to avoid breaking HTML if values contain special chars
        return escape(value)

    # Two-pass replacement: double braces first, then single braces
    # This prevents {{placeholder}} from being partially replaced
    content = _DOUBLE_BRACE_PATTERN.sub(_replace, content)
    content = _SINGLE_BRACE_PATTERN.sub(_replace, content)

    return content


def generate_footer_html(footer_settings) -> str:
    """
    Generate HTML for email footer from footer settings.
    
    Args:
        footer_settings: UserEmailFooterSettings instance or None
        
    Returns:
        Footer HTML string, or empty string if no settings or no content
    """
    if not footer_settings or not footer_settings.has_content:
        return ''
    
    # The model only has custom_html field - use it directly
    custom_html = getattr(footer_settings, 'custom_html', '') or ''
    
    if not custom_html.strip():
        return ''
    
    # Default styling values
    text_color = '#666666'
    background_color = '#ffffff'
    font_size = '12px'
    text_align = 'center'
    unsubscribe_text = 'Unsubscribe'
    
    # Use the custom HTML provided by the user
    content = str(custom_html)
    
    # Ensure at least one unsubscribe link placeholder exists for compliance
    if '{{unsubscribe_url}}' not in content:
        # Add unsubscribe link if not already present
        content += f'<div style="margin-top: 12px; font-size: {font_size}; color: {text_color}; text-align: {text_align};"><a href="{{{{unsubscribe_url}}}}" style="color:{text_color}; text-decoration:none;">{escape(unsubscribe_text)}</a></div>'
    
    # Wrap in footer container (use Arial font family for consistency)
    footer_html = f'''<div style="margin-top: 40px; padding: 20px; background-color: {background_color}; border-top: 1px solid #e0e0e0;">
  <div style="max-width: 600px; margin: 0 auto; text-align: {text_align}; font-size: {font_size}; color: {text_color}; font-family: Arial, sans-serif;">
    {content}
  </div>
</div>'''
    
    return footer_html


def validate_token(token: str):
    """
    Call old backend to validate token.
    Returns user info dict if valid, else None.
    """
    url = f"{settings.BE_CRM_API}accounts/validate-token/"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()  # raises HTTPError for 4xx/5xx
        data = response.json()
        # optionally check required fields
        if "id" in data:
            return data
        return None
    except (requests.RequestException, ValueError) as e:
        # ValueError for invalid JSON
        print(f"Token validation failed: {e}")
        return None
    
def fetch_person(person_id,token:str):
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(f"{settings.BE_CRM_API}people/{person_id}/",headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        return {
            "first_name": data.get("first_name"),
            "last_name": data.get("last_name"),
            "business_name": data.get("business_name"),
            "email":data.get("email"),
        }
    except requests.RequestException:
        return {}    
    

def fetchPeopleByTags(tag_ids,token:str):  
    """Fetch people from activity API filtered by tag IDs."""    
    if not tag_ids:
     return []
    try:
        API_URL= f"{settings.BE_CRM_API}activity/people/"
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "tags": ",".join([str(t) for t in tag_ids]),  # filter by tags
        }

        response = requests.get(API_URL, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
       
        return []