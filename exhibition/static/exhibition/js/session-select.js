function optionLabel(option) {
    return option.dataset.sessionLabel || option.textContent.trim()
}

function initSessionSelect(widget) {
    if (widget.dataset.sessionSelectInit === 'true') {
        return
    }
    widget.dataset.sessionSelectInit = 'true'

    var field = widget.querySelector('[data-session-field]')
    var panel = widget.querySelector('[data-session-panel]')
    var chips = widget.querySelector('[data-session-chips]')
    var placeholder = widget.querySelector('[data-session-placeholder]')
    var list = widget.querySelector('[data-session-list]')
    var search = widget.querySelector('[data-session-search]')
    var empty = widget.querySelector('[data-session-empty]')
    var clearAll = widget.querySelector('[data-session-deselect-all]')
    if (!field || !panel || !chips || !list) {
        return
    }

    var options = Array.prototype.slice.call(list.querySelectorAll('[data-session-option]'))

    function checkboxOf(option) {
        return option.querySelector('input[type="checkbox"]')
    }

    function chipKeyOf(option) {
        var box = checkboxOf(option)
        return box ? box.value : ''
    }

    function addChip(option) {
        var chip = document.createElement('span')
        chip.className = 'exhibition-session-chip'
        var text = document.createElement('span')
        text.className = 'exhibition-session-chip-label'
        text.textContent = optionLabel(option)
        var remove = document.createElement('button')
        remove.type = 'button'
        remove.className = 'exhibition-session-chip-remove'
        remove.setAttribute('aria-label', chips.dataset.removeLabel || 'Remove')
        remove.textContent = '×'
        remove.addEventListener('click', function (event) {
            event.preventDefault()
            event.stopPropagation()
            setSelected(option, false)
        })
        chip.appendChild(text)
        chip.appendChild(remove)
        chip.dataset.sessionChipFor = chipKeyOf(option)
        chips.appendChild(chip)
    }

    function removeChip(option) {
        var key = chipKeyOf(option)
        Array.prototype.slice.call(chips.children).forEach(function (chip) {
            if (chip.dataset.sessionChipFor === key) {
                chip.remove()
            }
        })
    }

    function setSelected(option, selected) {
        var box = checkboxOf(option)
        if (!box || box.checked === selected) {
            return
        }
        box.checked = selected
        option.setAttribute('aria-selected', selected ? 'true' : 'false')
        if (selected) {
            addChip(option)
        } else {
            removeChip(option)
        }
        applyFilter()
        syncPlaceholder()
    }

    function syncPlaceholder() {
        placeholder.hidden = chips.children.length > 0
    }

    function applyFilter() {
        var term = (search ? search.value : '').trim().toLowerCase()
        var visible = 0
        options.forEach(function (option) {
            var box = checkboxOf(option)
            var hide = (box && box.checked) || (term && optionLabel(option).toLowerCase().indexOf(term) === -1)
            option.hidden = !!hide
            if (!hide) {
                visible += 1
            }
        })
        if (empty) {
            empty.hidden = visible > 0
        }
    }

    function openPanel(open) {
        panel.hidden = !open
        field.setAttribute('aria-expanded', open ? 'true' : 'false')
        widget.classList.toggle('is-open', open)
        if (open && search) {
            search.focus()
        }
    }

    options.forEach(function (option) {
        var box = checkboxOf(option)
        if (box && box.checked) {
            addChip(option)
        }
        option.addEventListener('click', function (event) {
            if (event.target.closest('button')) {
                return
            }
            event.preventDefault()
            setSelected(option, true)
            if (search) {
                search.value = ''
                applyFilter()
                search.focus()
            }
        })
    })

    field.addEventListener('click', function () {
        openPanel(panel.hidden)
    })
    field.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            openPanel(panel.hidden)
        }
    })

    if (search) {
        search.addEventListener('input', applyFilter)
    }

    if (clearAll) {
        clearAll.addEventListener('click', function (event) {
            event.preventDefault()
            options.forEach(function (option) {
                setSelected(option, false)
            })
        })
    }

    document.addEventListener('click', function (event) {
        if (!widget.contains(event.target)) {
            openPanel(false)
        }
    })
    widget.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !panel.hidden) {
            openPanel(false)
            field.focus()
        }
    })

    applyFilter()
    syncPlaceholder()
}

function initAllSessionSelects() {
    document.querySelectorAll('[data-session-select]').forEach(initSessionSelect)
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAllSessionSelects)
} else {
    initAllSessionSelects()
}
