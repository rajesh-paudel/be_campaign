"""
Backend HTML generator for campaign components
Generates email-safe HTML from campaign components JSON
"""
from html import escape
from typing import List, Dict, Optional
from .models import UserEmailFooterSettings


def generate_html_from_components(components: List[Dict], user, footer_settings=None, campaign_subject=None) -> str:
    """
    Generate HTML from campaign components.
    Handles footer components by replacing them with footer HTML from user settings.
    
    Args:
        components: List of component dictionaries
        user: User instance to fetch footer settings
        footer_settings: Optional pre-fetched footer settings
        campaign_subject: Optional campaign subject line (used for image alt text fallback)
        
    Returns:
        HTML string ready for email
    """
    if not components:
        return ""
    
    # Ensure campaign_subject is a string or None
    if campaign_subject is not None:
        campaign_subject = str(campaign_subject).strip() or None
    
    # Get footer settings for the user if not provided
    if footer_settings is None:
        try:
            footer_settings = UserEmailFooterSettings.objects.filter(user=user).first()
        except Exception:
            footer_settings = None
    
    body_parts = []
    
    for component in components:
        component_type = component.get('type', '')
        component_data = component.get('data', {})
        
        if component_type == 'footer':
            # Prefer user's shared footer settings for consistent footer across campaigns
            if footer_settings and footer_settings.has_content:
                from .utils import generate_footer_html
                footer_html = generate_footer_html(footer_settings)
                body_parts.append(footer_html)
            else:
                # Fallback to any custom component HTML only if settings are absent
                custom = component_data.get('html') or component_data.get('content') or ''
                if isinstance(custom, str) and custom.strip():
                    body_parts.append(custom)
                else:
                    # Default minimal footer (looks more like a personal signature disclaimer)
                    body_parts.append(
                        '<div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #eeeeee; text-align: center; color: #888888; font-size: 11px; font-family: Arial, sans-serif;">'
                        'Sent by Milan | <a href="{{unsubscribe_url}}" style="color:#888888; text-decoration: underline;">Unsubscribe</a>'
                        '</div>'
                    )
        else:
            # Generate HTML for other component types
            html = _component_to_html(component_type, component_data, user, footer_settings, campaign_subject)
            if html:
                body_parts.append(html)
    
    return '\n'.join(body_parts)


