from django.conf import settings


def sso_flags(request):
    return {"sso_enabled": settings.SSO_ENABLED}
