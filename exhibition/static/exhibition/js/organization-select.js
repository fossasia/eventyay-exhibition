(function () {
    function init() {
        var scope = document.querySelector('[data-organization-select-scope]')
        if (!scope) {
            return
        }

        if (window.jQuery) {
            $(scope).find('[data-toggle="tooltip"]').tooltip()
        }

        var selectAll = scope.querySelector('[data-organization-select-all]')
        var countLabel = scope.querySelector('[data-organization-selected-count]')
        var downloadLink = scope.querySelector('[data-organization-download-link]')
        var selectedLabel = scope.dataset.selectedLabel || 'selected'
        var baseHref = downloadLink ? downloadLink.getAttribute('href') : null

        function checkboxes() {
            return Array.prototype.slice.call(scope.querySelectorAll('[data-organization-checkbox]'))
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
            if (countLabel) {
                countLabel.textContent = selected.length ? selected.length + ' ' + selectedLabel : ''
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
                checkboxes().forEach(function (box) {
                    box.checked = selectAll.checked
                })
                refreshSelection()
            })
        }

        scope.addEventListener('change', function (event) {
            if (event.target.hasAttribute('data-organization-checkbox')) {
                refreshSelection()
            }
        })

        if (downloadLink && baseHref) {
            downloadLink.addEventListener('click', function (event) {
                event.preventDefault()
                var selected = selectedValues()
                if (!selected.length) {
                    return
                }
                var params = selected
                    .map(function (value) {
                        return 'pk=' + encodeURIComponent(value)
                    })
                    .join('&')
                window.location.href = baseHref + '&' + params
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
