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

    function resetToggleStatus(statusWrapper) {
        statusWrapper.querySelectorAll('i').forEach(function (icon) {
            icon.classList.add('hidden')
        })
    }

    function setToggleStatus(statusWrapper, statusName) {
        resetToggleStatus(statusWrapper)
        var toggleSwitch = statusWrapper.querySelector('.toggle-switch')
        var statusIcon = statusWrapper.querySelector('.' + statusName)

        if (toggleSwitch) {
            toggleSwitch.classList.toggle('loading', statusName === 'working')
        }

        if (!statusIcon) {
            return
        }

        statusIcon.classList.remove('hidden')
        if (statusName !== 'working') {
            window.setTimeout(function () {
                statusIcon.classList.add('hidden')
            }, 3000)
        }
    }

    function handleFrontPageToggle(toggle) {
        var previousChecked = !toggle.checked
        var statusWrapper = toggle.closest('.sponsor-group-front-page-cell')
        var csrfToken = getCookie('eventyay_csrftoken') || getCookie('csrftoken')

        if (!statusWrapper) {
            return
        }

        toggle.disabled = true
        setToggleStatus(statusWrapper, 'working')

        fetch(toggle.dataset.url, {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrfToken,
            },
            credentials: 'include',
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error('Could not update front page visibility.')
                }
                return response.json()
            })
            .then(function (data) {
                toggle.checked = Boolean(data.show_on_front_page)
                setToggleStatus(statusWrapper, 'done')
            })
            .catch(function () {
                toggle.checked = previousChecked
                setToggleStatus(statusWrapper, 'fail')
            })
            .finally(function () {
                toggle.disabled = false
            })
    }

    function showAddGroupForm(addForm, showButton) {
        if (!addForm || !showButton) {
            return
        }
        addForm.classList.remove('hidden')
        showButton.classList.add('hidden')
    }

    function hideAddGroupForm(addForm, showButton) {
        if (!addForm || !showButton) {
            return
        }
        addForm.classList.add('hidden')
        showButton.classList.remove('hidden')
    }

    function closeAllEditorRows() {
        document.querySelectorAll('.sponsor-group-editor-row').forEach(function (row) {
            row.classList.add('hidden')
        })
        document.querySelectorAll('[data-group-editor-toggle]').forEach(function (button) {
            button.setAttribute('aria-expanded', 'false')
        })
    }

    function toggleEditorRow(targetId) {
        var target = document.getElementById(targetId)
        var toggle = document.querySelector('[data-group-editor-toggle="' + targetId + '"]')
        if (!target) {
            return
        }

        var isHidden = target.classList.contains('hidden')
        closeAllEditorRows()
        if (isHidden) {
            target.classList.remove('hidden')
            if (toggle) {
                toggle.setAttribute('aria-expanded', 'true')
            }
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        var showButton = document.getElementById('show-add-group-form')
        var addForm = document.getElementById('add-group-form')
        var cancelButton = document.getElementById('cancel-add-group-form')
        var frontPageToggles = document.querySelectorAll('.sponsor-group-front-page-toggle')
        var editToggles = document.querySelectorAll('[data-group-editor-toggle]')
        var closeEditorButtons = document.querySelectorAll('[data-group-editor-close]')

        if (showButton) {
            showButton.addEventListener('click', function () {
                closeAllEditorRows()
                showAddGroupForm(addForm, showButton)
            })
        }

        if (cancelButton) {
            cancelButton.addEventListener('click', function () {
                hideAddGroupForm(addForm, showButton)
            })
        }

        editToggles.forEach(function (toggle) {
            toggle.addEventListener('click', function () {
                hideAddGroupForm(addForm, showButton)
                toggleEditorRow(toggle.dataset.groupEditorToggle)
            })
        })

        closeEditorButtons.forEach(function (button) {
            button.addEventListener('click', function () {
                var target = document.getElementById(button.dataset.groupEditorClose)
                if (target) {
                    target.classList.add('hidden')
                }
                var toggle = document.querySelector(
                    '[data-group-editor-toggle="' + button.dataset.groupEditorClose + '"]'
                )
                if (toggle) {
                    toggle.setAttribute('aria-expanded', 'false')
                }
            })
        })

        frontPageToggles.forEach(function (toggle) {
            toggle.addEventListener('change', function () {
                handleFrontPageToggle(toggle)
            })
        })
    })
})()
