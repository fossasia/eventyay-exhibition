import pytest
from django.http import QueryDict
from eventyay.base.models import User

from exhibition.forms import ExhibitionProposalForm, ExhibitionQuestionForm
from exhibition.models import (
    ExhibitionAnswer,
    ExhibitionProposal,
    ExhibitionQuestion,
    ExhibitionQuestionOption,
    ExhibitionQuestionVariant,
    ExhibitorSettings,
)


def make_question(event, *, variant=ExhibitionQuestionVariant.STRING, label="Question", **kwargs):
    return ExhibitionQuestion.objects.create(
        event=event,
        variant=variant,
        question={"en": label},
        **kwargs,
    )


def make_choice_question(event, *, label="Pick one", answers=("Yes", "No"), variant=None):
    question = make_question(event, variant=variant or ExhibitionQuestionVariant.SELECT, label=label)
    options = [
        ExhibitionQuestionOption.objects.create(question=question, answer={"en": answer}, position=index)
        for index, answer in enumerate(answers)
    ]
    return question, options


def question_form_data(**overrides):
    data = QueryDict("", mutable=True)
    data.update(
        {
            "question_0": "How much furniture?",
            "variant": ExhibitionQuestionVariant.STRING,
            "active": "on",
        }
    )
    dependency_values = overrides.pop("dependency_values", None)
    data.update(overrides)
    if dependency_values is not None:
        data.setlist("dependency_values", dependency_values)
    return data


def make_proposal(event, email="applicant@example.org"):
    user = User.objects.create_user(email=email, password="dependency-tests-1234")
    return ExhibitionProposal.objects.create(event=event, user=user, name={"en": "Acme"})


def proposal_form(event, data):
    payload = {"name": "Acme", "content_locale": "en"}
    payload.update(data)
    return ExhibitionProposalForm(event=event, data=payload)


@pytest.fixture
def call_event(event):
    ExhibitorSettings.objects.create(
        event=event,
        call_enabled=True,
        exhibitors_access_mail_subject="",
        exhibitors_access_mail_body="",
    )
    return event


# --- organiser form: layout parity with the Tickets custom field form -------------------


@pytest.mark.django_db
def test_form_field_order_matches_tickets(call_event):
    form = ExhibitionQuestionForm(event=call_event)
    assert list(form.fields) == [
        "question",
        "variant",
        "required",
        "help_text",
        "dependency_question",
        "dependency_values",
        "active",
    ]


@pytest.mark.django_db
def test_form_labels_match_tickets(call_event):
    form = ExhibitionQuestionForm(event=call_event)
    assert str(form.fields["question"].label) == "Custom field"
    assert str(form.fields["variant"].label) == "Type"
    assert str(form.fields["required"].label) == "Required field"
    assert str(form.fields["dependency_question"].label) == "Custom field dependency"


# --- organiser form: dependency configuration -------------------------------------------


@pytest.mark.django_db
def test_only_fields_with_fixed_answers_can_be_depended_on(call_event):
    parent, _options = make_choice_question(call_event)
    boolean = make_question(call_event, variant=ExhibitionQuestionVariant.BOOLEAN, label="Need furniture?")
    make_question(call_event, variant=ExhibitionQuestionVariant.STRING, label="Free text")

    candidates = set(ExhibitionQuestionForm(event=call_event).dependency_candidates)

    assert candidates == {parent, boolean}


@pytest.mark.django_db
def test_a_field_cannot_depend_on_itself(call_event):
    question, _options = make_choice_question(call_event)

    form = ExhibitionQuestionForm(event=call_event, instance=question)

    assert question not in form.dependency_candidates


@pytest.mark.django_db
def test_dependency_is_saved_with_its_values(call_event):
    parent, options = make_choice_question(call_event)

    form = ExhibitionQuestionForm(
        event=call_event,
        data=question_form_data(
            dependency_question=str(parent.pk),
            dependency_values=[str(options[0].pk)],
        ),
    )

    assert form.is_valid(), form.errors
    saved = form.save()
    saved.refresh_from_db()
    assert saved.dependency_question == parent
    assert saved.dependency_values == [str(options[0].pk)]


