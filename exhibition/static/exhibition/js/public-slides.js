(function () {
    var SWIPE_THRESHOLD = 40

    function getScale(page, containerWidth) {
        var baseViewport = page.getViewport(1.0)
        if (!baseViewport.width) {
            return 1.0
        }
        return containerWidth / baseViewport.width
    }

    function setStatus(state) {
        if (!state.status) {
            return
        }
        state.status.textContent = 'Slide ' + state.pageNumber + ' of ' + state.pdf.numPages
    }

    function updateControls(state) {
        if (state.prevButton) {
            state.prevButton.disabled = state.pageNumber <= 1
        }
        if (state.nextButton) {
            state.nextButton.disabled = state.pageNumber >= state.pdf.numPages
        }
        setStatus(state)
    }

    async function renderCurrentPage(state) {
        if (!state.pdf || !state.canvas) {
            return
        }

        var page = await state.pdf.getPage(state.pageNumber)
        var frameWidth = state.frame.clientWidth || state.container.clientWidth || 960
        var pixelRatio = window.devicePixelRatio || 1
        var scale = getScale(page, frameWidth)
        var viewport = page.getViewport(scale)
        var renderViewport = page.getViewport(scale * pixelRatio)

        state.canvas.width = Math.floor(renderViewport.width)
        state.canvas.height = Math.floor(renderViewport.height)
        state.canvas.style.width = Math.floor(viewport.width) + 'px'
        state.canvas.style.height = Math.floor(viewport.height) + 'px'

        var renderTask = page.render({
            canvasContext: state.canvas.getContext('2d'),
            viewport: renderViewport,
        })

        if (renderTask.promise) {
            await renderTask.promise
        } else {
            await renderTask
        }

        updateControls(state)
    }

    function navigateToPage(state, nextPage) {
        if (!state.pdf) {
            return
        }

        var boundedPage = Math.min(Math.max(nextPage, 1), state.pdf.numPages)
        if (boundedPage === state.pageNumber || state.rendering) {
            return
        }

        state.pageNumber = boundedPage
        state.rendering = true
        renderCurrentPage(state)
            .catch(function () {
                showFallback(state.container)
            })
            .finally(function () {
                state.rendering = false
            })
    }

    function bindTouchNavigation(state) {
        var startX = null

        state.frame.addEventListener('touchstart', function (event) {
            if (!event.touches || event.touches.length !== 1) {
                startX = null
                return
            }
            startX = event.touches[0].clientX
        }, { passive: true })

        state.frame.addEventListener('touchend', function (event) {
            if (startX === null || !event.changedTouches || !event.changedTouches.length) {
                startX = null
                return
            }

            var deltaX = event.changedTouches[0].clientX - startX
            startX = null

            if (Math.abs(deltaX) < SWIPE_THRESHOLD) {
                return
            }

            if (deltaX < 0) {
                navigateToPage(state, state.pageNumber + 1)
            } else {
                navigateToPage(state, state.pageNumber - 1)
            }
        }, { passive: true })
    }

    function bindResize(state) {
        var resizeTimeout = null
        window.addEventListener('resize', function () {
            if (resizeTimeout) {
                window.clearTimeout(resizeTimeout)
            }
            resizeTimeout = window.setTimeout(function () {
                if (state.rendering) {
                    return
                }
                state.rendering = true
                renderCurrentPage(state)
                    .catch(function () {
                        showFallback(state.container)
                    })
                    .finally(function () {
                        state.rendering = false
                    })
            }, 120)
        })
    }

    async function renderPdfSlides(container) {
        if (!window.PDFJS) {
            throw new Error('PDFJS is not available.')
        }

        var pdfUrl = container.dataset.pdfUrl
        var workerUrl = container.dataset.workerUrl
        var frame = container.querySelector('[data-pdf-slides-frame]')
        var canvas = container.querySelector('[data-pdf-slides-canvas]')
        var prevButton = container.querySelector('[data-pdf-prev]')
        var nextButton = container.querySelector('[data-pdf-next]')
        var status = container.querySelector('[data-pdf-status]')
        if (!pdfUrl || !frame || !canvas) {
            return
        }

        window.PDFJS.workerSrc = workerUrl

        var loadingTask = window.PDFJS.getDocument(pdfUrl)
        var pdf = await loadingTask.promise

        var state = {
            container: container,
            frame: frame,
            canvas: canvas,
            prevButton: prevButton,
            nextButton: nextButton,
            status: status,
            pdf: pdf,
            pageNumber: 1,
            rendering: false,
        }

        if (prevButton) {
            prevButton.addEventListener('click', function () {
                navigateToPage(state, state.pageNumber - 1)
            })
        }

        if (nextButton) {
            nextButton.addEventListener('click', function () {
                navigateToPage(state, state.pageNumber + 1)
            })
        }

        bindTouchNavigation(state)
        bindResize(state)

        state.rendering = true
        await renderCurrentPage(state)
        state.rendering = false
    }

    function showFallback(container) {
        var fallback = container.querySelector('[data-pdf-slides-fallback]')
        var frame = container.querySelector('[data-pdf-slides-frame]')
        var controls = container.querySelector('[data-pdf-slides-controls]')
        if (frame) {
            frame.style.display = 'none'
        }
        if (controls) {
            controls.style.display = 'none'
        }
        if (fallback) {
            fallback.classList.remove('hidden')
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        var containers = document.querySelectorAll('[data-pdf-slides]')
        containers.forEach(function (container) {
            renderPdfSlides(container).catch(function () {
                showFallback(container)
            })
        })
    })
})()
