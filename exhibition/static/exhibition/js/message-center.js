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
            store = window.ExhibitionSelection.create();
        }
        return store;
    }

    /*
     * Re-tick the rows the user picked on other pages and show the total across
     * every page, not just the rows currently rendered.
     */
    function refreshSelection() {
        var table = selectionTable();
        if (!table) {
            return;
        }
        var boxes = rowBoxes(table);
        if (!boxes.length) {
            return;
        }
        var selection = selectionStore();
        boxes.forEach(function (box) {
            box.checked = selection.has(box.value);
        });
        var all = table.querySelector("[data-select-all]");
        if (all) {
            var onPage = boxes.filter(function (box) {
                return box.checked;
            }).length;
            all.checked = onPage === boxes.length;
            all.indeterminate = onPage > 0 && onPage < boxes.length;
        }
        var label = document.querySelector("[data-email-selected-count]");
        if (label) {
            var total = selection.size();
            label.textContent = total ? total + " " + (label.dataset.selectedLabel || "selected") : "";
        }
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
                // Rebuild the store against the URL we actually landed on, so a
                // filter change drops the selection but paging keeps it.
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
        var submitter = event.submitter;
        if (!submitter || submitter.name !== "op") {
            return;
        }
        // Bulk ops act on the whole cross-page selection: rows picked on other
        // pages have no checkbox here, so send them as hidden fields.
        var selection = selectionStore();
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
            form.appendChild(hidden);
        });
        selection.clear();
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
            var table = element.closest("table");
            var selection = selectionStore();
            rowBoxes(table).forEach(function (row) {
                row.checked = element.checked;
                selection.toggle(row.value, element.checked);
            });
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

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", refreshSelection);
    } else {
        refreshSelection();
    }
})();
