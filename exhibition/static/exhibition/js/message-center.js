(function () {
    "use strict";

    var store = null;

    function container() {
        return document.getElementById("email-list");
    }

    function selectionTable() {
        var target = container();
        return target ? target.querySelector("table") : null;
    }

    function rowBoxes(table) {
        return Array.prototype.slice.call(table.querySelectorAll("[data-select-row]"));
    }

    function selectionStore() {
        if (!store) {
            store = window.ExhibitionSelection.create({ scope: selectionTable() });
        }
        return store;
    }

    function selectionLimit() {
        var warning = document.querySelector("[data-email-selection-limit]");
        var limit = warning ? parseInt(warning.dataset.emailSelectionLimit, 10) : NaN;
        return isNaN(limit) ? Infinity : limit;
    }

    function selectableIds(table) {
        return (
            selectionStore().available() ||
            rowBoxes(table).map(function (box) {
                return box.value;
            })
        );
    }

    function refreshSelection() {
        var table = selectionTable();
        if (!table) {
            return;
        }
        var selection = selectionStore();
        var boxes = rowBoxes(table);
        boxes.forEach(function (box) {
            box.checked = selection.has(box.value);
        });
        var total = selection.size();
        var all = table.querySelector("[data-select-all]");
        if (all) {
            var selectable = selectableIds(table).length;
            all.checked = selectable > 0 && total === selectable;
            all.indeterminate = total > 0 && total < selectable;
        }
        var label = document.querySelector("[data-email-selected-count]");
        if (label) {
            label.textContent = total ? total + " " + (label.dataset.selectedLabel || "selected") : "";
        }
        var warning = document.querySelector("[data-email-selection-limit]");
        if (warning) {
            warning.hidden = total <= selectionLimit();
        }
    }

    function dropCarried(root) {
        Array.prototype.slice.call(root.querySelectorAll("[data-selection-carried]")).forEach(function (input) {
            input.remove();
        });
    }

    function consumeBulkResult() {
        var params = new URLSearchParams(window.location.search);
        if (!params.has("bulk")) {
            return;
        }
        selectionStore().clear();
        params.delete("bulk");
        var query = params.toString();
        window.history.replaceState({}, "", window.location.pathname + (query ? "?" + query : ""));
        store = null;
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
                if (push) {
                    window.history.pushState({ emailList: true }, "", url);
                }
                store = null;
                refreshSelection();
                target.dispatchEvent(
                    new CustomEvent("eventyay:ajax-results-replaced", { bubbles: true, detail: { container: target } })
                );
            })
            .catch(function () {
                target.classList.remove("email-list-loading");
                window.location.href = url;
            });
    }

    function onSubmit(event) {
        var form = event.target;
        if (!form.matches || !form.matches("#email-list form")) {
            return;
        }
        if (form.matches("[data-ajax]")) {
            var params = new URLSearchParams(new FormData(form)).toString();
            event.preventDefault();
            load(window.location.pathname + "?" + params, true);
            return;
        }
        dropCarried(form);
        var submitter = event.submitter;
        if (!submitter || submitter.name !== "op" || (submitter.value !== "send" && submitter.value !== "discard")) {
            return;
        }
        var selection = selectionStore();
        if (selection.size() > selectionLimit()) {
            event.preventDefault();
            refreshSelection();
            return;
        }
        var onPage = {};
        rowBoxes(form).forEach(function (box) {
            onPage[box.value] = true;
        });
        selection.ids().forEach(function (id) {
            if (onPage[id]) {
                return;
            }
            var hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = "selected";
            hidden.value = id;
            hidden.setAttribute("data-selection-carried", "");
            form.appendChild(hidden);
        });
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
        if (!element.matches) {
            return;
        }
        if (element.matches("[data-select-all]")) {
            var selection = selectionStore();
            if (element.checked) {
                selection.addAll(selectableIds(element.closest("table")));
            } else {
                selection.clear();
            }
            refreshSelection();
        } else if (element.matches("[data-select-row]")) {
            selectionStore().toggle(element.value, element.checked);
            refreshSelection();
        }
    }

    document.addEventListener("submit", onSubmit);
    document.addEventListener("click", onClick);
    document.addEventListener("change", onChange);
    window.addEventListener("popstate", function () {
        if (container()) {
            load(window.location.href, false);
        }
    });
    window.addEventListener("pageshow", function (event) {
        var target = container();
        if (!event.persisted || !target) {
            return;
        }
        dropCarried(target);
        store = null;
        refreshSelection();
    });

    function onReady() {
        consumeBulkResult();
        refreshSelection();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", onReady);
    } else {
        onReady();
    }
})();
