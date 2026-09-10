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
        var store = window.ExhibitionSelection.create({ scope: scope })

        function checkboxes() {
            return Array.prototype.slice.call(scope.querySelectorAll('[data-partner-checkbox]'))
        }

        function restoreSelection() {
            checkboxes().forEach(function (box) {
                box.checked = store.has(box.value)
            })
        }

        function refreshSelection() {
            var boxes = checkboxes()
            var onPage = boxes.filter(function (box) {
                return box.checked
            }).length
            // The count covers every page, not just the rows currently rendered.
            var total = store.size()
            if (countLabel) {
                countLabel.textContent = total ? total + ' ' + selectedLabel : ''
            }
            if (selectAll) {
                selectAll.checked = boxes.length > 0 && onPage === boxes.length
                selectAll.indeterminate = onPage > 0 && onPage < boxes.length
            }
            if (downloadLink) {
                downloadLink.classList.toggle('disabled', total === 0)
                downloadLink.setAttribute('aria-disabled', total === 0 ? 'true' : 'false')
            }
        }

        if (selectAll) {
            selectAll.addEventListener('change', function () {
                checkboxes().forEach(function (box) {
                    box.checked = selectAll.checked
                    store.toggle(box.value, selectAll.checked)
                })
                refreshSelection()
            })
        }

        scope.addEventListener('change', function (event) {
            if (event.target.hasAttribute('data-partner-checkbox')) {
                store.toggle(event.target.value, event.target.checked)
                refreshSelection()
            }
        })

        if (downloadLink && baseHref) {
            downloadLink.addEventListener('click', function (event) {
                event.preventDefault()
                var selected = store.ids()
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

        restoreSelection()
        refreshSelection()
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init)
    } else {
        init()
    }
    document.addEventListener('eventyay:ajax-results-replaced', init)
})()
