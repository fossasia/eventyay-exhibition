import pytest
from django.test import RequestFactory

from exhibition.forms import ExhibitionQuestionOptionFormSet
from exhibition.models import ExhibitionQuestion, ExhibitionQuestionOption, ExhibitionQuestionVariant
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
                f"options-{index}-answer_0": option["answer"],
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
