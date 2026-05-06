"""
core/middleware.py

CurrentCompanyMiddleware:
- Reads 'current_company_id' from the session.
- Attaches the Company object to request.current_company.
- If no company is selected, redirects unauthenticated pages gracefully.
- Protected views (non-exempt) redirect to /core/select-company/ if needed.
"""

from django.shortcuts import redirect
from django.urls import reverse
from .models import Company, UserCompanyAccess
from .utils.audit import set_current_user, set_current_company

# URL paths that are always accessible without a selected company
EXEMPT_PATHS = [
    "/accounts/login/",
    "/accounts/logout/",
    "/accounts/register/",
    "/core/ca-command-center/",
    "/core/partner-review/",
    "/core/client-360/",
    "/core/client-engagements/",
    "/core/client-profitability/",
    "/core/gst-workbench/",
    "/core/tasks/",
    "/core/filings/",
    "/core/notices/",
    "/core/select-company/",
    "/core/switch-company/",
    "/core/demo-workspace/",
    "/core/healthz/",
    "/admin/",
    "/personal-finance/",
]


class CurrentCompanyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Set thread locals for audit logging
        set_current_user(request.user)
        
        # Attach current_company to the request object
        company_id = request.session.get("current_company_id")
        request.current_company = None

        if request.user.is_authenticated:
            if company_id:
                try:
                    access = UserCompanyAccess.objects.select_related("company").get(
                        user=request.user, company_id=company_id
                    )
                    request.current_company = access.company
                    request.current_company_role = access.role
                except UserCompanyAccess.DoesNotExist:
                    request.session.pop("current_company_id", None)
            
            # Auto-select if user has only one company and none is selected
            if not request.current_company:
                access_list = UserCompanyAccess.objects.filter(user=request.user).select_related("company")
                if access_list.count() == 1:
                    access = access_list.first()
                    request.session["current_company_id"] = access.company.id
                    request.current_company = access.company
                    request.current_company_role = access.role

        # Global context for audit utility
        set_current_company(request.current_company)

        # Gate: authenticated users without a selected company → redirect to selection
        if (
            request.user.is_authenticated
            and request.current_company is None
            and not self._is_exempt(request.path)
        ):
            return redirect(reverse("core:select_company"))

        try:
            return self.get_response(request)
        finally:
            set_current_user(None)
            set_current_company(None)

    def _is_exempt(self, path):
        for exempt in EXEMPT_PATHS:
            if path.startswith(exempt):
                return True
        return False