def _component_to_html(component_type: str, data: Dict, user=None, footer_settings=None, campaign_subject=None) -> str:
    """
    Convert a single component to HTML.
    
    Args:
        component_type: Type of component (text-block, heading, etc.)
        data: Component data dictionary
        user: User instance (for footer settings in nested components)
        footer_settings: Optional footer settings (for footer in nested components)
        campaign_subject: Optional campaign subject line (used for image alt text fallback)
        
    Returns:
        HTML string for the component
    """
    if component_type == 'text-block':
        content = data.get('content', '')
        # Always use Arial font family (ignore JSON font preference)
        font = 'Arial, sans-serif'
        fontSize = data.get('fontSize', '16px')
        alignment = data.get('alignment', 'center')
        backgroundColor = data.get('backgroundColor', '#ffffff')
        letterSpacing = data.get('letterSpacing', 0)
        lineHeight = data.get('lineHeight') or 1.6
        
        return _wrap_with_padding(
            f'<div style="padding-bottom: 0.25rem; width: 100%; border: none; outline: none; font-size: {fontSize}; font-family: {font}; text-align: {alignment}; background-color: {backgroundColor}; letter-spacing: {letterSpacing}px; line-height: {lineHeight};">{content}</div>',
            data,
        )
    
    elif component_type == 'heading':
        content = escape(data.get('content', 'Heading'))
        level = data.get('level', 'h3')
        color = data.get('color', '#000000')
        alignment = data.get('alignment', 'center')
        # Always use Arial font family (ignore JSON font preference)
        font = 'Arial, sans-serif'
        fontSize = data.get('fontSize', '24px')
        bold = data.get('bold', False)
        italic = data.get('italic', False)
        underline = data.get('underline', False)
        backgroundColor = data.get('backgroundColor', '#ffffff')
        letterSpacing = data.get('letterSpacing', 0)
        lineHeight = data.get('lineHeight') or 1.4
        
        fontWeight = 'bold' if bold else 'normal'
        fontStyle = 'italic' if italic else 'normal'
        textDecoration = 'underline' if underline else 'none'
        
        return _wrap_with_padding(
            f'<{level} style="margin: 0; padding-left: 0.25rem; border: none; outline: none; font-family: {font}; font-weight: {fontWeight}; font-style: {fontStyle}; text-decoration: {textDecoration}; background-color: {backgroundColor}; letter-spacing: {letterSpacing}px; line-height: {lineHeight}; color: {color}; text-align: {alignment};">{content}</{level}>',
            data,
        )
    
    elif component_type == 'image':
        src = data.get('src', '')
        # Use campaign subject as alt text if alt is not provided or is generic
        # This improves accessibility and helps avoid spam filters
        alt_from_data = data.get('alt', '').strip()
        if alt_from_data and alt_from_data.lower() not in ['image', 'img', 'photo', 'picture']:
            alt = alt_from_data
        elif campaign_subject and campaign_subject.strip():
            alt = campaign_subject.strip()
        else:
            alt = 'Image'
        
        width = data.get('width', '100%')
        height = data.get('height', 'auto')
        borderRadius = data.get('borderRadius', 0)
        align = data.get('align', 'center')
        
        if not src:
            return ''
        
        # Format border-radius (handle both number and string with 'px')
        border_radius_style = f"{borderRadius}px" if borderRadius and str(borderRadius) != "0" else "0px"
        
        # Simplified centering using div instead of table (avoids promotional template fingerprint)
        return _wrap_with_padding(
            f'<div style="text-align: {align};"><img src="{escape(src)}" alt="{escape(alt)}" style="width: {width}; height: {height}; max-width: 100%; display: inline-block; border-radius: {border_radius_style};" /></div>',
            data,
        )
    
    elif component_type == 'button':
        text = escape(data.get('text', 'Click Here'))
        url = data.get('url', '#').strip() or '#'
        backgroundColor = data.get('backgroundColor', '#111111')
        color = data.get('color', '#ffffff')
        padding = data.get('padding', '12px 24px')
        borderRadius = data.get('borderRadius', '6px')
        align = data.get('align', 'center')
        
        # Handle border styling to match frontend
        borderWidth = data.get('borderWidth', '0px')
        borderStyle = data.get('borderStyle', 'solid')
        borderColor = data.get('borderColor', '#000000')
        
        border_style_string = 'none'
        if borderWidth and borderWidth not in ['0px', '0']:
            border_style_string = f'{borderWidth} {borderStyle} {borderColor}'
        
        return _wrap_with_padding(
            f'<div style="text-align: {align};"><a href="{escape(url)}" target="_blank" style="background-color: {backgroundColor}; color: {color} !important; padding: {padding}; border-radius: {borderRadius}; text-decoration: none !important; display: inline-block; font-size: 16px; font-weight: 600; border: {border_style_string}; cursor: pointer; transition: opacity 0.2s ease; font-family: Arial, sans-serif; line-height: 1.2;" onmouseover="this.style.opacity=0.9" onmouseout="this.style.opacity=1">{text}</a></div>',
            data,
        )
    
    elif component_type == 'divider':
        style = data.get('style', 'solid')
        color = data.get('color', '#e5e7eb')
        height = data.get('height', '1px')
        
        return _wrap_with_padding(
            f'<hr style="border: none; border-top: {height} {style} {color};" />',
            data,
        )
    
    elif component_type == 'spacer':
        height = data.get('height', '20px')
        return _wrap_with_padding(
            f'<div style="height: {height};"></div>',
            data,
        )
    
    elif component_type == 'link':
        text = escape(data.get('text', 'click here'))
        url = data.get('url', '#').strip() or '#'
        linkColor = data.get('color', '#007bff')
        underline = data.get('underline', True)
        alignment = data.get('alignment', 'center')
        
        textDecoration = 'underline' if underline else 'none'
        
        return _wrap_with_padding(
            f'<div style="text-align: {alignment};"><a href="{escape(url)}" target="_blank" rel="noopener noreferrer" style="text-align: {alignment}; color: {linkColor}; text-decoration: {textDecoration}; cursor: pointer; font-weight: 500; display: inline-block;">{text}</a></div>',
            data,
        )
    
    elif component_type == 'social-media':
        platforms = data.get('platforms', [])
        iconSize = data.get('iconSize', '24px')
        color = data.get('color', '#666666')
        alignment = data.get('alignment', 'center')
        
        # Simple social icons (using Unicode/emoji as fallback)
        social_icons = {
            'facebook': '📘',
            'twitter': '🐦',
            'instagram': '📷',
            'linkedin': '💼',
            'youtube': '📺',
            'tiktok': '🎵',
        }
        
        # Use text-align on container and inline-block for items (removes flexbox fingerprint)
        platform_links = []
        for platform in platforms:
            platform_name = platform.get('name', '')
            platform_url = platform.get('url', '#')
            icon = social_icons.get(platform_name, '🔗')
            platform_links.append(
                f'<a href="{escape(platform_url)}" target="_blank" rel="noopener noreferrer" '
                f'style="font-size: {iconSize}; color: {color}; text-decoration: none; margin: 0 10px; display: inline-block;">{icon}</a>'
            )
        
        return _wrap_with_padding(
            f'<div style="text-align: {alignment}; padding: 10px 0;">{"".join(platform_links)}</div>',
            data,
        )
    
    elif component_type == 'list':
        content = data.get('content', '')
        # Always use Arial font family (ignore JSON font preference)
        font = 'Arial, sans-serif'
        fontSize = data.get('fontSize', '16px')
        lineHeight = data.get('lineHeight') or 1.6

        return _wrap_with_padding(
            f'<div style="font-size: {fontSize}; font-family: {font}; line-height: {lineHeight};">{content}</div>',
            data,
        )

    elif component_type == 'container':
        backgroundColor = data.get('backgroundColor', 'transparent')
        padding = data.get('padding', '0px')
        borderRadius = data.get('borderRadius', '0px')
        inner_components = []
        for comp in data.get('components', []):
            if isinstance(comp, dict):
                comp_type = comp.get('type', '')
                comp_data = comp.get('data', {})
                comp_html = _component_to_html(comp_type, comp_data, user, footer_settings, campaign_subject)
                if comp_html:
                    inner_components.append(comp_html)

        # Use simple div structure for containers (works in modern clients, padding wrapper handles compatibility)
        return _wrap_with_padding(
            f'<div style="width:100%; max-width:100%; background-color:{backgroundColor}; border-radius:{borderRadius}; padding:{padding}; box-sizing:border-box;">{"".join(inner_components)}</div>',
            data,
        )

    elif component_type in ['two-column', 'three-column']:
        width = data.get('width', '100%')
        backgroundColor = data.get('backgroundColor', '#ffffff')
        padding = data.get('padding', '0px')
        columnsData = data.get('columnsData', [])
        columnWidths = data.get('columnWidths', [])
        total_columns = len(columnsData) if isinstance(columnsData, list) else 0
        default_width = f"{round(100 / total_columns, 2)}%" if total_columns else 'auto'

        column_html = []
        for idx, column in enumerate(columnsData if isinstance(columnsData, list) else []):
            column_components = []
            if isinstance(column, list):
                for comp in column:
                    if isinstance(comp, dict):
                        comp_type = comp.get('type', '')
                        comp_data = comp.get('data', {})
                        # Handle footer component in nested columns
                        if comp_type == 'footer':
                            if footer_settings and footer_settings.has_content:
                                from .utils import generate_footer_html
                                comp_html = generate_footer_html(footer_settings)
                            else:
                                custom_child = comp.get('data', {}).get('html') or comp.get('data', {}).get('content') or ''
                                if isinstance(custom_child, str) and custom_child.strip():
                                    comp_html = custom_child
                                else:
                                    comp_html = (
                                        '<div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #eeeeee; text-align: center; color: #888888; font-size: 11px; font-family: Arial, sans-serif;">'
                                        'Sent by Milan | <a href="{{unsubscribe_url}}" style="color:#888888; text-decoration: underline;">Unsubscribe</a>'
                                        '</div>'
                                    )
                        else:
                            comp_html = _component_to_html(comp_type, comp_data, user, footer_settings, campaign_subject)
                        if comp_html:
                            column_components.append(comp_html)

            col_width = columnWidths[idx] if idx < len(columnWidths) and columnWidths[idx] else default_width
            column_html.append(
                f'<td valign="top" style="width:{col_width}; padding:{padding}; box-sizing:border-box;">{"" .join(column_components)}</td>'
            )

        # Use standard table for multi-column layouts (needed for Outlook compatibility)
        # Removed role="presentation" to avoid promotional template fingerprint
        return _wrap_with_padding(
            f'<table cellpadding="0" cellspacing="0" border="0" style="width:{width}; max-width:100%; background-color:{backgroundColor}; border-collapse:collapse;"><tr>{"".join(column_html)}</tr></table>',
            data,
        )
    
    elif component_type == 'column':
        width = data.get('width', '100%')
        backgroundColor = data.get('backgroundColor', '#ffffff')
        padding = data.get('padding', '0px')
        gap = data.get('gap', '20px')
        columnsData = data.get('columnsData', [])
        columnWidths = data.get('columnWidths', [])
        
        column_html = []
        for idx, column in enumerate(columnsData):
            column_components = []
            if isinstance(column, list):
                for comp in column:
                    if isinstance(comp, dict):
                        comp_type = comp.get('type', '')
                        comp_data = comp.get('data', {})
                        # Handle footer component in nested columns
                        if comp_type == 'footer':
                            if footer_settings and footer_settings.has_content:
                                from .utils import generate_footer_html
                                comp_html = generate_footer_html(footer_settings)
                            else:
                                custom_child = comp.get('data', {}).get('html') or comp.get('data', {}).get('content') or ''
                                if isinstance(custom_child, str) and custom_child.strip():
                                    comp_html = custom_child
                                else:
                                    comp_html = (
                                        '<div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #eeeeee; text-align: center; color: #888888; font-size: 11px; font-family: Arial, sans-serif;">'
                                        'Sent by Milan | <a href="{{unsubscribe_url}}" style="color:#888888; text-decoration: underline;">Unsubscribe</a>'
                                        '</div>'
                                    )
                        else:
                            comp_html = _component_to_html(comp_type, comp_data, user, footer_settings, campaign_subject)
                        if comp_html:
                            column_components.append(comp_html)
            
            col_width = columnWidths[idx] if idx < len(columnWidths) else 'auto'
            column_html.append(
                f'<div style="width: {col_width};">{"".join(column_components)}</div>'
            )
        
        return _wrap_with_padding(
            f'<div style="background-color: {backgroundColor}; padding: {padding}; width: {width}; display: flex; gap: {gap};">{"".join(column_html)}</div>',
            data,
        )
    
    # Unknown component type
    return ''


