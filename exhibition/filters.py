from django import forms
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from eventyay.control.forms.filter import FilterForm

from .models import (
    ExhibitionOrderPosition,
    ExhibitionOrderStatus,
    ExhibitionProduct,
    ExhibitionProductPurpose,
    ExhibitionRequestState,
    OrganizationVoucherState,
    SponsorGroup,
)

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
        if organization_type == "exhibitor":
            del self.fields["sponsor_group"]
        else:
            self.fields["sponsor_group"].queryset = sponsor_group_queryset(event)

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


class OrderFilterForm(ExhibitionFilterForm):
    orders = {
        "code": "code",
        "organization": "organization_name",
        "total": "total",
        "status": "status",
        "created": "created",
    }

    query = forms.CharField(
        label=_("Search orders…"),
        widget=search_widget(_("Search by organization or order code…"), _("Search orders")),
        required=False,
    )
    status = forms.ChoiceField(
        label=_("Order status"),
        choices=(("", _("All statuses")),) + tuple(ExhibitionOrderStatus.choices),
        required=False,
    )
    product = forms.ModelChoiceField(
        label=_("Product"),
        queryset=ExhibitionProduct.objects.none(),
        required=False,
        empty_label=_("All products"),
    )
    purpose = forms.ChoiceField(
        label=_("Product purpose"),
        choices=(("", _("All purposes")),) + tuple(ExhibitionProductPurpose.choices),
        required=False,
    )
    includes_booth = forms.ChoiceField(
        label=_("Includes exhibition booth"),
        choices=(
            ("", _("Any")),
            ("1", _("Yes")),
            ("0", _("No")),
        ),
        required=False,
    )

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = ExhibitionProduct.objects.filter(event=event).in_sales_order()

    def filter_qs(self, queryset):
        fdata = self.cleaned_data

        query = (fdata.get("query") or "").strip()
        if query:
            queryset = queryset.filter(Q(code__icontains=query) | Q(organization_name__icontains=query))

        if fdata.get("status"):
            queryset = queryset.filter(status=fdata["status"])

        positions = ExhibitionOrderPosition.objects.all()
        if fdata.get("product"):
            positions = positions.filter(product=fdata["product"])
        if fdata.get("purpose"):
            positions = positions.filter(purpose=fdata["purpose"])
        positions = apply_flag(positions, "includes_booth", fdata.get("includes_booth"))
        if fdata.get("product") or fdata.get("purpose") or fdata.get("includes_booth"):
            queryset = queryset.filter(pk__in=positions.values("order_id"))

        return self.apply_ordering(queryset)
