(function () {
    "use strict";

    function init() {
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

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
