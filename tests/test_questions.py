import datetime
import re
from unittest.mock import MagicMock, patch

import pytest
from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from eventyay.base.forms.questions import WrappedPhoneNumberPrefixWidget
from eventyay.base.forms.widgets import (
    DatePickerWidget,
    SplitDateTimePickerWidget,
    TimePickerWidget,
)
from eventyay.base.models import User
from eventyay.control.forms import ExtFileField, SplitDateTimeField
from phonenumber_field.formfields import PhoneNumberField

from exhibition.forms import (
    ExhibitionQuestionFieldsMixin,
    ExhibitionQuestionOptionFormSet,
    ExhibitionRequestForm,
    ExhibitorInfoForm,
    ExhibitorSelfEditForm,
    delete_exhibition_answer,
    parse_answer_date,
    parse_answer_datetime,
    parse_answer_time,
)
from exhibition.models import (
    ExhibitionQuestion,
    ExhibitionQuestionOption,
    ExhibitionQuestionVariant,
    ExhibitorSettings,
)
from exhibition.views import ExhibitionQuestionOptionFormSetMixin


def option_formset_data(options, *, initial_forms):
    data = {
        "options-TOTAL_FORMS": str(len(options)),
        "options-INITIAL_FORMS": str(initial_forms),
        "options-MIN_NUM_FORMS": "0",
        "options-MAX_NUM_FORMS": "1000",
    }
    for index, option in enumerate(options):
        data.update(
            {
                f"options-{index}-id": str(option.get("id", "")),
                f"options-{index}-answer_0": option.get("answer", ""),
                f"options-{index}-ORDER": str(option["order"]),
            }
        )
        if option.get("delete"):
            data[f"options-{index}-DELETE"] = "on"
    return data


def option_formset_view(event, question, data):
    view = ExhibitionQuestionOptionFormSetMixin()
    request = RequestFactory().post("/", data)
    request.event = event
    view.request = request
    view.object = question
    return view


def option_formset_get_view(event, question=None):
    view = ExhibitionQuestionOptionFormSetMixin()
    request = RequestFactory().get("/")
    request.event = event
    view.request = request
    view.object = question
    return view


@pytest.mark.django_db
def test_new_question_option_formset_does_not_include_existing_question_options(event):
    existing_question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.SELECT,
        question={"en": "Existing question"},
    )
    ExhibitionQuestionOption.objects.create(
        question=existing_question,
        answer={"en": "First existing option"},
        position=0,
    )
    ExhibitionQuestionOption.objects.create(
        question=existing_question,
        answer={"en": "Second existing option"},
        position=1,
    )

    view = option_formset_get_view(event)

    assert view.option_formset.initial_form_count() == 0
    assert view.option_formset.total_form_count() == 0


@pytest.mark.django_db
def test_edit_question_option_formset_includes_only_its_own_options(event):
    question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.SELECT,
        question={"en": "Question being edited"},
    )
    option = ExhibitionQuestionOption.objects.create(
        question=question,
        answer={"en": "Its own option"},
        position=0,
    )
    other_question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.CHOICES,
        question={"en": "Another question"},
    )
    ExhibitionQuestionOption.objects.create(
        question=other_question,
        answer={"en": "Other question option"},
        position=0,
    )

    view = option_formset_get_view(event, question)

    assert list(view.option_formset.queryset) == [option]


@pytest.mark.django_db
def test_choice_option_formset_requires_an_option(event):
    formset = ExhibitionQuestionOptionFormSet(
        option_formset_data([], initial_forms=0),
        event=event,
        prefix="options",
        requires_option=True,
    )

    assert not formset.is_valid()
    assert "Please provide at least one option" in formset.non_form_errors().as_text()


