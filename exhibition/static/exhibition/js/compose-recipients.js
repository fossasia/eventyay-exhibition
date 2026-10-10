(function () {
    function init() {
        var filters = document.querySelector('[data-compose-filters]')
        var organizations = document.querySelector('[data-compose-organizations]')
        if (!filters || !organizations) {
            return
        }

        function sync() {
            var chosen = organizations.querySelector('input[type="checkbox"]:checked') !== null
            filters.classList.toggle('is-overridden', chosen)
        }

        organizations.addEventListener('change', sync)
        sync()
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init)
    } else {
        init()
    }
})()
