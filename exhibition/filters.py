from django import forms
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.utils.translation import gettext_lazy as _
from eventyay.control.forms.filter import FilterForm

from .models import ExhibitionRequestState, ExhibitorGroup, OrganizationVoucherState, SponsorGroup

FLAG_CHOICES = (
    ("", _("Any")),
    ("1", _("Enabled")),
    ("0", _("Disabled")),
)

ORGANIZATION_TYPE_CHOICES = (
    ("", _("All types")),
    ("exhibitor", _("Exhibitor only")),
    ("sponsor", _("Sponsor only")),
    ("both", _("Exhibitor and sponsor")),
)


def sponsor_group_queryset(event):
    return SponsorGroup.objects.filter(event=event)


def apply_organization_type(queryset, organization_type):
    if organization_type == "exhibitor":
        return queryset.filter(is_exhibitor=True, is_sponsor=False)
    if organization_type == "sponsor":
        return queryset.filter(is_sponsor=True, is_exhibitor=False)
    if organization_type == "both":
        return queryset.filter(is_exhibitor=True, is_sponsor=True)
    return queryset


def apply_flag(queryset, field, value):
    if value == "1":
        return queryset.filter(**{field: True})
    if value == "0":
        return queryset.filter(**{field: False})
    return queryset


def search_widget(placeholder, aria_label):
    return forms.TextInput(
        attrs={
            "class": "form-control",
            "placeholder": placeholder,
            "aria-label": aria_label,
        }
    )


class ExhibitionFilterForm(FilterForm):
    """Adds deterministic ordering so pagination stays stable across pages."""

    @property
    def advanced_fields(self):
        return [self[name] for name in self.fields if name not in ("query", "ordering")]

    @property
    def current_ordering(self):
        return self["ordering"].value() or ""

    def apply_ordering(self, queryset):
        if not self.cleaned_data.get("ordering"):
            return queryset
        return queryset.order_by(self.get_order_by(), "pk")


class RequestFilterForm(ExhibitionFilterForm):
    orders = {
        "name": "name",
        "submitter": "user__fullname",
        "state": "state",
        "updated": "updated",
    }

    query = forms.CharField(
        label=_("Search requests…"),
        widget=search_widget(_("Search requests…"), _("Search requests")),
        required=False,
    )
    state = forms.ChoiceField(
        label=_("State"),
        choices=(("", _("All states")),) + tuple(ExhibitionRequestState.choices),
        required=False,
    )
    organization_type = forms.ChoiceField(
        label=_("Type"),
        choices=ORGANIZATION_TYPE_CHOICES,
        required=False,
    )

    def __init__(self, *args, hide_emails=False, **kwargs):
        self.hide_emails = hide_emails
        super().__init__(*args, **kwargs)
        if hide_emails:
            self.fields["query"].label = _("Search by name…")
            self.fields["query"].widget.attrs.update(
                {
                    "placeholder": _("Search by name…"),
                    "aria-label": _("Search requests by name"),
                }
            )

    def filter_qs(self, queryset):
        fdata = self.cleaned_data

        query = (fdata.get("query") or "").strip()
        if query:
            lookup = Q(name__icontains=query) | Q(user__fullname__icontains=query)
            if not self.hide_emails:
                lookup |= Q(user__email__icontains=query)
            queryset = queryset.filter(lookup)

        if fdata.get("state"):
            queryset = queryset.filter(state=fdata["state"])

        queryset = apply_organization_type(queryset, fdata.get("organization_type"))
        return self.apply_ordering(queryset)