@pytest.mark.django_db
def test_choice_option_formset_rejects_only_blank_options(event):
    formset = ExhibitionQuestionOptionFormSet(
        option_formset_data([{"answer": "", "order": 0}], initial_forms=0),
        event=event,
        prefix="options",
        requires_option=True,
    )

    assert not formset.is_valid()
    assert "Please provide at least one option" in formset.non_form_errors().as_text()


@pytest.mark.django_db
def test_choice_option_formset_saves_ordered_options(event):
    question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.CHOICES,
        question={"en": "Which option?"},
    )
    first = ExhibitionQuestionOption.objects.create(question=question, answer={"en": "First"}, position=0)
    second = ExhibitionQuestionOption.objects.create(question=question, answer={"en": "Second"}, position=1)
    view = option_formset_view(
        event,
        question,
        option_formset_data(
            [
                {"id": second.pk, "answer": "Second", "order": 0},
                {"id": first.pk, "answer": "First", "order": 1},
                {"answer": "Third", "order": 2},
            ],
            initial_forms=2,
        ),
    )

    assert view.option_formset.is_valid()
    view.save_option_formset()

    assert list(question.options.values_list("answer", "position")) == [
        ({"en": "Second"}, 0),
        ({"en": "First"}, 1),
        ({"en": "Third"}, 2),
    ]


@pytest.mark.django_db
def test_choice_option_formset_ignores_empty_extra_options(event):
    question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.CHOICES,
        question={"en": "Which option?"},
    )
    existing_option = ExhibitionQuestionOption.objects.create(
        question=question,
        answer={"en": "Existing option"},
        position=0,
    )
    view = option_formset_view(
        event,
        question,
        option_formset_data(
            [
                {"id": existing_option.pk, "answer": "Existing option", "order": 0},
                {"answer": "", "order": 1},
            ],
            initial_forms=1,
        ),
    )

    assert view.option_formset.is_valid()
    view.save_option_formset()

    assert list(question.options.values_list("answer", "position")) == [({"en": "Existing option"}, 0)]


@pytest.mark.django_db
def test_choice_option_formset_ignores_deleted_extra_options(event):
    question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.CHOICES,
        question={"en": "Which option?"},
    )
    existing_option = ExhibitionQuestionOption.objects.create(
        question=question,
        answer={"en": "Existing option"},
        position=0,
    )
    view = option_formset_view(
        event,
        question,
        option_formset_data(
            [
                {"id": existing_option.pk, "answer": "Existing option", "order": 0},
                {"answer": "", "order": 1, "delete": True},
            ],
            initial_forms=1,
        ),
    )

    assert view.option_formset.is_valid()
    view.save_option_formset()

    assert list(question.options.values_list("answer", "position")) == [({"en": "Existing option"}, 0)]


@pytest.mark.django_db
def test_choice_option_formset_deletes_marked_options(event):
    question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.SELECT,
        question={"en": "Which option?"},
    )
    first = ExhibitionQuestionOption.objects.create(question=question, answer={"en": "First"}, position=0)
    second = ExhibitionQuestionOption.objects.create(question=question, answer={"en": "Second"}, position=1)
    view = option_formset_view(
        event,
        question,
        option_formset_data(
            [
                {"id": first.pk, "answer": "First", "order": 0},
                {"id": second.pk, "answer": "Second", "order": 1, "delete": True},
            ],
            initial_forms=2,
        ),
    )

    assert view.option_formset.is_valid()
    view.save_option_formset()

    assert list(question.options.values_list("answer", flat=True)) == [{"en": "First"}]


@pytest.mark.django_db
def test_non_choice_question_clears_existing_options(event):
    question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.STRING,
        question={"en": "Short answer"},
    )
    ExhibitionQuestionOption.objects.create(question=question, answer={"en": "Unused"}, position=0)

    view = ExhibitionQuestionOptionFormSetMixin()
    view.object = question
    view.save_option_formset()

    assert not question.options.exists()


