const SEARCH_DELAY_MS = 300

const form = document.querySelector('[data-exhibition-search]')
const results = document.querySelector('[data-exhibition-results]')
const status = document.querySelector('[data-exhibition-search-status]')

if (form && results) {
    let controller = null
    let timer = null

    const searchUrl = () => {
        const params = new URLSearchParams(new FormData(form))
        if (!params.get('query')) {
            params.delete('query')
        }
        const query = params.toString()
        return query ? `${window.location.pathname}?${query}` : window.location.pathname
    }

    const cancelPendingSearch = () => {
        window.clearTimeout(timer)
        controller?.abort()
        controller = null
    }

    const showFailure = (submitted, url) => {
        if (submitted) {
            window.location.assign(url)
            return
        }
        results.removeAttribute('aria-busy')
        if (status) {
            status.hidden = false
        }
    }

    const search = async ({ submitted = false } = {}) => {
        const url = searchUrl()
        cancelPendingSearch()
        const current = new AbortController()
        controller = current
        results.setAttribute('aria-busy', 'true')
        try {
            const response = await fetch(url, { signal: current.signal })
            const page = new DOMParser().parseFromString(await response.text(), 'text/html')
            const nextResults = page.querySelector('[data-exhibition-results]')
            if (!response.ok || !nextResults) {
                showFailure(submitted, url)
                return
            }
            results.replaceChildren(...nextResults.childNodes)
            results.removeAttribute('aria-busy')
            if (status) {
                status.hidden = true
            }
            window.history.replaceState(null, '', url)
        } catch (error) {
            if (error.name !== 'AbortError') {
                showFailure(submitted, url)
            }
        }
    }

    form.addEventListener('submit', (event) => {
        event.preventDefault()
        search({ submitted: true })
    })

    form.querySelector('input[name="query"]')?.addEventListener('input', () => {
        cancelPendingSearch()
        timer = window.setTimeout(search, SEARCH_DELAY_MS)
    })
}