class ExhibitorFilterForm(ExhibitionFilterForm):
    orders = {
        "name": "name",
        "booth": "booth_id",
        "active": "active",
        "published": "published",
        "group": "group_rank",
        "vouchers": "voucher_rank",
    }

    query = forms.CharField(
        label=_("Search organizations…"),
        widget=search_widget(_("Search organizations…"), _("Search organizations")),
        required=False,
    )
    published = forms.ChoiceField(
        label=_("Publication"),
        choices=(
            ("", _("Any")),
            ("1", _("Published")),
            ("0", _("Not published")),
        ),
        required=False,
    )
    active = forms.ChoiceField(
        label=_("Visibility"),
        choices=(
            ("", _("All organizations")),
            ("1", _("Active")),
            ("0", _("Inactive")),
        ),
        required=False,
    )
    sponsor_group = forms.ModelChoiceField(
        label=_("Sponsor group"),
        queryset=SponsorGroup.objects.none(),
        required=False,
        empty_label=_("All groups"),
    )
    group = forms.ChoiceField(
        label=_("Group"),
        required=False,
    )
    lead_scanning = forms.ChoiceField(
        label=_("Lead scanning"),
        choices=FLAG_CHOICES,
        required=False,
    )
    lead_access = forms.ChoiceField(
        label=_("Lead access"),
        choices=FLAG_CHOICES,
        required=False,
    )
    voucher_access = forms.ChoiceField(
        label=_("Voucher access"),
        choices=FLAG_CHOICES,
        required=False,
    )
    voucher_state = forms.ChoiceField(
        label=_("Vouchers"),
        choices=(("", _("Any")),) + tuple(OrganizationVoucherState.choices),
        required=False,
    )

    def __init__(self, *args, event=None, organization_type=None, **kwargs):
        self.event = event
        self.organization_type = organization_type
        super().__init__(*args, **kwargs)
        if organization_type == "sponsor":
            del self.fields["group"]
            self.fields["sponsor_group"].queryset = sponsor_group_queryset(event)
        else:
            del self.fields["sponsor_group"]
            self.fields["group"].choices = self.group_choices(event)

    @staticmethod
    def group_choices(event):
        """Every sponsor group, then the two exhibitor groups, as one filter."""
        sponsor_groups = sponsor_group_queryset(event).order_by("level", "pk") if event else []
        return [
            ("", _("All groups")),
            ("sponsors", _("All sponsors")),
            *(
                (f"sponsor-{group.pk}", _("Sponsor · {group}").format(group=group.localized_name))
                for group in sponsor_groups
            ),
            *ExhibitorGroup.choices,
            ("none", _("No group")),
        ]

    def filter_group(self, queryset, value):
        if value == "sponsors":
            return queryset.filter(is_sponsor=True)
        if value.startswith("sponsor-"):
            return queryset.filter(is_sponsor=True, sponsor_group_id=value.removeprefix("sponsor-"))
        if value == "none":
            return queryset.filter(is_sponsor=False, exhibitor_group="")
        return queryset.filter(is_sponsor=False, exhibitor_group=value)

    def apply_ordering(self, queryset):
        """Sorting by group lists sponsors by level, then paid, non-paid and ungrouped exhibitors."""
        ordering = self.cleaned_data.get("ordering") or ""
        if ordering.lstrip("-") != "group":
            return super().apply_ordering(queryset)
        queryset = queryset.annotate(
            group_rank=Case(
                When(is_sponsor=True, then=Value(0)),
                When(exhibitor_group=ExhibitorGroup.PAID, then=Value(1)),
                When(exhibitor_group=ExhibitorGroup.NON_PAID, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        )
        if ordering.startswith("-"):
            return queryset.order_by("-group_rank", F("sponsor_group__level").desc(nulls_first=True), "name", "pk")
        return queryset.order_by("group_rank", F("sponsor_group__level").asc(nulls_last=True), "name", "pk")

    def filter_qs(self, queryset):
        fdata = self.cleaned_data

        query = (fdata.get("query") or "").strip()
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query)
                | Q(booth_id__icontains=query)
                | Q(booth_name__icontains=query)
                | Q(email__icontains=query)
            )

        if fdata.get("active"):
            queryset = apply_flag(queryset, "active", fdata["active"])

        queryset = apply_flag(queryset, "published", fdata.get("published"))

        if fdata.get("sponsor_group"):
            queryset = queryset.filter(sponsor_group=fdata["sponsor_group"])

        if fdata.get("group"):
            queryset = self.filter_group(queryset, fdata["group"])

        queryset = apply_flag(queryset, "lead_scanning_enabled", fdata.get("lead_scanning"))
        queryset = apply_flag(queryset, "allow_lead_access", fdata.get("lead_access"))
        queryset = apply_flag(queryset, "allow_voucher_access", fdata.get("voucher_access"))
        if fdata.get("voucher_state"):
            queryset = queryset.filter(voucher_state=fdata["voucher_state"])
        return self.apply_ordering(queryset)


class PublicExhibitorFilterForm(forms.Form):
    query = forms.CharField(
        label=_("Search exhibitors…"),
        widget=search_widget(_("Search exhibitors…"), _("Search exhibitors")),
        required=False,
    )
    sponsor_group = forms.ModelChoiceField(
        label=_("Sponsor group"),
        queryset=SponsorGroup.objects.none(),
        required=False,
        empty_label=_("All groups"),
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, event=None, **kwargs):
        self.event = event
        super().__init__(*args, **kwargs)
        self.fields["sponsor_group"].queryset = sponsor_group_queryset(event)

    @property
    def has_group_choices(self):
        return self.fields["sponsor_group"].queryset.exists()

    def filter_qs(self, queryset):
        if not self.is_valid():
            return queryset
        fdata = self.cleaned_data

        query = (fdata.get("query") or "").strip()
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(description__icontains=query))

        if fdata.get("sponsor_group"):
            queryset = queryset.filter(sponsor_group=fdata["sponsor_group"])

        return queryset


class EmailFilterForm(ExhibitionFilterForm):
    query = forms.CharField(
        label=_("Search emails…"),
        widget=search_widget(_("Search emails…"), _("Search emails")),
        required=False,
    )

    def __init__(self, *args, date_field="created", hide_emails=False, **kwargs):
        self.hide_emails = hide_emails
        self.orders = {"subject": "subject", date_field: date_field}
        if not hide_emails:
            self.orders["to_email"] = "to_email"
        super().__init__(*args, **kwargs)
        if hide_emails:
            self.fields["query"].label = _("Search by subject…")
            self.fields["query"].widget.attrs.update(
                {
                    "placeholder": _("Search by subject…"),
                    "aria-label": _("Search emails by subject"),
                }
            )

    def filter_qs(self, queryset):
        fdata = self.cleaned_data

        query = (fdata.get("query") or "").strip()
        if query:
            lookup = Q(subject__icontains=query)
            if not self.hide_emails:
                lookup |= Q(to_email__icontains=query)
            queryset = queryset.filter(lookup)

        return queryset