@pytest.mark.django_db
def test_phone_question_builds_a_form_field(event):
    question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.PHONE,
        question={"en": "Contact phone"},
    )

    field = ExhibitionQuestionFieldsMixin().get_exhibition_question_field(
        question=question,
        answer=None,
        readonly=False,
    )

    assert isinstance(field.widget, WrappedPhoneNumberPrefixWidget)
    assert field.widget.render("phone", None)


@pytest.mark.django_db
def test_public_request_form_renders_with_phone_question(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    ExhibitorSettings.objects.create(
        event=event,
        call_enabled=True,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
    )
    ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.PHONE,
        question={"en": "Contact phone"},
    )
    applicant = User.objects.create_user(
        email="applicant@example.com",
        password="secret",
        fullname="Applicant",
        locale="en",
    )
    client = Client()
    client.force_login(applicant)

    response = client.get(
        reverse(
            "plugins:exhibition:request.add",
            kwargs={"organizer": event.organizer.slug, "event": event.slug},
        )
    )

    assert response.status_code == 200
    assert "Contact phone" in response.content.decode()


def _option_labels(html):
    return re.findall(r'class="question-option-label">\s*([^<]*?)\s*<', html)


def _question_editor_client(event):
    event.plugins = "exhibition"
    event.save(update_fields=["plugins"])
    user = User.objects.create_user(email="fields@example.com", password="pw")
    team = event.organizer.teams.create(name="Organizers", all_events=True, can_change_event_settings=True)
    team.members.add(user)
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_unsaved_answer_options_keep_their_new_label_after_a_validation_error(event):
    client = _question_editor_client(event)
    url = reverse(
        "plugins:exhibition:call.questions.add",
        kwargs={"organizer": event.organizer.slug, "event": event.slug},
    )
    data = {
        "question_0": "",
        "variant": ExhibitionQuestionVariant.CHOICES,
        **option_formset_data(
            [{"answer": "Small booth", "order": 0}, {"answer": "Large booth", "order": 1}],
            initial_forms=0,
        ),
    }

    html = client.post(url, data).content.decode()

    assert "Answer option None" not in html
    assert _option_labels(html)[:2] == ["New answer option", "New answer option"]


@pytest.mark.django_db
@override_settings(SITE_URL="https://testserver")
def test_saved_answer_options_are_numbered_by_position(event):
    client = _question_editor_client(event)
    question = ExhibitionQuestion.objects.create(
        event=event,
        variant=ExhibitionQuestionVariant.CHOICES,
        question={"en": "Booth size"},
    )
    for position, answer in enumerate(["Small booth", "Large booth"]):
        ExhibitionQuestionOption.objects.create(question=question, answer={"en": answer}, position=position)
    url = reverse(
        "plugins:exhibition:call.questions.edit",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, "pk": question.pk},
    )

    html = client.get(url).content.decode()

    assert _option_labels(html)[:2] == ["Answer option 1", "Answer option 2"]


def test_exhibitor_forms_inherit_from_canonical_question_fields_mixin():
    """Verify that all exhibitor forms share the canonical ExhibitionQuestionFieldsMixin in MRO."""
    assert issubclass(ExhibitorInfoForm, ExhibitionQuestionFieldsMixin)
    assert issubclass(ExhibitorSelfEditForm, ExhibitionQuestionFieldsMixin)
    assert issubclass(ExhibitionRequestForm, ExhibitionQuestionFieldsMixin)
    assert (
        ExhibitorInfoForm.get_exhibition_question_field == ExhibitionQuestionFieldsMixin.get_exhibition_question_field
    )
    assert (
        ExhibitorSelfEditForm.get_exhibition_question_field
        == ExhibitionQuestionFieldsMixin.get_exhibition_question_field
    )


