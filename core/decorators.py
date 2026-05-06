"""
core/decorators.py

Role-based access control for Akshaya Vistara views.

Usage:
    from core.decorators import role_required

    @login_required
    @role_required("Admin", "Accountant")   # must come AFTER @login_required
    def my_write_view(request): ...

    @login_required
    @role_required("Admin")
    def admin_only_view(request): ...

Role hierarchy (most to least privileged):
    Admin       → full access (create, edit, delete, all settings)
    Accountant  → create and edit vouchers / ledgers; no delete
    Viewer      → read-only (list, detail, reports, exports)
"""

from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from .models import UserCompanyAccess


def role_required(*allowed_roles):
    """
    Decorator that gates a view behind one or more UserCompanyAccess roles.

    - If the user has no company selected  → redirect to select-company.
    - If the user's role is not in allowed_roles → redirect back with an
      error flash message (or to the dashboard if no Referer is available).
    - Roles are matched case-sensitively against UserCompanyAccess.ROLE_CHOICES.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            company = getattr(request, "current_company", None)
            if company is None:
                messages.error(request, "Please select a company first.")
                return redirect("core:select_company")

            try:
                access = UserCompanyAccess.objects.get(
                    user=request.user, company=company
                )
            except UserCompanyAccess.DoesNotExist:
                messages.error(request, "You do not have access to this company.")
                return redirect("core:select_company")

            if access.role not in allowed_roles:
                messages.error(
                    request,
                    f"Permission denied. "
                    f"Your role ({access.role}) cannot perform this action. "
                    f"Required: {' or '.join(allowed_roles)}."
                )
                # Go back where the user came from, or fall back to dashboard
                referer = request.META.get("HTTP_REFERER")
                return redirect(referer) if referer else redirect("core:dashboard")

            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


# Convenience aliases for the three common permission tiers
def admin_required(view_func):
    """Shortcut: only Admin role may proceed."""
    return role_required("Admin")(view_func)


def write_required(view_func):
    """Shortcut: Admin or Accountant may proceed (write operations)."""
    return role_required("Admin", "Accountant")(view_func)
