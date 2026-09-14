;(function () {
    'use strict'

    var PREFIX = 'exhibition:selection:'
    var VOLATILE_PARAMS = ['page', 'page_size', 'ordering']
    var LAST_PATH_KEY = PREFIX + 'last-path'

    function openStorage() {
        try {
            var storage = window.sessionStorage
            var probe = '__exhibition_selection_probe__'
            storage.setItem(probe, '1')
            storage.removeItem(probe)
            return storage
        } catch (err) {
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
            return
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

    function continuesSameList() {
        if (!storage) {
            return false
        }
        var current = window.location.pathname
        try {
            var previous = storage.getItem(LAST_PATH_KEY)
            if (!document.querySelector('[data-selection-passthrough]')) {
                storage.setItem(LAST_PATH_KEY, current)
            }
            return previous === current
        } catch (err) {
            return false
        }
    }

    var continued = continuesSameList()
    var started = {}

    function presentIds(element) {
        if (!element || !element.hasAttribute('data-selection-ids')) {
            return null
        }
        return element.getAttribute('data-selection-ids').split(' ').filter(Boolean)
    }

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
            addAll: function (ids, metaFor) {
                ids.forEach(function (id) {
                    state.items[id] = (metaFor ? metaFor(id) : null) || state.items[id] || {}
                })
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
            available: function () {
                return present ? present.slice() : null
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
