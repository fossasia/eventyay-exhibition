/*
 * Cross-page selection for the paginated exhibition lists.
 *
 * Checkbox state used to live only in the DOM, so paginating a list threw the
 * selection away. This keeps it in sessionStorage instead, keyed by the list's
 * path, so it survives pagination, sorting and page-size changes while still
 * resetting when the filters change or the list is left.
 */
;(function () {
    'use strict'

    var PREFIX = 'exhibition:selection:'

    // Query parameters that move you around a result set without changing it.
    // Everything else counts as a filter, and changing a filter drops the
    // selection because the user is now looking at a different list.
    var VOLATILE_PARAMS = ['page', 'page_size', 'ordering']

    function openStorage() {
        try {
            var storage = window.sessionStorage
            var probe = '__exhibition_selection_probe__'
            storage.setItem(probe, '1')
            storage.removeItem(probe)
            return storage
        } catch (err) {
            // Private mode or blocked storage: fall back to per-page selection.
            return null
        }
    }

    var storage = openStorage()

    function read(key) {
        if (!storage) {
            return null
        }
        try {
            return JSON.parse(storage.getItem(PREFIX + key))
        } catch (err) {
            return null
        }
    }

    function write(key, state) {
        if (!storage) {
            return
        }
        try {
            storage.setItem(PREFIX + key, JSON.stringify(state))
        } catch (err) {
            // Quota exhausted: the selection just stops persisting.
        }
    }

    function signatureFrom(search) {
        var params = new URLSearchParams(search || '')
        VOLATILE_PARAMS.forEach(function (name) {
            params.delete(name)
        })
        var pairs = []
        params.forEach(function (value, name) {
            pairs.push(name + '=' + value)
        })
        return pairs.sort().join('&')
    }

    var LAST_PATH_KEY = PREFIX + 'last-path'

    /*
     * True when this page load continues browsing the same list, i.e. a
     * pagination or sort click, a reload, or a step back to another page of it.
     * Arriving from anywhere else -- the dashboard, another list -- means the
     * list was left, so the stored selection is dropped.
     *
     * document.referrer cannot answer this: the control panel ships
     * <meta name="referrer" content="origin">, so it never carries a path.
     * Instead every exhibition page records its own path as it loads (this
     * module is pulled in from exhibitors/base.html), and the previous value is
     * where the user just came from.
     */
    function continuesSameList() {
        if (!storage) {
            return false
        }
        var current = window.location.pathname
        try {
            var previous = storage.getItem(LAST_PATH_KEY)
            // A bulk-action confirmation belongs to the list that opened it, so it
            // leaves the marker untouched: cancelling returns you to your selection.
            if (!document.querySelector('[data-selection-passthrough]')) {
                storage.setItem(LAST_PATH_KEY, current)
            }
            return previous === current
        } catch (err) {
            return false
        }
    }

    // Evaluated once per page load; init() may run again after an AJAX refresh
    // and must not re-drop a selection the user is still building.
    var continued = continuesSameList()
    var started = {}

    // Ids the list still contains, from the data-selection-ids attribute the
    // server renders; null when the element does not carry one.
    function presentIds(element) {
        if (!element || !element.hasAttribute('data-selection-ids')) {
            return null
        }
        return element.getAttribute('data-selection-ids').split(' ').filter(Boolean)
    }

    /*
     * options.scope: element carrying data-selection-ids. Stored ids missing from
     * it were deleted or acted on since they were picked, so they are dropped
     * rather than inflating the count with rows the server would ignore.
     */
    function create(options) {
        var settings = options || {}
        var key = settings.key || window.location.pathname
        var signature = signatureFrom(window.location.search)
        var state = read(key)

        if (!started[key]) {
            started[key] = true
            if (!continued) {
                state = null
            }
        }
        if (!state || state.signature !== signature || !state.items) {
            state = { signature: signature, items: {} }
        }
        var present = presentIds(settings.scope)
        if (present) {
            var keep = {}
            present.forEach(function (id) {
                keep[id] = true
            })
            Object.keys(state.items).forEach(function (id) {
                if (!keep[id]) {
                    delete state.items[id]
                }
            })
        }
        write(key, state)

        function persist() {
            write(key, state)
        }

        return {
            has: function (id) {
                return Object.prototype.hasOwnProperty.call(state.items, id)
            },
            meta: function (id) {
                return state.items[id] || null
            },
            add: function (id, meta) {
                state.items[id] = meta || {}
                persist()
            },
            remove: function (id) {
                delete state.items[id]
                persist()
            },
            toggle: function (id, selected, meta) {
                if (selected) {
                    state.items[id] = meta || state.items[id] || {}
                } else {
                    delete state.items[id]
                }
                persist()
            },
            ids: function () {
                return Object.keys(state.items)
            },
            size: function () {
                return Object.keys(state.items).length
            },
            clear: function () {
                state.items = {}
                persist()
            },
        }
    }

    window.ExhibitionSelection = { create: create, signatureFrom: signatureFrom }
})()
