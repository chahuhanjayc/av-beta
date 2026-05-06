"""
core/utils/search_utils.py
Helpers for Universal Search (Ctrl+K) and Recent Items tracking.
"""

def add_recent_item(request, category, item_id, name, url):
    """
    Store the last 10 accessed items in the session.
    Categories: 'ledgers', 'vouchers', 'items'
    """
    recent = request.session.get('recent_search_items', {})
    
    if category not in recent:
        recent[category] = []
    
    # Remove if already exists (to move it to top)
    recent[category] = [i for i in recent[category] if i['id'] != item_id]
    
    # Add to top
    recent[category].insert(0, {
        'id': item_id,
        'name': name,
        'url': url
    })
    
    # Limit to 10
    recent[category] = recent[category][:10]
    
    request.session['recent_search_items'] = recent
    request.session.modified = True
