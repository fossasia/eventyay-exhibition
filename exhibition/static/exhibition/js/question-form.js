(function () {
    "use strict";

    function initAnswerOptions() {
        var variantField = document.querySelector("[data-question-variant]");
        var optionsGroup = document.querySelector("[data-question-options]");
        if (!variantField || !optionsGroup) {
            return;
        }

        var choiceVariants = (optionsGroup.dataset.choiceVariants || "").split(" ");

        function sync() {
            var isChoiceVariant = choiceVariants.indexOf(variantField.value) !== -1;
            optionsGroup.hidden = !isChoiceVariant;
            optionsGroup.classList.toggle("is-choice-variant", isChoiceVariant);
        }

        variantField.addEventListener("change", sync);
        sync();
    }

    function parseJson(raw, fallback) {
        if (!raw) {
            return fallback;
        }
        try {
            return JSON.parse(raw);
        } catch (error) {
            return fallback;
        }
    }

    function initDependency() {
        var group = document.querySelector("[data-question-dependency]");
        if (!group) {
            return;
        }
        var parentField = group.querySelector("select[name='dependency_question']");
        var valueField = group.querySelector("select[name='dependency_values']");
        if (!parentField || !valueField) {
            return;
        }

        var mapNode = group.querySelector("[data-question-dependency-map]");
        var selectedNode = group.querySelector("[data-question-dependency-selected]");
        var valueMap = parseJson(mapNode && mapNode.textContent, {});
        var saved = parseJson(selectedNode && selectedNode.textContent, []);

        function sync() {
            var previous = saved.length
                ? saved
                : Array.prototype.map.call(valueField.selectedOptions, function (option) {
                      return option.value;
                  });
            saved = [];

            valueField.innerHTML = "";
            // Without a parent field there is nothing to choose from, so the list stays empty.
            var choices = valueMap[parentField.value] || [];
            valueField.required = choices.length > 0;

            choices.forEach(function (choice) {
                var option = document.createElement("option");
                option.value = choice.value;
                option.textContent = choice.label;
                option.selected = previous.indexOf(choice.value) !== -1;
                valueField.appendChild(option);
            });
        }

        parentField.addEventListener("change", sync);
        sync();
    }

    function init() {
        initAnswerOptions();
        initDependency();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
