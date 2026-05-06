from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from core.models import AuditLog

@login_required
def audit_diff_view(request, object_id):
    """
    Shows a human-readable list of changes for a specific object (Voucher).
    """
    logs = AuditLog.objects.filter(
        model_name='voucher',
        record_id=object_id,
    ).order_by('-timestamp')
    
    diff_history = []
    
    for log in logs:
        changes = []
        before = log.old_data or {}
        after = log.new_data or {}
        
        if log.action == AuditLog.ACTION_CREATE:
            changes.append("Voucher created with these details.")
        elif log.action == AuditLog.ACTION_DELETE:
            changes.append("Voucher was deleted.")
        else:
            # UPDATE logic: compare keys
            all_keys = set(before.keys()) | set(after.keys())
            for key in all_keys:
                old_val = before.get(key)
                new_val = after.get(key)
                
                if old_val != new_val:
                    field_name = key.replace('_', ' ').title()
                    changes.append(f"Changed {field_name} from '{old_val}' to '{new_val}'")
        
        diff_history.append({
            'log': log,
            'changes': changes
        })

    return render(request, 'audit/diff.html', {
        'object_id': object_id,
        'diff_history': diff_history
    })
