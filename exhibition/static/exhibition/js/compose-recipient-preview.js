const FILTER_FIELDS = ['state', 'organization_type', 'sponsor_group', 'organizations']

const button = document.querySelector('[data-compose-recipient-preview-button]')
const panel = document.querySelector('[data-compose-recipient-preview]')

const filterQuery = (form) => {
    const params = new URLSearchParams()
    new FormData(form).forEach((value, name) => {
        if (FILTER_FIELDS.includes(name) && value) {
            params.append(name, value)
        }
    })
    return params.toString()
}

const showMessage = (text, failed = false) => {
    const message = document.createElement('p')
    message.className = failed ? 'text-danger' : 'text-muted'
    message.textContent = text
    panel.replaceChildren(message)
    panel.hidden = false
}

const showRecipients = (recipients) => {
    if (!recipients.length) {
        showMessage(panel.dataset.emptyLabel)
        return
    }
    const heading = document.createElement('p')
    heading.className = 'compose-recipient-preview-count'
    heading.textContent =
        recipients.length === 1
            ? panel.dataset.oneLabel
            : panel.dataset.manyLabel.replace('{count}', recipients.length)
    const list = document.createElement('ul')
    list.className = 'compose-recipient-preview-list'
    recipients.forEach((recipient) => {
        const item = document.createElement('li')
        const name = document.createElement('strong')
        name.textContent = recipient.name
        const email = document.createElement('span')
        email.className = 'text-muted'
        email.textContent = recipient.email || panel.dataset.hiddenEmailLabel
        item.append(name, ' · ', email)
        list.append(item)
    })
    panel.replaceChildren(heading, list)
    panel.hidden = false
}

button?.addEventListener('click', async () => {
    const form = button.closest('form')
    const url = `${button.dataset.url}?${filterQuery(form)}`
    button.disabled = true
    try {
        const response = await fetch(url, { credentials: 'same-origin' })
        if (!response.ok) {
            throw new Error(`Recipient preview returned ${response.status}`)
        }
        const data = await response.json()
        showRecipients(data.recipients || [])
    } catch (error) {
        console.error('Loading the compose recipient preview failed', url, error)
        showMessage(panel.dataset.errorLabel, true)
    } finally {
        button.disabled = false
    }
})

button?.closest('form')?.addEventListener('change', (event) => {
    if (FILTER_FIELDS.includes(event.target.name)) {
        panel.hidden = true
    }
})
