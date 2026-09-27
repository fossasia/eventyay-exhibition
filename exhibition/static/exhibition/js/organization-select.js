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
        var publishForm = document.getElementById('organization-publish')
        var publishSubmit = scope.querySelector('[data-organization-publish-submit]')
        var publishLabel = scope.querySelector('[data-organization-publish-label]')
        var publishIcon = scope.querySelector('[data-organization-publish-icon]')
        var publishModes = Array.prototype.slice.call(scope.querySelectorAll('[data-organization-publish-mode]'))
        var publishNeedsSelection = false
        var selectedLabel = scope.dataset.selectedLabel || 'selected'
        var baseHref = downloadLink ? downloadLink.getAttribute('href') : null
        var store = window.ExhibitionSelection.create({ scope: scope })

        function checkboxes() {
            return Array.prototype.slice.call(scope.querySelectorAll('[data-organization-checkbox]'))
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
            if (publishSubmit) {
                publishSubmit.disabled = publishNeedsSelection && total === 0
            }
            syncPublishForm()
        }

        publishModes.forEach(function (option) {
            option.addEventListener('click', function () {
                if (!publishSubmit) {
                    return
                }
                publishSubmit.value = option.dataset.organizationPublishMode
                publishNeedsSelection = option.dataset.needsSelection === '1'
                if (publishLabel) {
                    publishLabel.textContent = option.dataset.label
                }
                if (publishIcon) {
                    publishIcon.className = 'fa ' + option.dataset.icon
                }
                refreshSelection()
            })
        })

        function syncPublishForm() {
            if (!publishForm) {
                return
            }
            publishForm.querySelectorAll('input[name="selected"]').forEach(function (input) {
                input.remove()
            })
            store.ids().forEach(function (value) {
                var input = document.createElement('input')
                input.type = 'hidden'
                input.name = 'selected'
                input.value = value
                publishForm.appendChild(input)
            })
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
            if (event.target.hasAttribute('data-organization-checkbox')) {
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
                var searchParams = new URLSearchParams(window.location.search)
                searchParams.delete('pk')
                searchParams.set('download', 'yes')
                if (selected.length !== selectableIds().length) {
                    selected.forEach(function (value) {
                        searchParams.append('pk', value)
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