def test_exhibitor_info_form_builds_rich_question_fields():
    """Verify ExhibitorInfoForm builds rich widgets and form fields for every question variant."""
    variants_and_checks = [
        (
            ExhibitionQuestionVariant.PHONE,
            lambda f: isinstance(f, PhoneNumberField) and isinstance(f.widget, WrappedPhoneNumberPrefixWidget),
        ),
        (ExhibitionQuestionVariant.EMAIL, lambda f: isinstance(f, forms.EmailField)),
        (ExhibitionQuestionVariant.NUMBER, lambda f: isinstance(f, forms.DecimalField)),
        (ExhibitionQuestionVariant.BOOLEAN, lambda f: isinstance(f, forms.BooleanField)),
        (
            ExhibitionQuestionVariant.TEXT,
            lambda f: isinstance(f, forms.CharField) and isinstance(f.widget, forms.Textarea),
        ),
        (ExhibitionQuestionVariant.URL, lambda f: isinstance(f, forms.URLField)),
        (
            ExhibitionQuestionVariant.DATE,
            lambda f: isinstance(f, forms.DateField) and isinstance(f.widget, DatePickerWidget),
        ),
        (
            ExhibitionQuestionVariant.TIME,
            lambda f: isinstance(f, forms.TimeField) and isinstance(f.widget, TimePickerWidget),
        ),
        (
            ExhibitionQuestionVariant.DATETIME,
            lambda f: isinstance(f, SplitDateTimeField) and isinstance(f.widget, SplitDateTimePickerWidget),
        ),
        (ExhibitionQuestionVariant.FILE, lambda f: isinstance(f, ExtFileField)),
    ]

    for variant, check in variants_and_checks:
        question = ExhibitionQuestion(
            variant=variant,
            question={"en": f"Question for {variant}"},
        )
        field = ExhibitorInfoForm.get_exhibition_question_field(
            None,
            question=question,
            answer=None,
            readonly=False,
        )
        assert check(field), f"Field for {variant} failed check: {type(field)}"


def test_exhibitor_self_edit_form_builds_rich_question_fields():
    """Verify ExhibitorSelfEditForm builds ExtFileField for FILE question variant."""
    question = ExhibitionQuestion(
        variant=ExhibitionQuestionVariant.FILE,
        question={"en": "Upload brochure"},
    )
    field = ExhibitorSelfEditForm.get_exhibition_question_field(
        None,
        question=question,
        answer=None,
        readonly=False,
    )
    assert isinstance(field, ExtFileField)


class _DummyAnswer:
    def __init__(self, answer, file=None):
        self.answer = answer
        self.file = file


def test_question_fields_mixin_parses_initial_date_and_time():
    """Verify that stored date and time answer strings are parsed into python date/time initial values."""
    mixin = ExhibitionQuestionFieldsMixin()

    date_q = ExhibitionQuestion(
        variant=ExhibitionQuestionVariant.DATE,
        question={"en": "Arrival date"},
    )
    date_field = mixin.get_exhibition_question_field(
        question=date_q,
        answer=_DummyAnswer("2025-06-15"),
        readonly=False,
    )
    assert isinstance(date_field.widget, DatePickerWidget)
    assert date_field.initial == datetime.date(2025, 6, 15)

    time_q = ExhibitionQuestion(
        variant=ExhibitionQuestionVariant.TIME,
        question={"en": "Arrival time"},
    )
    time_field = mixin.get_exhibition_question_field(
        question=time_q,
        answer=_DummyAnswer("14:30:00"),
        readonly=False,
    )
    assert isinstance(time_field.widget, TimePickerWidget)
    assert time_field.initial == datetime.time(14, 30, 0)


def test_parse_answer_date_does_not_shift_calendar_day():
    """Verify date-only answers parse directly into date objects without timezone conversion shifting."""
    assert parse_answer_date("2025-06-15") == datetime.date(2025, 6, 15)
    assert parse_answer_date(None) is None
    assert parse_answer_date("") is None
    assert parse_answer_date("invalid-date") is None


