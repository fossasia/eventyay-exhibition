(function () {
    var TRIGGER_SELECTOR = '.exhibition-image-preview a[data-image-preview-link]'

    function init() {
        var dialog = document.getElementById('exhibition-image-dialog')
        if (!dialog || typeof dialog.showModal !== 'function') {
            return
        }

        if (dialog.parentNode !== document.body) {
            document.body.appendChild(dialog)
        }

        var image = dialog.querySelector('[data-image-dialog-image]')
        var caption = dialog.querySelector('[data-image-dialog-caption]')
        var content = dialog.querySelector('.modal-card-content')

        function close() {
            if (dialog.open) {
                dialog.close()
            }
        }

        function open(link) {
            var thumbnail = link.querySelector('img')
            var label = thumbnail ? thumbnail.alt : ''
            image.src = link.href
            image.alt = label
            caption.textContent = label
            if (dialog.open) {
                return
            }

            var scrollX = window.scrollX
            var scrollY = window.scrollY
            dialog.showModal()
            dialog.focus({ preventScroll: true })
            window.scrollTo({ top: scrollY, left: scrollX, behavior: 'instant' })
        }

        dialog.addEventListener('click', close)
        dialog.addEventListener('close', function () {
            image.removeAttribute('src')
        })
        content.addEventListener('click', function (event) {
            event.stopPropagation()
        })
        dialog.querySelector('[data-image-dialog-close]').addEventListener('click', close)

        document.addEventListener('click', function (event) {
            if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
                return
            }
            var target = event.target
            if (!target || typeof target.closest !== 'function') {
                return
            }
            var link = target.closest(TRIGGER_SELECTOR)
            if (!link || !link.getAttribute('href')) {
                return
            }
            event.preventDefault()
            open(link)
        })
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init)
    } else {
        init()
    }
})()
