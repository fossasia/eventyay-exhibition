(function () {
    "use strict";

    function container() {
        return document.getElementById("email-list");
    }

    function load(url, push) {
        var target = container();
        if (!target) {
            return;
        }
        target.classList.add("email-list-loading");
        fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" }, credentials: "same-origin" })
            .then(function (response) {
                return response.text();
            })
            .then(function (html) {
                target.innerHTML = html;
                target.classList.remove("email-list-loading");
                target.dispatchEvent(
                    new CustomEvent("eventyay:ajax-results-replaced", { bubbles: true, detail: { container: target } })
                );
                if (push) {
                    window.history.pushState({ emailList: true }, "", url);
                }
            })
            .catch(function () {
                target.classList.remove("email-list-loading");
                window.location.href = url;
            });
    }

    function onSubmit(event) {
        var form = event.target;
        if (!form.matches || !form.matches("#email-list form[data-ajax]")) {
            return;
        }
        event.preventDefault();
        var params = new URLSearchParams(new FormData(form)).toString();
        load(window.location.pathname + "?" + params, true);
    }

    function onClick(event) {
        var link = event.target.closest("#email-list a[data-ajax], #email-list .btn-clear-filter");
        if (!link) {
            return;
        }
        event.preventDefault();
        load(link.href, true);
    }

    function onChange(event) {
        var element = event.target;
        if (!element.matches || !element.matches("[data-select-all], [data-select-row]")) {
            return;
        }

        var table = element.closest("table");
        if (!table) {
            return;
        }

        var form = element.closest("form");
        var boxes = Array.prototype.slice.call(table.querySelectorAll("[data-select-row]"));

        if (element.matches("[data-select-all]")) {
            boxes.forEach(function (row) {
                row.checked = element.checked;
            });
            if (form) {
                form.dataset.selectAllPages = element.checked ? "true" : "false";
            }
        } else if (element.matches("[data-select-row]")) {
            if (form) {
                form.dataset.selectAllPages = "false";
            }
            var all = table.querySelector("[data-select-all]");
            if (all) {
                all.checked = boxes.length > 0 && boxes.every(function (box) {
                    return box.checked;
                });
                all.indeterminate = !all.checked && boxes.some(function (box) {
                    return box.checked;
                });
            }
        }

        var toolbar = form ? form.querySelector(".email-bulk-toolbar") : null;
        var countLabel = toolbar ? toolbar.querySelector("[data-selected-count]") : null;

        if (countLabel) {
            var checkedCount = boxes.filter(function (b) { return b.checked; }).length;
            var selectedLabel = toolbar.dataset.selectedLabel || "selected";
            var isSelectAllPages = form && form.dataset.selectAllPages === "true";
            var totalCount = toolbar ? toolbar.dataset.totalCount : null;

            if (isSelectAllPages && totalCount) {
                countLabel.textContent = totalCount + " " + selectedLabel;
            } else if (checkedCount > 0) {
                countLabel.textContent = checkedCount + " " + selectedLabel;
            } else {
                countLabel.textContent = "";
            }
        }
    }

    function onBulkButtonClick(event) {
        var btn = event.target.closest('button[type="submit"][name="op"]');
        if (!btn) {
            return;
        }

        var form = btn.closest("form");
        if (!form || form.dataset.selectAllPages !== "true") {
            return;
        }

        if (btn.value === "send" || btn.value === "discard") {
            var originalValue = btn.value;
            btn.value = originalValue + "_all";
            setTimeout(function () {
                btn.value = originalValue;
            }, 0);
        }
    }

    document.addEventListener("submit", onSubmit);
    document.addEventListener("click", onClick);
    document.addEventListener("click", onBulkButtonClick);
    document.addEventListener("change", onChange);

    window.addEventListener("popstate", function () {
        if (container()) {
            load(window.location.href, false);
        }
    });
})();
