from django.db.models import Count, Prefetch
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django_scopes import scope
from eventyay.base.exporter import ListExporter

from .models import (
    QUESTION_OPTION_VARIANTS,
    ExhibitionAnswer,
    ExhibitionQuestion,
    ExhibitionQuestionVariant,
    ExhibitionRequest,
    ExhibitorDevice,
    ExhibitorInfo,
    Lead,
    OrganizationVoucherState,
)
from .utils import localized_value_for, public_exhibitors_queryset

EXPORT_FORMAT_XLSX = "xlsx"
EXPORT_FORMAT_CSV = "default"
EXPORT_FORMATS = (EXPORT_FORMAT_XLSX, EXPORT_FORMAT_CSV)
FILE_KINDS = {"exhibitor": "exhibitors", "sponsor": "sponsors"}
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralize_formula(value):
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


class ExhibitionListExporter(ListExporter):
    """Shared plumbing for the organizer-facing CSV / XLSX downloads."""

    def __init__(self, request):
        super().__init__(request.event)
        self.request = request
        self.locale = self.event.settings.locale

    @property
    def export_form_fields(self):
        fields = super().export_form_fields
        fields["_format"].choices = [choice for choice in fields["_format"].choices if choice[0] in EXPORT_FORMATS]
        return fields

    def records(self):
        raise NotImplementedError

    def headers(self):
        raise NotImplementedError

    def row(self, record):
        raise NotImplementedError

    def iterate_list(self, form_data):
        records = self.records()
        with translation.override(self.locale):
            yield self.ProgressSetTotal(total=len(records))
            yield [neutralize_formula(value) for value in self.headers()]
            for record in records:
                yield [neutralize_formula(value) for value in self.row(record)]

    def answer(self, answer):
        if answer is None:
            return ""
        if answer.question.variant == ExhibitionQuestionVariant.FILE:
            return self.link(answer.file_url)
        if answer.question.variant in QUESTION_OPTION_VARIANTS:
            return self.lines(self.text(option.answer) for option in answer.options.all())
        return str(answer.answer_string)

    def text(self, value):
        return localized_value_for(value, self.locale)

    def link(self, url):
        return self.request.build_absolute_uri(url) if url else ""

    def when(self, value):
        return value.astimezone(self.event.tz).strftime("%Y-%m-%d %H:%M") if value else ""

    @staticmethod
    def yes_no(value):
        return _("Yes") if value else _("No")

    @staticmethod
    def lines(values):
        return "\n".join(value for value in values if value)


