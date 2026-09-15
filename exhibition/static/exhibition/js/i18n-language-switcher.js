(function () {
    var INPUT_SELECTOR = 'input[lang], textarea[lang]'

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
        return units
    }

    function hasContent(unit) {
        return String(unit.input.value || '').trim().length > 0
    }

    function isOverLength(unit) {
        var max = parseInt(unit.input.getAttribute('maxlength'), 10)
        return max > 0 && String(unit.input.value || '').length > max
    }

    function localesWithErrors(units) {
        var flagged = {}
        var groups = new Map()
        units.forEach(function (unit) {
            if (!unit.group.closest('.has-error')) {
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
        var units = collectUnits(form)
        var available = {}
        units.forEach(function (unit) {
            available[unit.locale] = true
        })
        if (Object.keys(available).length < 2) {
            return
        }
        root.dataset.i18nSwitcherInit = 'true'

        var select = root.querySelector('[data-i18n-select]')
        var field = root.querySelector('[data-i18n-field]')
        var panel = root.querySelector('[data-i18n-panel]')
        var badges = Array.prototype.slice.call(root.querySelectorAll('[data-i18n-badge]'))
        var options = Array.prototype.slice.call(root.querySelectorAll('[data-i18n-option]'))
        if (!select || !field || !panel) {
            return
        }

        options.forEach(function (option) {
            option.hidden = !available[option.dataset.locale]
        })
        badges = badges.filter(function (badge) {
            return available[badge.dataset.locale]
        })
        options = options.filter(function (option) {
            return available[option.dataset.locale]
        })

        var flagged = localesWithErrors(units)
        var current = null

        function refresh() {
            var content = {}
            units.forEach(function (unit) {
                if (hasContent(unit)) {
                    content[unit.locale] = true
                }
            })
            badges.forEach(function (badge) {
                var locale = badge.dataset.locale
                var active = locale === current
                badge.hidden = !(content[locale] || flagged[locale] || active)
                badge.classList.toggle('is-active', active)
                badge.classList.toggle('has-error', !!flagged[locale])
            })
            options.forEach(function (option) {
                var locale = option.dataset.locale
                var active = locale === current
                option.classList.toggle('is-active', active)
                option.classList.toggle('has-content', !!content[locale])
                option.classList.toggle('has-error', !!flagged[locale])
                option.setAttribute('aria-selected', active ? 'true' : 'false')
            })
        }

        function apply(locale) {
            current = locale
            units.forEach(function (unit) {
                unit.el.classList.toggle('exhibition-lang-hidden', unit.locale !== locale)
            })
            refresh()
        }

        function openPanel(open) {
            panel.hidden = !open
            field.setAttribute('aria-expanded', open ? 'true' : 'false')
            select.classList.toggle('is-open', open)
        }

        field.addEventListener('click', function (event) {
            if (event.target.closest('[data-i18n-badge]')) {
                return
            }
            openPanel(panel.hidden)
        })
        field.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                openPanel(panel.hidden)
            }
        })
        badges.forEach(function (badge) {
            badge.addEventListener('click', function (event) {
                event.preventDefault()
                event.stopPropagation()
                apply(badge.dataset.locale)
                openPanel(false)
            })
        })
        options.forEach(function (option) {
            option.addEventListener('click', function () {
                apply(option.dataset.locale)
                openPanel(false)
                field.focus()
            })
        })
        document.addEventListener('click', function (event) {
            if (!root.contains(event.target)) {
                openPanel(false)
            }
        })
        root.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && !panel.hidden) {
                openPanel(false)
                field.focus()
            }
        })
        form.addEventListener('input', refresh)

        function containsI18nGroup(node) {
            return node.nodeType === 1 && (node.matches('.i18n-form-group') || !!node.querySelector('.i18n-form-group'))
        }

        new MutationObserver(function (mutations) {
            var added = mutations.some(function (mutation) {
                return Array.prototype.some.call(mutation.addedNodes, containsI18nGroup)
            })
            if (added) {
                units = collectUnits(form)
                apply(current)
            }
        }).observe(form, { childList: true, subtree: true })

        var order = options.map(function (option) {
            return option.dataset.locale
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
