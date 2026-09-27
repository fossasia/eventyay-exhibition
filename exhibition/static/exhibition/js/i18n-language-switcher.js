(function () {
    var INPUT_SELECTOR = 'input[lang], textarea[lang]'
    var PREVIEW_SELECTOR = '.mail-preview[lang]'

    function unitElement(input) {
        return input.closest('.tiptap-wrapper') || input
    }

    function collectUnits(form) {
        var units = []
        form.querySelectorAll('.i18n-form-group').forEach(function (group) {
            group.querySelectorAll(INPUT_SELECTOR).forEach(function (input) {
                units.push({ locale: input.lang, input: input, el: unitElement(input), group: group })
            })
        })
        form.querySelectorAll(PREVIEW_SELECTOR).forEach(function (pane) {
            units.push({ locale: pane.lang, input: null, el: pane, group: null })
        })
        return units
    }

    function hasContent(unit) {
        return !!unit.input && String(unit.input.value || '').trim().length > 0
    }

    function isOverLength(unit) {
        var max = parseInt(unit.input.getAttribute('maxlength'), 10)
        return max > 0 && String(unit.input.value || '').length > max
    }

    function localesWithErrors(units) {
        var flagged = {}
        var groups = new Map()
        units.forEach(function (unit) {
            if (!unit.group || !unit.group.closest('.has-error')) {
                return
            }
            if (!groups.has(unit.group)) {
                groups.set(unit.group, [])
            }
            groups.get(unit.group).push(unit)
        })
        groups.forEach(function (groupUnits) {
            var empty = groupUnits.filter(function (unit) {
                return !hasContent(unit)
            })
            var overLength = groupUnits.filter(isOverLength)
            var culprits = empty.length ? empty : overLength.length ? overLength : groupUnits
            culprits.forEach(function (unit) {
                flagged[unit.locale] = true
            })
        })
        return flagged
    }

    function initSwitcher(root) {
        if (root.dataset.i18nSwitcherInit === 'true') {
            return
        }
        var form = root.closest('form')
        if (!form) {
            return
        }
        var scope = root.closest('[data-i18n-scope]') || form
        var units = collectUnits(scope)
        var available = {}
        units.forEach(function (unit) {
            if (unit.input) {
                available[unit.locale] = true
            }
        })
        if (Object.keys(available).length < 2) {
            return
        }
        root.dataset.i18nSwitcherInit = 'true'

        var chips = Array.prototype.slice.call(root.querySelectorAll('[data-i18n-chip]')).filter(function (chip) {
            var offered = !!available[chip.dataset.locale]
            chip.hidden = !offered
            return offered
        })
        var flagged = localesWithErrors(units)
        var current = null

        function refresh() {
            chips.forEach(function (chip) {
                var locale = chip.dataset.locale
                var active = locale === current
                chip.classList.toggle('is-active', active)
                chip.classList.toggle('has-error', !!flagged[locale])
                chip.setAttribute('aria-selected', active ? 'true' : 'false')
                chip.tabIndex = active ? 0 : -1
            })
        }

        function apply(locale) {
            current = locale
            units.forEach(function (unit) {
                unit.el.classList.toggle('exhibition-lang-hidden', unit.locale !== locale)
            })
            refresh()
        }

        chips.forEach(function (chip, index) {
            chip.addEventListener('click', function () {
                apply(chip.dataset.locale)
            })
            chip.addEventListener('keydown', function (event) {
                var offset = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
                if (!offset) {
                    return
                }
                event.preventDefault()
                var next = chips[(index + offset + chips.length) % chips.length]
                apply(next.dataset.locale)
                next.focus()
            })
        })
        function containsI18nGroup(node) {
            return (
                node.nodeType === 1 &&
                (node.matches('.i18n-form-group, .mail-preview') || !!node.querySelector('.i18n-form-group, .mail-preview'))
            )
        }

        new MutationObserver(function (mutations) {
            var added = mutations.some(function (mutation) {
                return Array.prototype.some.call(mutation.addedNodes, containsI18nGroup)
            })
            if (added) {
                units = collectUnits(scope)
                flagged = localesWithErrors(units)
                apply(current)
            }
        }).observe(scope, { childList: true, subtree: true })

        var order = chips.map(function (chip) {
            return chip.dataset.locale
        })
        var initial =
            order.filter(function (locale) {
                return flagged[locale]
            })[0] ||
            order.filter(function (locale) {
                return units.some(function (unit) {
                    return unit.locale === locale && hasContent(unit)
                })
            })[0] ||
            order[0]

        apply(initial)
        root.hidden = false
    }

    function initAll() {
        document.querySelectorAll('[data-i18n-switcher]').forEach(initSwitcher)
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAll)
    } else {
        initAll()
    }
})()
