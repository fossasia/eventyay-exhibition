(function () {
    function init() {
        var scope = document.querySelector('[data-partner-select-scope]')
        if (!scope) {
            return
        }

        if (window.jQuery) {
            $(scope).find('[data-toggle="tooltip"]').tooltip()
        }

        var selectAll = scope.querySelector('[data-partner-select-all]')
        var countLabel = scope.querySelector('[data-partner-selected-count]')
        var downloadLink = scope.querySelector('[data-partner-download-link]')
        var selectedLabel = scope.dataset.selectedLabel || 'selected'
        var baseHref = downloadLink ? downloadLink.getAttribute('href') : null

        function checkboxes() {
            return Array.prototype.slice.call(scope.querySelectorAll('[data-partner-checkbox]'))
        }

        function selectedValues() {
            return checkboxes()
                .filter(function (box) {
                    return box.checked
                })
                .map(function (box) {
                    return box.value
                })
        }

        function refreshSelection() {
            var boxes = checkboxes()
            var selected = selectedValues()
            var isSelectAllPages = scope.dataset.selectAllPages === "true";
            if (countLabel) {
                if (isSelectAllPages && scope.dataset.totalCount) {
                    countLabel.textContent = scope.dataset.totalCount + ' ' + selectedLabel
                } else {
                    countLabel.textContent = selected.length ? selected.length + ' ' + selectedLabel : ''
                }
            }
            if (selectAll) {
                selectAll.checked = boxes.length > 0 && selected.length === boxes.length
                selectAll.indeterminate = selected.length > 0 && selected.length < boxes.length
            }
            if (downloadLink) {
                downloadLink.classList.toggle('disabled', selected.length === 0)
                downloadLink.setAttribute('aria-disabled', selected.length === 0 ? 'true' : 'false')
            }
        }

        if (selectAll) {
            selectAll.addEventListener('change', function () {
                scope.dataset.selectAllPages = selectAll.checked ? "true" : "false"
                checkboxes().forEach(function (box) {
                    box.checked = selectAll.checked
                })
                refreshSelection()
            })
        }

        scope.addEventListener('change', function (event) {
            if (event.target.hasAttribute('data-partner-checkbox')) {
                scope.dataset.selectAllPages = "false"
                refreshSelection()
            }
        })

        if (downloadLink && baseHref) {
            downloadLink.addEventListener('click', function (event) {
                event.preventDefault()
                var selected = selectedValues()
                var isSelectAllPages = scope.dataset.selectAllPages === "true";
                if (!selected.length && !isSelectAllPages) {
                    return
                }
                var searchParams = new URLSearchParams(window.location.search);
                searchParams.set('download', 'yes');
                if (!isSelectAllPages) {
                    selected.forEach(function (value) {
                        searchParams.append('pk', value);
                    })
                }
                window.location.href = window.location.pathname + '?' + searchParams.toString()
            })
        }

        refreshSelection()
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init)
    } else {
        init()
    }
    document.addEventListener('eventyay:ajax-results-replaced', init)
})()
