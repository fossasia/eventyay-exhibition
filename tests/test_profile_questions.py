import datetime

import pytest
from django import forms
from django_scopes import scopes_disabled
from eventyay.base.forms.questions import WrappedPhoneNumberPrefixWidget
from eventyay.base.models import User
from eventyay.control.forms import ExtFileField, SplitDateTimeField
from phonenumber_field.formfields import PhoneNumberField

from exhibition import forms as exhibition_forms
from exhibition.forms import ExhibitionRequestForm, ExhibitorInfoForm, ExhibitorSelfEditForm
from exhibition.models import (
    ExhibitionAnswer,
    ExhibitionQuestion,
    ExhibitionQuestionVariant,
    ExhibitionRequest,
    ExhibitionRequestState,
    ExhibitorGroup,
    ExhibitorInfo,
)

PROFILE_FORMS = [ExhibitorInfoForm, ExhibitorSelfEditForm]

FIELD_TYPES = [
    (ExhibitionQuestionVariant.EMAIL, forms.EmailField),
    (ExhibitionQuestionVariant.NUMBER, forms.DecimalField),
    (ExhibitionQuestionVariant.PHONE, PhoneNumberField),
    (ExhibitionQuestionVariant.DATE, forms.DateField),
    (ExhibitionQuestionVariant.TIME, forms.TimeField),
    (ExhibitionQuestionVariant.DATETIME, SplitDateTimeField),
    (ExhibitionQuestionVariant.FILE, ExtFileField),
]


def _accepted_profile(event):
    """A profile created from an accepted request, which is when profile forms show the call's questions."""
    user = User.objects.create_user(email="applicant@example.com", password="pw")
    with scopes_disabled():
        exhibitor = ExhibitorInfo.objects.create(event=event, name="Acme", is_exhibitor=True)
        request = ExhibitionRequest.objects.create(
            event=event,
            user=user,
            name="Acme",
            state=ExhibitionRequestState.ACCEPTED,
            approved_exhibitor=exhibitor,
        )
    return exhibitor, request


def _question(event, variant):
    return ExhibitionQuestion.objects.create(event=event, variant=variant, question={"en": f"{variant} question"})


def test_profile_forms_use_the_same_question_mixin_as_the_request_form():
    for form_class in [ExhibitionRequestForm, *PROFILE_FORMS]:
        assert exhibition_forms.ExhibitionQuestionFieldsMixin in form_class.__mro__


@pytest.mark.django_db
@pytest.mark.parametrize("form_class", PROFILE_FORMS)
@pytest.mark.parametrize(("variant", "field_type"), FIELD_TYPES)
def test_profile_forms_build_typed_question_fields(event, form_class, variant, field_type):
    exhibitor, _request = _accepted_profile(event)
    question = _question(event, variant)

    form = form_class(instance=exhibitor, event=event)

    assert isinstance(form.fields[f"question_{question.pk}"], field_type)


@pytest.mark.django_db
@pytest.mark.parametrize("form_class", PROFILE_FORMS)
def test_profile_forms_build_a_phone_widget_and_a_country_select(event, form_class):
    exhibitor, _request = _accepted_profile(event)
    phone = _question(event, ExhibitionQuestionVariant.PHONE)
    country = _question(event, ExhibitionQuestionVariant.COUNTRY)

    form = form_class(instance=exhibitor, event=event)

    assert isinstance(form.fields[f"question_{phone.pk}"].widget, WrappedPhoneNumberPrefixWidget)
    country_field = form.fields[f"question_{country.pk}"]
    assert isinstance(country_field.widget, forms.Select)
    assert "DE" in dict(country_field.choices)


@pytest.mark.django_db
def test_profile_form_shows_a_stored_date_answer(event):
    exhibitor, request = _accepted_profile(event)
    question = _question(event, ExhibitionQuestionVariant.DATE)
    with scopes_disabled():
        ExhibitionAnswer.objects.create(exhibition_request=request, question=question, answer="2026-12-01")

    form = ExhibitorInfoForm(instance=exhibitor, event=event)

    assert form.fields[f"question_{question.pk}"].initial == datetime.date(2026, 12, 1)


@pytest.mark.django_db
def test_profile_form_saves_a_time_answer_in_iso_format(event, image_uploads):
    exhibitor, request = _accepted_profile(event)
    question = _question(event, ExhibitionQuestionVariant.TIME)

    form = ExhibitorInfoForm(
        data={
            "name_0": "Acme",
            "exhibitor_group": ExhibitorGroup.PAID,
            f"question_{question.pk}": "10:30",
        },
        files=image_uploads(),
        instance=exhibitor,
        event=event,
        organization_type="exhibitor",
    )
    assert form.is_valid(), form.errors
    with scopes_disabled():
        form.save()
        answer = ExhibitionAnswer.objects.get(exhibition_request=request, question=question)

    assert answer.answer == "10:30:00"
