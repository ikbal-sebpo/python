"""Private URL conf for the documents app (mounted at /documents/)."""

from django.urls import path

from documents import views

app_name = "documents"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("my/", views.MyDocumentsView.as_view(), name="my_documents"),

    # Request flow
    path("request/", views.RequestStartView.as_view(), name="request_start"),
    path("request/<int:doc_type_id>/",
         views.RequestFormView.as_view(), name="request_form"),
    path("requests/<uuid:request_id>/",
         views.RequestDetailView.as_view(), name="request_detail"),
    path("requests/<uuid:request_id>/cancel/",
         views.RequestCancelView.as_view(), name="request_cancel"),
    path("requests/<uuid:request_id>/download/",
         views.DownloadPdfView.as_view(), name="download_pdf"),

    # Action queues
    path("queue/", views.PendingQueueView.as_view(), name="all_pending_queue"),
    path("reviews/<int:step_id>/",
         views.ReviewActionView.as_view(), name="review_action"),
    path("approvals/<int:step_id>/",
         views.ApprovalActionView.as_view(), name="approval_action"),

    # HR
    path("all/", views.AllDocumentsView.as_view(), name="all_documents"),
]