def _container_padding_style(data: Dict) -> str:
    pad = (data or {}).get('containerPadding', {})
    def to_px(v, d):
        if isinstance(v, (int, float)):
            return f"{int(v)}px"
        if isinstance(v, str) and v.strip():
            return v
        return f"{d}px"
    top = to_px(pad.get('top'), 8)
    right = to_px(pad.get('right'), 12)
    bottom = to_px(pad.get('bottom'), 8)
    left = to_px(pad.get('left'), 12)
    return f"padding: {top} {right} {bottom} {left};"


def _wrap_with_padding(inner_html: str, data: Dict) -> str:
    style = _container_padding_style(data)
    return f'<div style="{style} box-sizing: border-box; max-width: 100%; width: 100%; overflow-wrap: break-word;">{inner_html}</div>'


def generate_full_email_html(components: List[Dict], user, footer_settings: Optional[UserEmailFooterSettings] = None, campaign_subject: Optional[str] = None) -> str:
    """
    Generate complete email HTML from components and footer settings.
    Matches frontend generateHtml() output exactly for consistent preview.
    
    Args:
        components: List of component dictionaries
        user: User instance
        footer_settings: Optional footer settings (if None, will fetch)
        campaign_subject: Optional campaign subject line (used for image alt text fallback)
        
    Returns:
        Complete HTML email string
    """
    # Get footer settings if not provided
    if footer_settings is None:
        try:
            footer_settings = UserEmailFooterSettings.objects.filter(user=user).first()
        except Exception:
            footer_settings = None
    
    # Generate body HTML from components.
    body_html = generate_html_from_components(components, user, footer_settings, campaign_subject)

    # If there's no explicit footer component, append user's footer by default
    try:
        has_footer = any(isinstance(c, dict) and c.get('type') == 'footer' for c in (components or []))
    except Exception:
        has_footer = False
    
    footer_html = ""
    if not has_footer and footer_settings and footer_settings.has_content:
        from .utils import generate_footer_html
        try:
            footer_html = generate_footer_html(footer_settings)
        except Exception:
            pass
    
    # Build complete HTML - Use Arial only (no external font imports to avoid promotional fingerprint)
    full_html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Email Campaign</title>
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
    /* Reset UA margins inside email */
    .email-container p, .email-container h1, .email-container h2, .email-container h3,
    .email-container h4, .email-container h5, .email-container h6,
    .email-container ul, .email-container ol {{ margin: 0; }}
  </style>
  <!--[if mso]>
  <xml>
    <o:OfficeDocumentSettings>
      <o:AllowPNG/>
      <o:PixelsPerInch>96</o:PixelsPerInch>
    </o:OfficeDocumentSettings>
  </xml>
  <![endif]-->
</head>
<body>
  <div class="email-container">
    {body_html}
    {footer_html}
  </div>
</body>
</html>'''
    
    return full_html

