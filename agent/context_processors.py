from django.conf import settings


def branding(request):
    return {"logo_file": settings.LOGO_FILE}
