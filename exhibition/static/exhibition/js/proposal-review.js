(function () {
    function init() {
        var checkboxes = {
            sponsor: document.getElementById('id_is_sponsor'),
            exhibitor: document.getElementById('id_is_exhibitor'),
        }
        var fields = document.querySelectorAll('[data-partner-type-field]')
        if (!fields.length) return

        function sync() {
            fields.forEach(function (field) {
                var checkbox = checkboxes[field.dataset.partnerTypeField]
                field.classList.toggle('hidden', !(checkbox && checkbox.checked))
            })
        }

        Object.keys(checkboxes).forEach(function (key) {
            if (checkboxes[key]) checkboxes[key].addEventListener('change', sync)
        })
        sync()
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true })
    } else {
        init()
    }
})()
