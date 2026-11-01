from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("outbox/", views.outbox, name="outbox"),
    path("outbox/approve-all/", views.approve_all, name="approve_all"),
    path("messages/<int:pk>/", views.message_action, name="message_action"),
    path("leads/", views.leads, name="leads"),
    path("leads/add/", views.lead_add, name="lead_add"),
    path("leads/<int:pk>/", views.lead_detail, name="lead_detail"),
    path("leads/<int:pk>/action/", views.lead_action, name="lead_action"),
    path("call-list/", views.call_list, name="call_list"),
    path("markets/", views.markets, name="markets"),
    path("settings/", views.settings_view, name="settings"),
    path("activity/", views.activity, name="activity"),
]
