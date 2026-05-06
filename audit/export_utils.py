import csv
import io
import json
from .models import AuditLog, OverrideLog

def generate_audit_csv():
    """
    Generates a CSV export of all Audit and Override logs.
    Fields: user, action, timestamp, before_data, after_data
    """
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow(['Log Type', 'User', 'Action', 'Timestamp', 'Details/Before Data', 'After Data'])
    
    # 1. Audit Logs
    for log in AuditLog.objects.all().select_related('user'):
        writer.writerow([
            'Audit',
            str(log.user) if log.user else 'System',
            f"{log.action} {log.model_name}({log.object_id})",
            log.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            json.dumps(log.before_data) if log.before_data else '',
            json.dumps(log.after_data) if log.after_data else ''
        ])
        
    # 2. Override Logs
    for log in OverrideLog.objects.all().select_related('user'):
        writer.writerow([
            'Override',
            str(log.user) if log.user else 'System',
            log.action,
            log.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            log.reason,
            '' # No after_data for simple overrides
        ])
        
    return output.getvalue()
