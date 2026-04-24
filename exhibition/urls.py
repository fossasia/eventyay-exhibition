from django.urls import path
from eventyay.api.urls import event_router

from .api import (
    ExhibitorAuthView,
    ExhibitorInfoViewSet,
    LeadCreateView,
    LeadRetrieveView,
    LeadUpdateView,
    TagListView,
)
from .views import (
    ExhibitorCopyKeyView,
    ExhibitorCreateView,
    ExhibitorDeleteView,
    ExhibitorEditView,
    ExhibitorListView,
    PublicExhibitorDetailView,
    PublicExhibitorListView,
    SettingsView,
    SponsorGroupFrontPageToggleView,
)

urlpatterns = [
    path(
        "<str:organizer>/<str:event>/exhibition/",
        PublicExhibitorListView.as_view(),
        name="public_list",
    ),
    path(
        "<str:organizer>/<str:event>/exhibition/<int:pk>/",
        PublicExhibitorDetailView.as_view(),
        name="public_detail",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/settings/exhibitors",
        SettingsView.as_view(),
        name="settings",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/settings/exhibitors/groups/<int:pk>/toggle-front-page",
        SponsorGroupFrontPageToggleView.as_view(),
        name="toggle_front_page",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/exhibitors",
        ExhibitorListView.as_view(),
        name="info",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/exhibitors/add",
        ExhibitorCreateView.as_view(),
        name="add",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/exhibitors/edit/<int:pk>",
        ExhibitorEditView.as_view(),
        name="edit",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/exhibitors/delete/<int:pk>",
        ExhibitorDeleteView.as_view(),
        name="delete",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/exhibitors/copy_key/<int:pk>",
        ExhibitorCopyKeyView.as_view(),
        name="copy_key",
    ),
    path(
        "api/v1/event/<str:organizer>/<str:event>/exhibitors/auth",
        ExhibitorAuthView.as_view(),
        name="exhibitor-auth",
    ),
    path(
        "api/v1/event/<str:organizer>/<str:event>/exhibitors/lead/create",
        LeadCreateView.as_view(),
        name="lead-create",
    ),
    path(
        "api/v1/event/<str:organizer>/<str:event>/exhibitors/lead/retrieve",
        LeadRetrieveView.as_view(),
        name="lead-retrieve",
    ),
    path(
        "api/v1/event/<str:organizer>/<str:event>/exhibitors/tags",
        TagListView.as_view(),
        name="exhibitor-tags",
    ),
    path(
        "api/v1/event/<str:organizer>/<str:event>/exhibitors/lead/<str:lead_id>/update",
        LeadUpdateView.as_view(),
        name="lead-update",
    ),
]

event_router.register("exhibitors", ExhibitorInfoViewSet, basename="exhibitorinfo")
