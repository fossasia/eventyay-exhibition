document.addEventListener("DOMContentLoaded", function () {
    var selector = document.querySelector("select[name='content_locale'][data-rtl-locales]");
    if (!selector) {
        return;
    }

    var rtlLocales = selector.dataset.rtlLocales
        .split(",")
        .map(function (code) {
            return code.trim();
        })
        .filter(Boolean);

    function applyDirection() {
        var direction = rtlLocales.indexOf(selector.value) === -1 ? "ltr" : "rtl";
        document.querySelectorAll("[data-content-text]").forEach(function (field) {
            field.setAttribute("dir", direction);
        });
    }

    selector.addEventListener("change", applyDirection);
});
