(function () {
    function getCookie(name) {
        var cookieValue = null
        if (document.cookie && document.cookie !== '') {
            var cookies = document.cookie.split(';')
            for (var index = 0; index < cookies.length; index++) {
                var cookie = cookies[index].trim()
                if (cookie.substring(0, name.length + 1) === name + '=') {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1))
                    break
                }
            }
        }
        return cookieValue
    }

    function init() {
        var container = document.querySelector('[data-request-list]')
        if (!container) {
            return
        }

        var actionUrl = container.dataset.requestActionsUrl
        var csrfToken = getCookie('eventyay_csrftoken') || getCookie('csrftoken')
        var feedback = document.querySelector('[data-request-feedback]')
        var i18nEl = document.querySelector('[data-request-i18n]')
        var i18n = i18nEl ? i18nEl.dataset : {}

        var selectAll = container.querySelector('[data-request-select-all]')
        var bulkButtons = container.querySelectorAll('[data-request-bulk]')
        var countLabel = container.querySelector('[data-request-selected-count]')
        var bulkBar = container.querySelector('.request-bulk-bar')
        var bulkHint = container.querySelector('[data-request-bulk-hint]')
        var bulkReasons = bulkBar ? bulkBar.dataset : {}
        var selectAllAcrossPages = false

        function allResultsSelected() {
            return selectAllAcrossPages && selectedBoxes().length > 0
        }

        function filterParams() {
            var params = new URLSearchParams(window.location.search)
            params.delete('page')
            params.delete('page_size')
            return params
        }

        var actionConfig = {
            approve: { icon: 'fa-check', cls: 'request-action-approve', variant: 'btn-success', label: i18n.labelApprove },
            reject: { icon: 'fa-times', cls: 'request-action-reject', variant: 'btn-danger', label: i18n.labelReject },
            withdraw: { icon: 'fa-undo', cls: 'request-action-withdraw', variant: '', label: i18n.labelWithdraw },
            reopen: { icon: 'fa-inbox', cls: 'request-action-reopen', variant: '', label: i18n.labelReopen },
        }

        function checkboxes() {
            return Array.prototype.slice.call(container.querySelectorAll('[data-request-checkbox]'))
        }

        function selectedBoxes() {
            return checkboxes().filter(function (box) {
                return box.checked
            })
        }

        function eligibleFor(action) {
            return selectedBoxes().filter(function (box) {
                var actions = (box.dataset.requestBulkActions || '').split(' ')
                return actions.indexOf(action) !== -1
            })
        }

        function selectedCodes(action) {
            return eligibleFor(action).map(function (box) {
                return box.value
            })
        }

        function capitalize(word) {
            return word.charAt(0).toUpperCase() + word.slice(1)
        }

        function refreshSelection() {
            var boxes = checkboxes()
            var count = selectedBoxes().length
            var pageFullySelected = boxes.length > 0 && count === boxes.length
            var acrossPages = allResultsSelected()
            var hints = []
            bulkButtons.forEach(function (button) {
                var action = button.dataset.requestBulk
                var eligible = acrossPages ? count : eligibleFor(action).length
                button.disabled = eligible === 0
                if (!count) {
                    button.title = bulkReasons.requestReasonNone || ''
                } else if (!eligible) {
                    button.title = bulkReasons['requestReason' + capitalize(action)] || ''
                } else {
                    button.removeAttribute('title')
                    var skipped = acrossPages ? 0 : count - eligible
                    var template = bulkReasons['requestSkip' + capitalize(action)]
                    if (skipped > 0 && template) {
                        hints.push(template.replace('{count}', skipped))
                    }
                }
            })
            if (bulkHint) {
                bulkHint.textContent = hints.join(' ')
                bulkHint.hidden = hints.length === 0
            }
            if (countLabel) {
                var shown = acrossPages ? bulkReasons.requestTotal : count
                countLabel.textContent = count ? shown + ' ' + (i18n.selected || '') : ''
            }
            if (selectAll) {
                selectAll.checked = pageFullySelected
                selectAll.indeterminate = count > 0 && !pageFullySelected
            }
        }

        function showFeedback(ok, message) {
            if (!feedback) {
                return
            }
            feedback.innerHTML = ''
            var alert = document.createElement('div')
            alert.className = 'alert ' + (ok ? 'alert-success' : 'alert-danger')
            alert.textContent = message
            feedback.appendChild(alert)
        }

        function buildActionButton(action, code) {
            var config = actionConfig[action]
            if (!config) {
                return null
            }
            var button = document.createElement('button')
            button.type = 'button'
            button.className = 'btn btn-sm request-action-btn ' + config.cls + (config.variant ? ' ' + config.variant : '')
            button.setAttribute('data-request-action', action)
            button.setAttribute('data-request-code', code)
            button.setAttribute('data-toggle', 'tooltip')
            if (config.label) {
                button.title = config.label
            }
            var icon = document.createElement('i')
            icon.className = 'fa ' + config.icon
            button.appendChild(icon)
            return button
        }

        function rebuildActions(row, result) {
            var actionsCell = row.querySelector('[data-request-actions-cell]')
            if (!actionsCell) {
                return
            }
            actionsCell.querySelectorAll('[data-request-action]').forEach(function (button) {
                button.remove()
            })
            var viewLink = actionsCell.querySelector('a')
            ;(result.actions || []).forEach(function (action) {
                var button = buildActionButton(action, result.code)
                if (button) {
                    actionsCell.insertBefore(button, viewLink)
                }
            })
            if (window.jQuery) {
                $(actionsCell).find('[data-toggle="tooltip"]').tooltip()
            }
        }

        function rebuildCheckbox(row, result) {
            var box = row.querySelector('[data-request-checkbox]')
            if (!box) {
                return
            }
            box.checked = false
            box.setAttribute('data-request-bulk-actions', (result.bulk_actions || []).join(' '))
        }

        function updateRow(result) {
            var row = container.querySelector('[data-request-row="' + result.code + '"]')
            if (!row) {
                return
            }
            var stateCell = row.querySelector('[data-request-state]')
            if (stateCell) {
                stateCell.textContent = result.state_display
            }
            rebuildActions(row, result)
            rebuildCheckbox(row, result)
        }

        function setBusy(busy) {
            bulkButtons.forEach(function (button) {
                var pending = allResultsSelected()
                    ? selectedBoxes().length === 0
                    : selectedCodes(button.dataset.requestBulk).length === 0
                button.disabled = busy || pending
            })
            container.querySelectorAll('[data-request-action]').forEach(function (button) {
                button.disabled = busy
            })
        }

        function submitAction(action, codes, acrossPages) {
            if (!acrossPages && !codes.length) {
                return
            }
            setBusy(true)
            var body = new URLSearchParams()
            body.append('action', action)
            if (acrossPages) {
                body.append('all', '1')
                filterParams().forEach(function (value, key) {
                    body.append(key, value)
                })
            } else {
                codes.forEach(function (code) {
                    body.append('exhibition_request', code)
                })
            }
            fetch(actionUrl, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrfToken,
                    'X-Requested-With': 'XMLHttpRequest',
                },
                credentials: 'include',
                body: body,
            })
                .then(function (response) {
                    return response.json().then(function (data) {
                        return { ok: response.ok, data: data }
                    })
                })
                .then(function (payload) {
                    if (payload.ok && payload.data.ok) {
                        if (payload.data.reload) {
                            refreshResults(payload.data.message)
                            return
                        }
                        payload.data.results.forEach(updateRow)
                        showFeedback(true, payload.data.message)
                    } else {
                        showFeedback(false, payload.data.message || i18n.error)
                    }
                })
                .catch(function () {
                    showFeedback(false, i18n.error)
                })
                .finally(function () {
                    setBusy(false)
                    refreshSelection()
                })
        }

        function confirmFor(action, isBulk, acrossPages) {
            if (isBulk) {
                if (action === 'approve') {
                    return acrossPages ? i18n.confirmApproveAll : i18n.confirmApprove
                }
                if (action === 'reject') {
                    return acrossPages ? i18n.confirmRejectAll : i18n.confirmReject
                }
                return null
            }
            if (action === 'reject') {
                return i18n.confirmRejectOne
            }
            if (action === 'withdraw') {
                return i18n.confirmWithdrawOne
            }
            if (action === 'reopen') {
                return i18n.confirmReopenOne
            }
            return null
        }

        function confirmClassFor(action) {
            if (action === 'approve') {
                return 'btn-success'
            }
            if (action === 'reopen') {
                return 'btn-primary'
            }
            return 'btn-danger'
        }

        function requestConfirmation(action, message) {
            if (!message) {
                return Promise.resolve(true)
            }
            var options = {
                message: message,
                title: i18n.confirmTitle,
                confirmLabel: i18n.confirmLabel,
                cancelLabel: i18n.cancelLabel,
                confirmClass: confirmClassFor(action),
            }
            if (typeof window.showConfirmDialog === 'function') {
                return window.showConfirmDialog(options)
            }
            return Promise.resolve(window.confirm(message))
        }

        container.addEventListener('click', function (event) {
            var button = event.target.closest('[data-request-action]')
            if (!button) {
                return
            }
            var action = button.dataset.requestAction
            var code = button.dataset.requestCode
            requestConfirmation(action, confirmFor(action, false)).then(function (confirmed) {
                if (confirmed) {
                    submitAction(action, [code])
                }
            })
        })

        bulkButtons.forEach(function (button) {
            button.addEventListener('click', function () {
                var action = button.dataset.requestBulk
                var acrossPages = allResultsSelected()
                var codes = selectedCodes(action)
                if (!acrossPages && !codes.length) {
                    return
                }
                requestConfirmation(action, confirmFor(action, true, acrossPages)).then(function (confirmed) {
                    if (confirmed) {
                        submitAction(action, codes, acrossPages)
                    }
                })
            })
        })

        if (selectAll) {
            selectAll.addEventListener('change', function () {
                selectAllAcrossPages = selectAll.checked
                checkboxes().forEach(function (box) {
                    box.checked = selectAll.checked
                })
                refreshSelection()
            })
        }

        container.addEventListener('change', function (event) {
            if (event.target.matches('[data-request-checkbox]')) {
                selectAllAcrossPages = false
                refreshSelection()
            }
        })

        refreshSelection()
    }

    function announce(message) {
        var feedback = document.querySelector('[data-request-feedback]')
        if (!feedback || !message) {
            return
        }
        feedback.innerHTML = ''
        var alert = document.createElement('div')
        alert.className = 'alert alert-success'
        alert.textContent = message
        feedback.appendChild(alert)
    }

    function refreshResults(message) {
        var region = document.querySelector('[data-ajax-results-region]')
        if (!region) {
            window.location.reload()
            return
        }
        region.style.opacity = '0.5'
        fetch(window.location.href, {
            credentials: 'same-origin',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
        })
            .then(function (response) {
                return response.text()
            })
            .then(function (html) {
                var fresh = new DOMParser()
                    .parseFromString(html, 'text/html')
                    .querySelector('[data-ajax-results-region]')
                if (!fresh) {
                    window.location.reload()
                    return
                }
                region.replaceWith(fresh)
                fresh.dispatchEvent(
                    new CustomEvent('eventyay:ajax-results-replaced', { bubbles: true, detail: { container: fresh } })
                )
                announce(message)
            })
            .catch(function () {
                window.location.reload()
            })
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init)
    } else {
        init()
    }
    document.addEventListener('eventyay:ajax-results-replaced', init)
})()