@pytest.mark.django_db
def test_boolean_parent_offers_checked_and_unchecked(call_event):
    parent = make_question(call_event, variant=ExhibitionQuestionVariant.BOOLEAN, label="Need furniture?")

    form = ExhibitionQuestionForm(
        event=call_event,
        data=question_form_data(dependency_question=str(parent.pk), dependency_values=["True"]),
    )

    assert form.is_valid(), form.errors
    assert [value for value, _label in parent.dependency_value_choices()] == ["True", "False"]


@pytest.mark.django_db
def test_dependency_without_values_is_rejected(call_event):
    parent, _options = make_choice_question(call_event)

    form = ExhibitionQuestionForm(
        event=call_event,
        data=question_form_data(dependency_question=str(parent.pk), dependency_values=[]),
    )

    assert not form.is_valid()
    assert "dependency_values" in form.errors


@pytest.mark.django_db
def test_value_from_another_field_is_rejected(call_event):
    parent, _options = make_choice_question(call_event)
    other, other_options = make_choice_question(call_event, label="Unrelated", answers=("A", "B"))

    form = ExhibitionQuestionForm(
        event=call_event,
        data=question_form_data(
            dependency_question=str(parent.pk),
            dependency_values=[str(other_options[0].pk)],
        ),
    )

    assert not form.is_valid()
    assert "dependency_values" in form.errors


@pytest.mark.django_db
def test_circular_dependency_is_rejected(call_event):
    first, first_options = make_choice_question(call_event, label="First")
    second, second_options = make_choice_question(call_event, label="Second")
    second.dependency_question = first
    second.dependency_values = [str(first_options[0].pk)]
    second.save()

    form = ExhibitionQuestionForm(
        event=call_event,
        instance=first,
        data=question_form_data(
            question_0="First",
            variant=ExhibitionQuestionVariant.SELECT,
            dependency_question=str(second.pk),
            dependency_values=[str(second_options[0].pk)],
        ),
    )

    assert not form.is_valid()
    assert "dependency_question" in form.errors