def test_parse_answer_time_valid_and_invalid():
    """Verify time parsing for valid and invalid inputs."""
    assert parse_answer_time("14:30:00") == datetime.time(14, 30, 0)
    assert parse_answer_time(None) is None
    assert parse_answer_time("") is None
    assert parse_answer_time("invalid-time") is None


def test_parse_answer_datetime_converts_to_active_timezone():
    """Verify datetime parsing converts correctly and handles empty/invalid values."""
    with timezone.override("Europe/Berlin"):
        dt = parse_answer_datetime("2025-06-15T14:30:00Z")
        assert dt is not None
        assert dt.hour == 16
        assert dt.utcoffset() == datetime.timedelta(hours=2)
    assert parse_answer_datetime(None) is None
    assert parse_answer_datetime("") is None
    assert parse_answer_datetime("invalid-dt") is None


def test_delete_exhibition_answer_defers_file_deletion_to_on_commit():
    """Verify delete_exhibition_answer schedules file deletion on transaction commit."""
    answer = MagicMock()
    answer.file.name = "test_file.pdf"

    with (
        patch("exhibition.forms.transaction.on_commit") as mock_on_commit,
        patch("exhibition.forms.default_storage.delete") as mock_delete,
    ):
        delete_exhibition_answer(answer)
        answer.delete.assert_called_once()
        mock_on_commit.assert_called_once()

        callback = mock_on_commit.call_args[0][0]
        mock_delete.assert_not_called()
        callback()
        mock_delete.assert_called_once_with("test_file.pdf")

    answer_no_file = MagicMock()
    answer_no_file.file = None

    with patch("exhibition.forms.transaction.on_commit") as mock_on_commit:
        delete_exhibition_answer(answer_no_file)
        answer_no_file.delete.assert_called_once()
        mock_on_commit.assert_not_called()


def test_save_exhibition_questions_defers_file_deletion_on_clear():
    """Verify clearing a file field defers old file deletion until transaction commit."""
    mixin = ExhibitionQuestionFieldsMixin()
    mixin.cleaned_data = {"question_1": False}

    field = ExtFileField(ext_whitelist=[".pdf"])
    field.question = MagicMock()
    answer = MagicMock()
    answer.file.name = "old_file.pdf"
    field.answer = answer
    mixin.fields = {"question_1": field}

    request = MagicMock()

    with (
        patch("exhibition.forms.transaction.on_commit") as mock_on_commit,
        patch("exhibition.forms.default_storage.delete") as mock_delete,
    ):
        mixin.save_exhibition_questions(request)
        assert answer.file is None
        assert answer.answer == ""
        answer.save.assert_called_once()
        mock_on_commit.assert_called_once()

        callback = mock_on_commit.call_args[0][0]
        mock_delete.assert_not_called()
        callback()
        mock_delete.assert_called_once_with("old_file.pdf")


def test_save_exhibition_questions_defers_file_deletion_on_replace():
    """Verify replacing a file field defers old file deletion until transaction commit."""
    mixin = ExhibitionQuestionFieldsMixin()
    new_file = SimpleUploadedFile("new_file.pdf", b"new content")
    mixin.cleaned_data = {"question_1": new_file}

    field = ExtFileField(ext_whitelist=[".pdf"])
    field.question = MagicMock()
    answer = MagicMock()
    answer.file.name = "old_file.pdf"
    field.answer = answer
    mixin.fields = {"question_1": field}

    request = MagicMock()

    with (
        patch("exhibition.forms.transaction.on_commit") as mock_on_commit,
        patch("exhibition.forms.default_storage.delete") as mock_delete,
    ):
        mixin.save_exhibition_questions(request)
        assert answer.file == new_file
        assert answer.answer == "new_file.pdf"
        answer.save.assert_called_once()
        mock_on_commit.assert_called_once()

        callback = mock_on_commit.call_args[0][0]
        mock_delete.assert_not_called()
        callback()
        mock_delete.assert_called_once_with("old_file.pdf")
