"""
accounts/views.py
"""

from django.contrib.auth import authenticate, login, logout
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.shortcuts import render, redirect
from django.contrib import messages
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.crypto import constant_time_compare, get_random_string
from django.views.decorators.http import require_http_methods

from .forms import LoginForm, RegisterForm

User = get_user_model()

MFA_PENDING_USER_KEY = "pending_mfa_user_id"
MFA_NEXT_KEY = "pending_mfa_next"
MFA_CACHE_TTL_SECONDS = 10 * 60


def _get_client_ip(request):
    """Helper: get client IP, respecting X-Forwarded-For if behind a proxy."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _requires_mfa(user):
    return bool(
        getattr(settings, "REQUIRE_STAFF_MFA", False)
        and (user.is_staff or user.is_superuser)
    )


def _mfa_cache_key(request, user_id):
    session_key = request.session.session_key or "no-session"
    return f"login_mfa:{session_key}:{user_id}"


def _start_mfa_challenge(request, user, next_url=None):
    if not request.session.session_key:
        request.session.save()

    code = get_random_string(6, allowed_chars="0123456789")
    cache.set(_mfa_cache_key(request, user.pk), code, timeout=MFA_CACHE_TTL_SECONDS)
    request.session[MFA_PENDING_USER_KEY] = user.pk
    request.session[MFA_NEXT_KEY] = _safe_next_url(request, next_url)

    send_mail(
        subject="Akshaya Vistara login verification code",
        message=(
            f"Your Akshaya Vistara login verification code is {code}. "
            "It expires in 10 minutes."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def _safe_next_url(request, next_url):
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return settings.LOGIN_REDIRECT_URL


def _registration_allowed(request):
    if getattr(settings, "ALLOW_PUBLIC_REGISTRATION", False):
        return True

    invite_code = getattr(settings, "REGISTRATION_INVITE_CODE", "")
    supplied_code = request.POST.get("invite_code") or request.GET.get("invite") or ""
    return bool(invite_code and supplied_code and constant_time_compare(invite_code, supplied_code))


def login_view(request):
    if request.user.is_authenticated:
        return redirect("core:select_company")

    ip = _get_client_ip(request)
    lock_key = f"login_lockout_{ip}"
    attempt_key = f"login_attempts_{ip}"

    # 1. Check if the IP is currently locked out
    if cache.get(lock_key):
        messages.error(
            request,
            "Too many failed login attempts. Please try again in 15 minutes."
        )
        return render(request, "registration/login.html", {"form": LoginForm()})

    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        user = authenticate(request, username=email, password=password)

        if user:
            # Success: reset attempts and login
            cache.delete(attempt_key)
            if _requires_mfa(user):
                try:
                    _start_mfa_challenge(request, user, request.GET.get("next"))
                except Exception:
                    messages.error(request, "Could not send verification code. Contact support.")
                    return render(request, "registration/login.html", {"form": form})
                messages.info(request, "Enter the verification code sent to your email.")
                return redirect("accounts:mfa_verify")
            login(request, user)
            return redirect("core:select_company")
        else:
            # Failure: increment attempts and lockout if needed
            attempts = cache.get(attempt_key, 0) + 1
            if attempts >= 5:
                # Set lockout for 15 minutes (900s)
                cache.set(lock_key, True, timeout=900)
                cache.delete(attempt_key)
                messages.error(
                    request,
                    "Too many failed login attempts. Please try again in 15 minutes."
                )
            else:
                cache.set(attempt_key, attempts, timeout=600)  # reset count after 10 mins
                messages.error(request, "Invalid email or password.")

    return render(request, "registration/login.html", {"form": form})


@require_http_methods(["GET", "POST"])
def mfa_verify_view(request):
    if request.user.is_authenticated:
        return redirect("core:select_company")

    user_id = request.session.get(MFA_PENDING_USER_KEY)
    if not user_id:
        messages.error(request, "Your verification session expired. Please sign in again.")
        return redirect("accounts:login")

    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        request.session.pop(MFA_PENDING_USER_KEY, None)
        request.session.pop(MFA_NEXT_KEY, None)
        messages.error(request, "Your verification session is invalid. Please sign in again.")
        return redirect("accounts:login")

    if request.method == "POST":
        supplied_code = request.POST.get("code", "").strip()
        expected_code = cache.get(_mfa_cache_key(request, user.pk))
        if expected_code and supplied_code and constant_time_compare(supplied_code, expected_code):
            cache.delete(_mfa_cache_key(request, user.pk))
            next_url = request.session.pop(MFA_NEXT_KEY, settings.LOGIN_REDIRECT_URL)
            request.session.pop(MFA_PENDING_USER_KEY, None)
            login(request, user)
            return redirect(next_url or "core:select_company")

        messages.error(request, "Invalid or expired verification code.")

    return render(request, "registration/mfa_verify.html")


def logout_view(request):
    logout(request)
    return redirect("accounts:login")


def register_view(request):
    if request.user.is_authenticated:
        return redirect("core:select_company")

    if not _registration_allowed(request):
        messages.error(request, "Self-registration is disabled. Ask an administrator to invite you.")
        return redirect("accounts:login")

    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Account created. An administrator must grant company access.")
        return redirect("core:select_company")

    return render(
        request,
        "registration/register.html",
        {"form": form, "invite_code": request.GET.get("invite", "")},
    )
