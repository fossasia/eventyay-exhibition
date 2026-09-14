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

        function selectableIds() {
            return (
                store.available() ||
                checkboxes().map(function (box) {
                    return box.value
                })
            )
        }

        function refreshSelection() {
            checkboxes().forEach(function (box) {
                box.checked = store.has(box.value)
            })
            var total = store.size()
            var selectable = selectableIds().length
            if (countLabel) {
                countLabel.textContent = total ? total + ' ' + selectedLabel : ''
            }
            if (selectAll) {
                selectAll.checked = selectable > 0 && total === selectable
                selectAll.indeterminate = total > 0 && total < selectable
            }
            if (downloadLink) {
                downloadLink.classList.toggle('disabled', total === 0)
                downloadLink.setAttribute('aria-disabled', total === 0 ? 'true' : 'false')
            }
        }

        if (selectAll) {
            selectAll.addEventListener('change', function () {
                if (selectAll.checked) {
                    store.addAll(selectableIds())
                } else {
                    store.clear()
                }
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
                if (selected.length === selectableIds().length) {
                    var filtered = new URLSearchParams(window.location.search)
                    filtered.set('download', 'yes')
                    window.location.href = window.location.pathname + '?' + filtered.toString()
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