class OrganizationsExporter(ExhibitionListExporter):
    identifier = "exhibition_organizations"

    def __init__(self, request, organizations, organization_type=None):
        super().__init__(request)
        self.organization_type = organization_type
        self.organizations = list(
            organizations.select_related("sponsor_group").prefetch_related(
                "social_links",
                "extra_links",
                Prefetch(
                    "source_requests",
                    queryset=ExhibitionRequest.objects.select_related("user").prefetch_related(
                        Prefetch(
                            "answers",
                            queryset=ExhibitionAnswer.objects.select_related("question").prefetch_related("options"),
                        )
                    ),
                ),
            )
        )
        pks = [organization.pk for organization in self.organizations]
        self.questions = list(ExhibitionQuestion.objects.filter(event=self.event).order_by("position", "id"))
        self.lead_counts = self._counts(Lead, pks)
        self.device_counts = self._counts(ExhibitorDevice, pks)
        shown = public_exhibitors_queryset(self.event).prefetch_related(None).values_list("pk", flat=True)
        self.positions = {pk: index for index, pk in enumerate(shown, 1)}
        with scope(event=self.event):
            sessions = ExhibitorInfo.sessions.through.objects.filter(exhibitorinfo_id__in=pks).select_related(
                "submission"
            )
            self.sessions = {}
            for link in sessions.order_by("submission__title"):
                self.sessions.setdefault(link.exhibitorinfo_id, []).append(link.submission.title)

    @property
    def verbose_name(self):
        if self.organization_type == "sponsor":
            return _("Sponsors")
        if self.organization_type == "exhibitor":
            return _("Exhibitors")
        return _("Organizations")

    def get_filename(self):
        kind = FILE_KINDS.get(self.organization_type, "organizations")
        return f"{self.event.slug}-{kind}-{timezone.now().astimezone(self.event.tz).date().isoformat()}"

    def records(self):
        return self.organizations

    @staticmethod
    def _counts(model, pks):
        rows = model.objects.filter(exhibitor_id__in=pks).values_list("exhibitor_id").annotate(count=Count("pk"))
        return dict(rows)

    def headers(self):
        return [
            _("Name"),
            _("Description"),
            _("Website"),
            _("Contact page URL"),
            _("Email"),
            _("Logo"),
            _("Banner"),
            _("Video"),
            _("Slides"),
            _("Social media links"),
            _("Additional links"),
            _("Related sessions"),
            _("Type"),
            _("Sponsor group"),
            _("Exhibitor group"),
            _("Published"),
            _("Active"),
            _("Position on the public exhibitor page"),
            _("Booth number"),
            _("Booth name"),
            _("Access key"),
            _("Lead scanning enabled"),
            _("Voucher access"),
            _("Lead access"),
            _("Lead scanning limited to registered devices"),
            _("Registered scanning devices"),
            _("Vouchers assigned"),
            _("Vouchers claimed"),
            _("Voucher state"),
            _("Vouchers last sent"),
            _("Leads scanned"),
            _("Request date"),
            _("Applicant name"),
            _("Applicant email"),
            *(self.text(question.question) for question in self.questions),
        ]

    def row(self, organization):
        application = self.application(organization)
        answers = {answer.question_id: answer for answer in application.answers.all()} if application else {}
        applicant = application.user if application and application.user_id else None
        return [
            self.text(organization.name),
            self.text(organization.description),
            organization.url or "",
            organization.contact_url or "",
            organization.recipient_email,
            self.link(organization.visible_logo_url),
            self.link(organization.visible_banner_url),
            organization.video_url or "",
            self.link(organization.visible_slides_url),
            self.lines(str(link) for link in organization.social_links.all()),
            self.lines(str(link) for link in organization.extra_links.all()),
            self.lines(self.sessions.get(organization.pk, ())),
            self.type_label(organization),
            self.text(organization.sponsor_group.name) if organization.sponsor_group else "",
            self.exhibitor_group_label(organization),
            self.yes_no(organization.published),
            self.yes_no(organization.active),
            self.positions.get(organization.pk, ""),
            organization.booth_number or "",
            organization.localized_booth_name,
            organization.key,
            self.yes_no(organization.lead_scanning_enabled),
            self.yes_no(organization.allow_voucher_access),
            self.yes_no(organization.allow_lead_access),
            self.yes_no(organization.lead_scanning_scope_by_device),
            self.device_counts.get(organization.pk, 0),
            organization.voucher_total,
            organization.voucher_claimed,
            str(OrganizationVoucherState(organization.voucher_state).label),
            self.when(organization.voucher_sent_at),
            self.lead_counts.get(organization.pk, 0),
            self.when(application.submitted if application else None),
            (applicant.fullname or "") if applicant else "",
            ((application.email or "").strip() or (applicant.email if applicant else "")) if application else "",
            *(self.answer(answers.get(question.pk)) for question in self.questions),
        ]

    @staticmethod
    def application(organization):
        requests = sorted(organization.source_requests.all(), key=lambda request: request.pk, reverse=True)
        return requests[0] if requests else None

    @staticmethod
    def exhibitor_group_label(organization):
        if not organization.is_exhibitor:
            return ""
        return str(organization.get_exhibitor_group_display() or _("No group"))

    @staticmethod
    def type_label(organization):
        if organization.is_exhibitor and organization.is_sponsor:
            return _("Exhibitor and sponsor")
        if organization.is_sponsor:
            return _("Sponsor")
        return _("Exhibitor")


class RequestsExporter(ExhibitionListExporter):
    identifier = "exhibition_requests"

    def __init__(self, request, exhibition_requests, hide_emails=False):
        super().__init__(request)
        self.hide_emails = hide_emails
        self.exhibition_requests = list(
            exhibition_requests.select_related("user", "sponsor_group", "approved_exhibitor").prefetch_related(
                Prefetch(
                    "answers",
                    queryset=ExhibitionAnswer.objects.select_related("question").prefetch_related("options"),
                )
            )
        )
        self.questions = list(ExhibitionQuestion.objects.filter(event=self.event).order_by("position", "id"))

    @property
    def verbose_name(self):
        return _("Exhibition requests")

    def get_filename(self):
        return f"{self.event.slug}-exhibition-requests-{timezone.now().astimezone(self.event.tz).date().isoformat()}"

    def records(self):
        return self.exhibition_requests

    def headers(self):
        return [
            _("Code"),
            _("Organization name"),
            *(() if self.hide_emails else (_("Applicant email"),)),
            _("State"),
            _("Sponsor group"),
            _("Submitted"),
            _("Last updated"),
            *(self.text(question.question) for question in self.questions),
            _("Linked exhibitor"),
            _("Linked exhibitor ID"),
        ]

    def row(self, exhibition_request):
        answers = {answer.question_id: answer for answer in exhibition_request.answers.all()}
        exhibitor = exhibition_request.approved_exhibitor
        return [
            exhibition_request.code,
            self.text(exhibition_request.name),
            *(() if self.hide_emails else (self.applicant_email(exhibition_request),)),
            str(exhibition_request.get_state_display()),
            self.text(exhibition_request.sponsor_group.name) if exhibition_request.sponsor_group else "",
            self.when(exhibition_request.submitted),
            self.when(exhibition_request.updated),
            *(self.answer(answers.get(question.pk)) for question in self.questions),
            self.text(exhibitor.name) if exhibitor else "",
            exhibitor.pk if exhibitor else "",
        ]

    @staticmethod
    def applicant_email(exhibition_request):
        applicant = exhibition_request.user if exhibition_request.user_id else None
        return (exhibition_request.email or "").strip() or (applicant.email if applicant else "")