@pytest.mark.django_db
def test_editing_restores_the_saved_dependency(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()

    form = ExhibitionQuestionForm(event=call_event, instance=child)

    assert form.initial["dependency_question"] == parent.pk
    assert form.dependency_value_map[str(parent.pk)] == [
        {"value": str(options[0].pk), "label": "Yes"},
        {"value": str(options[1].pk), "label": "No"},
    ]


@pytest.mark.django_db
def test_clearing_the_dependency_clears_its_values(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()

    form = ExhibitionQuestionForm(
        event=call_event,
        instance=child,
        data=question_form_data(question_0="Which furniture?", dependency_question="", dependency_values=[]),
    )

    assert form.is_valid(), form.errors
    saved = form.save()
    saved.refresh_from_db()
    assert saved.dependency_question is None
    assert saved.dependency_values == []


# --- public application form: conditional visibility ------------------------------------


@pytest.mark.django_db
def test_widget_carries_the_dependency_attributes(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()

    form = proposal_form(call_event, {})
    attrs = form.fields[f"question_{child.pk}"].widget.attrs

    assert attrs["data-question-dependency"] == parent.pk
    assert attrs["data-question-dependency-values"] == f'["{options[0].pk}"]'


@pytest.mark.django_db
def test_hidden_dependent_field_is_not_required(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?", required=True)
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()

    form = proposal_form(
        call_event,
        {f"question_{parent.pk}": str(options[1].pk), f"question_{child.pk}": ""},
    )

    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_visible_dependent_field_is_required(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?", required=True)
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()

    form = proposal_form(
        call_event,
        {f"question_{parent.pk}": str(options[0].pk), f"question_{child.pk}": ""},
    )

    assert not form.is_valid()
    assert f"question_{child.pk}" in form.errors


@pytest.mark.django_db
def test_visible_dependent_field_accepts_an_answer(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?", required=True)
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()

    form = proposal_form(
        call_event,
        {f"question_{parent.pk}": str(options[0].pk), f"question_{child.pk}": "Two chairs"},
    )

    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_boolean_parent_drives_visibility(call_event):
    parent = make_question(call_event, variant=ExhibitionQuestionVariant.BOOLEAN, label="Need furniture?")
    child = make_question(call_event, label="Which furniture?", required=True)
    child.dependency_question = parent
    child.dependency_values = ["True"]
    child.save()

    unchecked = proposal_form(call_event, {f"question_{child.pk}": ""})
    checked = proposal_form(call_event, {f"question_{parent.pk}": "on", f"question_{child.pk}": ""})

    assert unchecked.is_valid(), unchecked.errors
    assert not checked.is_valid()


@pytest.mark.django_db
def test_a_broken_chain_leaves_the_leaf_alone(call_event):
    root, root_options = make_choice_question(call_event, label="Need a booth?")
    middle, middle_options = make_choice_question(call_event, label="Which size?", answers=("Small", "Large"))
    middle.dependency_question = root
    middle.dependency_values = [str(root_options[0].pk)]
    middle.save()
    leaf = make_question(call_event, label="Anything else?", required=True)
    leaf.dependency_question = middle
    leaf.dependency_values = [str(middle_options[0].pk)]
    leaf.save()

    form = proposal_form(
        call_event,
        {
            f"question_{root.pk}": str(root_options[1].pk),
            f"question_{middle.pk}": str(middle_options[0].pk),
            f"question_{leaf.pk}": "",
        },
    )

    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_a_deactivated_parent_hides_its_dependents(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?", required=True)
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()
    parent.active = False
    parent.save()

    form = proposal_form(call_event, {f"question_{child.pk}": ""})

    assert form.is_valid(), form.errors
    assert f"question_{child.pk}" in form.hidden_question_fields


@pytest.mark.django_db
def test_hidden_answers_are_not_stored(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()

    form = proposal_form(
        call_event,
        {f"question_{parent.pk}": str(options[1].pk), f"question_{child.pk}": "Two chairs"},
    )
    assert form.is_valid(), form.errors
    proposal = make_proposal(call_event)
    form.save_exhibition_questions(proposal)

    assert not ExhibitionAnswer.objects.filter(proposal=proposal, question=child).exists()


@pytest.mark.django_db
def test_an_answer_is_dropped_when_its_field_becomes_hidden(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()
    proposal = make_proposal(call_event, email="editor@example.org")
    ExhibitionAnswer.objects.create(proposal=proposal, question=child, answer="Two chairs")

    form = ExhibitionProposalForm(
        event=call_event,
        instance=proposal,
        data={
            "name": "Acme",
            "content_locale": "en",
            f"question_{parent.pk}": str(options[1].pk),
        },
    )
    assert form.is_valid(), form.errors
    form.save_exhibition_questions(proposal)

    assert not ExhibitionAnswer.objects.filter(proposal=proposal, question=child).exists()


# --- review follow-ups: cleanup when the configuration itself changes --------------------


@pytest.mark.django_db
def test_deleting_an_option_drops_it_from_dependents(call_event):
    parent, options = make_choice_question(call_event, answers=("Yes", "No", "Maybe"))
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk), str(options[1].pk)]
    child.save()

    options[0].delete()

    child.refresh_from_db()
    assert child.dependency_question == parent
    assert child.dependency_values == [str(options[1].pk)]


@pytest.mark.django_db
def test_deleting_the_last_option_makes_a_dependent_unconditional(call_event):
    parent, options = make_choice_question(call_event, answers=("Yes",))
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()

    options[0].delete()

    child.refresh_from_db()
    assert child.dependency_question is None
    assert child.dependency_values == []


@pytest.mark.django_db
def test_deleting_the_parent_leaves_no_stale_configuration(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()

    parent.delete()

    child.refresh_from_db()
    assert child.dependency_question is None
    assert child.dependency_values == []


@pytest.mark.django_db
def test_a_dependent_can_still_be_saved_after_its_option_went_away(call_event):
    parent, options = make_choice_question(call_event, answers=("Yes", "No"))
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()
    options[0].delete()
    child.refresh_from_db()

    form = ExhibitionQuestionForm(
        event=call_event,
        instance=child,
        data=question_form_data(question_0="Which furniture?", dependency_question="", dependency_values=[]),
    )

    assert form.is_valid(), form.errors


# --- review follow-ups: answers are only discarded when the visitor could act ------------


@pytest.mark.django_db
def test_an_answer_survives_a_deactivated_parent(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()
    proposal = make_proposal(call_event, email="kept@example.org")
    ExhibitionAnswer.objects.create(proposal=proposal, question=child, answer="Two chairs")
    parent.active = False
    parent.save()

    form = ExhibitionProposalForm(
        event=call_event,
        instance=proposal,
        data={"name": "Acme", "content_locale": "en"},
    )
    assert form.is_valid(), form.errors
    form.save_exhibition_questions(proposal)

    answer = ExhibitionAnswer.objects.get(proposal=proposal, question=child)
    assert answer.answer == "Two chairs"


@pytest.mark.django_db
def test_an_answer_survives_a_deleted_parent(call_event):
    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?")
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()
    proposal = make_proposal(call_event, email="kept-deleted@example.org")
    ExhibitionAnswer.objects.create(proposal=proposal, question=child, answer="Two chairs")
    parent.delete()

    form = ExhibitionProposalForm(
        event=call_event,
        instance=proposal,
        data={"name": "Acme", "content_locale": "en", f"question_{child.pk}": "Two chairs"},
    )
    assert form.is_valid(), form.errors
    form.save_exhibition_questions(proposal)

    assert ExhibitionAnswer.objects.filter(proposal=proposal, question=child).exists()


@pytest.mark.django_db
def test_deleting_an_answer_removes_its_file(call_event):
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Upload a floor plan", variant=ExhibitionQuestionVariant.FILE)
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()
    proposal = make_proposal(call_event, email="file@example.org")
    answer = ExhibitionAnswer.objects.create(proposal=proposal, question=child)
    answer.file.save("floor-plan.txt", ContentFile(b"plan"), save=True)
    stored_name = answer.file.name
    assert default_storage.exists(stored_name)

    form = ExhibitionProposalForm(
        event=call_event,
        instance=proposal,
        data={"name": "Acme", "content_locale": "en", f"question_{parent.pk}": str(options[1].pk)},
    )
    assert form.is_valid(), form.errors
    form.save_exhibition_questions(proposal)

    assert not ExhibitionAnswer.objects.filter(proposal=proposal, question=child).exists()
    assert not default_storage.exists(stored_name)


# --- review follow-up: the organiser-side form goes through the same mixin ---------------


@pytest.mark.django_db
def test_organiser_form_drops_a_hidden_answer(call_event):
    from exhibition.forms import ExhibitorInfoForm
    from exhibition.models import ExhibitorInfo

    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?", required=True)
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()
    exhibitor = ExhibitorInfo.objects.create(event=call_event, name={"en": "Acme"}, is_exhibitor=True)
    proposal = make_proposal(call_event, email="organiser-side@example.org")
    proposal.approved_exhibitor = exhibitor
    proposal.save()
    ExhibitionAnswer.objects.create(proposal=proposal, question=child, answer="Two chairs")

    form = ExhibitorInfoForm(
        data={"name_0": "Acme", f"question_{parent.pk}": str(options[1].pk)},
        event=call_event,
        instance=exhibitor,
        partner_type="exhibitor",
    )

    assert form.is_valid(), form.errors
    assert f"question_{child.pk}" in form.hidden_question_fields
    form.save_exhibition_questions(proposal)
    assert not ExhibitionAnswer.objects.filter(proposal=proposal, question=child).exists()


@pytest.mark.django_db
def test_organiser_form_keeps_an_answer_whose_parent_was_deactivated(call_event):
    from exhibition.forms import ExhibitorInfoForm
    from exhibition.models import ExhibitorInfo

    parent, options = make_choice_question(call_event)
    child = make_question(call_event, label="Which furniture?", required=True)
    child.dependency_question = parent
    child.dependency_values = [str(options[0].pk)]
    child.save()
    exhibitor = ExhibitorInfo.objects.create(event=call_event, name={"en": "Acme"}, is_exhibitor=True)
    proposal = make_proposal(call_event, email="organiser-kept@example.org")
    proposal.approved_exhibitor = exhibitor
    proposal.save()
    ExhibitionAnswer.objects.create(proposal=proposal, question=child, answer="Two chairs")
    parent.active = False
    parent.save()

    form = ExhibitorInfoForm(
        data={"name_0": "Acme"},
        event=call_event,
        instance=exhibitor,
        partner_type="exhibitor",
    )

    assert form.is_valid(), form.errors
    form.save_exhibition_questions(proposal)
    assert ExhibitionAnswer.objects.filter(proposal=proposal, question=child).exists()
