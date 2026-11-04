const toggles = document.querySelectorAll('[data-exhibition-view-toggle]')
const views = document.querySelectorAll('[data-exhibition-view]')

const showView = (view) => {
    views.forEach((element) => {
        element.hidden = element.dataset.exhibitionView !== view
    })
    toggles.forEach((toggle) => {
        toggle.hidden = toggle.dataset.exhibitionViewToggle === view
    })
    const url = new URL(window.location.href)
    if (view === 'layout') {
        url.searchParams.set('view', 'layout')
    } else {
        url.searchParams.delete('view')
    }
    window.history.replaceState(null, '', url)
}

toggles.forEach((toggle) => {
    toggle.addEventListener('click', (event) => {
        event.preventDefault()
        showView(toggle.dataset.exhibitionViewToggle)
        document.querySelector('[data-exhibition-view-toggle]:not([hidden])')?.focus()
    })
})
