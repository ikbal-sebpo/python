"""Public URL conf for the documents app (mounted at /verify/, no auth)."""

from django.urls import path

from documents import public_views

app_name = "documents_public"

urlpatterns = [
    path("<uuid:uid>/", public_views.PublicVerifyView.as_view(), name="verify_document"),
    path("<uuid:uid>/preview/",
         public_views.PublicPreviewView.as_view(), name="public_preview"),
    path("<uuid:uid>/pdf/", public_views.public_pdf, name="public_pdf"),
]
