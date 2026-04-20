(function () {
    function setVisibleState(element, visible) {
        if (!element) return
        element.hidden = !visible
        element.style.display = visible ? '' : 'none'
    }

    document.addEventListener('DOMContentLoaded', function () {
        var previewObjectUrls = new WeakMap()
        var sponsorCheckbox = document.getElementById('id_is_sponsor')
        var sponsorGroupWrapper = document.getElementById('sponsor-group-wrapper')
        var notExhibitorCheckbox = document.getElementById('id_not_an_exhibitor')
        var boothNameWrapper = document.getElementById('booth-name-wrapper')
        var boothIdWrapper = document.getElementById('booth-id-wrapper')
        var leadScanningSection = document.getElementById('lead-scanning-section')
        var socialLinksFormset = document.getElementById('social-links-formset')
        var socialLinkPrefixes = {}

        if (socialLinksFormset && socialLinksFormset.dataset.socialLinkPrefixes) {
            try {
                socialLinkPrefixes = JSON.parse(socialLinksFormset.dataset.socialLinkPrefixes)
            } catch (error) {
                socialLinkPrefixes = {}
            }
        }

        function revokePreviewObjectUrl(pair) {
            var url = previewObjectUrls.get(pair)
            if (url) {
                URL.revokeObjectURL(url)
                previewObjectUrls.delete(pair)
            }
        }

        function getImagePreviewSource(pair, fileInput, urlInput, clearCheckbox) {
            var urlValue = urlInput.value.trim()
            if (urlValue) {
                revokePreviewObjectUrl(pair)
                return urlValue
            }

            if (fileInput.files && fileInput.files.length > 0) {
                revokePreviewObjectUrl(pair)
                var objectUrl = URL.createObjectURL(fileInput.files[0])
                previewObjectUrls.set(pair, objectUrl)
                return objectUrl
            }

            revokePreviewObjectUrl(pair)
            if (clearCheckbox && clearCheckbox.checked) {
                return ''
            }

            return pair.dataset.currentPreviewUrl || ''
        }

        function initImageSourcePair(pair) {
            var fileInput = document.getElementById(pair.dataset.fileInputId)
            var urlInput = document.getElementById(pair.dataset.urlInputId)
            var preview = pair.querySelector('[data-image-preview]')
            var previewLink = pair.querySelector('[data-image-preview-link]')
            var previewImage = pair.querySelector('[data-image-preview-image]')
            var previewError = pair.querySelector('[data-image-preview-error]')

            if (!fileInput || !urlInput || !preview || !previewLink || !previewImage) {
                return
            }

            var clearCheckbox = fileInput.form && fileInput.form.elements
                ? fileInput.form.elements[fileInput.name + '-clear']
                : null

            function syncImageState() {
                var urlValue = urlInput.value.trim()
                var hasUrl = urlValue.length > 0
                var hasSelectedFile = Boolean(fileInput.files && fileInput.files.length > 0)
                var hasCurrentFile = pair.dataset.hasCurrentFile === 'true' && !(clearCheckbox && clearCheckbox.checked)

                if (hasUrl && hasSelectedFile) {
                    fileInput.value = ''
                }

                fileInput.disabled = hasUrl
                if (clearCheckbox) {
                    clearCheckbox.disabled = hasUrl
                }
                urlInput.disabled = !hasUrl && (hasSelectedFile || hasCurrentFile)

                var previewSource = getImagePreviewSource(pair, fileInput, urlInput, clearCheckbox)
                if (!previewSource) {
                    setVisibleState(preview, false)
                    setVisibleState(previewLink, false)
                    setVisibleState(previewImage, false)
                    setVisibleState(previewError, false)
                    previewLink.removeAttribute('href')
                    previewImage.removeAttribute('src')
                    return
                }

                setVisibleState(preview, true)
                setVisibleState(previewLink, true)
                previewLink.href = previewSource
                previewImage.src = previewSource
                setVisibleState(previewImage, true)
                setVisibleState(previewError, false)
            }

            previewImage.addEventListener('load', function () {
                setVisibleState(previewImage, true)
                setVisibleState(previewError, false)
            })
            previewImage.addEventListener('error', function () {
                if (!previewImage.src) {
                    return
                }
                setVisibleState(previewImage, false)
                setVisibleState(previewError, true)
            })

            urlInput.addEventListener('input', syncImageState)
            fileInput.addEventListener('change', function () {
                if (fileInput.files && fileInput.files.length > 0) {
                    urlInput.value = ''
                }
                syncImageState()
            })
            if (clearCheckbox) {
                clearCheckbox.addEventListener('change', function () {
                    if (clearCheckbox.checked) {
                        pair.dataset.hasCurrentFile = 'false'
                    }
                    syncImageState()
                })
            }

            syncImageState()
        }

        function updateSocialLinkPrefix(row) {
            if (!row) return
            var select = row.querySelector('select[name$="-network"]')
            var prefix = row.querySelector('[data-social-prefix]')
            if (!select || !prefix) {
                return
            }
            prefix.textContent = socialLinkPrefixes[select.value] || 'https://'
        }

        function initSocialLinkRow(row) {
            if (!row || row.dataset.socialPrefixBound === 'true') {
                updateSocialLinkPrefix(row)
                return
            }

            var select = row.querySelector('select[name$="-network"]')
            if (select) {
                select.addEventListener('change', function () {
                    updateSocialLinkPrefix(row)
                })
            }

            row.dataset.socialPrefixBound = 'true'
            updateSocialLinkPrefix(row)
        }

        function toggleSponsorGroup() {
            if (!sponsorCheckbox || !sponsorGroupWrapper) {
                return
            }
            sponsorGroupWrapper.classList.toggle('hidden', !sponsorCheckbox.checked)
        }

        function toggleExhibitorFields() {
            if (!notExhibitorCheckbox) {
                return
            }
            var hideExhibitorFields = notExhibitorCheckbox.checked
            boothNameWrapper.classList.toggle('hidden', hideExhibitorFields)
            boothIdWrapper.classList.toggle('hidden', hideExhibitorFields)
            leadScanningSection.classList.toggle('hidden', hideExhibitorFields)
        }

        toggleSponsorGroup()
        toggleExhibitorFields()

        if (sponsorCheckbox) {
            sponsorCheckbox.addEventListener('change', toggleSponsorGroup)
        }

        if (notExhibitorCheckbox) {
            notExhibitorCheckbox.addEventListener('change', toggleExhibitorFields)
        }

        document.querySelectorAll('[data-partner-image-source-pair]').forEach(initImageSourcePair)
        document.querySelectorAll('[data-social-link-row]').forEach(initSocialLinkRow)

        if (window.jQuery && socialLinksFormset) {
            window.jQuery(socialLinksFormset).on('formAdded', 'div', function (event) {
                initSocialLinkRow(event.target)
            })
        }
    })
})()
