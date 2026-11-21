(function () {
    document.addEventListener('click', function (event) {
        var toggle = event.target.closest('[data-discount-group-toggle]')
        if (!toggle) {
            return
        }
        event.preventDefault()
        var expanded = toggle.getAttribute('aria-expanded') === 'true'
        var groupId = toggle.getAttribute('data-discount-group-toggle')
        document.querySelectorAll('[data-discount-group-member="' + groupId + '"]').forEach(function (row) {
            row.classList.toggle('hidden', expanded)
        })
        toggle.setAttribute('aria-expanded', expanded ? 'false' : 'true')
        var caret = toggle.querySelector('.fa')
        if (caret) {
            caret.classList.toggle('fa-caret-right', expanded)
            caret.classList.toggle('fa-caret-down', !expanded)
        }
    })
})()
