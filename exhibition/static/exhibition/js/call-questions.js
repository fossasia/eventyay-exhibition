document.addEventListener("DOMContentLoaded", () => {
    const syncRequiredDropdown = (row) => {
        const activeInput = row.querySelector(".field-active-input");
        const wrapper = row.querySelector(".required-status-wrapper");
        const dropdown = wrapper ? wrapper.querySelector(".required-status-dropdown") : null;
        if (!activeInput || !wrapper || !dropdown || dropdown.dataset.locked === "1") return;

        const isActive = activeInput.checked;
        dropdown.disabled = !isActive;
        wrapper.classList.toggle("is-disabled", !isActive);
    };

    document.querySelectorAll(".dragsort-table tbody tr").forEach((row) => {
        const activeInput = row.querySelector(".field-active-input");
        if (activeInput) {
            activeInput.addEventListener("change", () => syncRequiredDropdown(row));
        }
    });

    document.querySelectorAll(".dragsort-table .required-status-dropdown").forEach((dropdown) => {
        dropdown.addEventListener("change", () => {
            dropdown.dataset.current = dropdown.value;
            const wrapper = dropdown.closest(".required-status-wrapper");
            if (wrapper) {
                wrapper.dataset.current = dropdown.value;
            }
        });
    });
});


document.addEventListener("DOMContentLoaded", () => {
    const toggles = Array.from(
        document.querySelectorAll(
            '.dragsort-table .info-toggle[data-toggle="info-box"]'
        )
    );

    if (!toggles.length) return;

    const mobile = window.matchMedia("(max-width: 768px)");
    const tooltips = new Map();
    let activeToggle = null;

    toggles.forEach((toggle) => {
        const box = toggle.nextElementSibling;

        if (box && box.classList.contains("inline-info-box")) {
            tooltips.set(toggle, box);
        }
    });

    function closeTooltip() {
        if (!activeToggle) return;

        const toggle = activeToggle;
        const box = tooltips.get(toggle);

        box.classList.add("d-none");
        box.classList.remove("exhibition-mobile-tooltip");

        box.style.removeProperty("top");
        box.style.removeProperty("left");
        box.style.removeProperty("max-height");

        // Put the tooltip back beside its information icon.
        toggle.after(box);

        toggle.setAttribute("aria-expanded", "false");
        activeToggle = null;
    }

    function positionTooltip() {
        if (!activeToggle || !mobile.matches) return;

        const box = tooltips.get(activeToggle);
        const anchor = activeToggle.getBoundingClientRect();

        const margin = 16;
        const gap = 8;
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;

        if (
            anchor.bottom <= 0 ||
            anchor.top >= viewportHeight ||
            anchor.right <= 0 ||
            anchor.left >= viewportWidth
        ) {
            closeTooltip();
            return;
        }

        box.style.maxHeight = "none";

        const rect = box.getBoundingClientRect();

        const left = Math.max(
            margin,
            Math.min(anchor.left, viewportWidth - rect.width - margin)
        );

        const spaceBelow = Math.max(
            0,
            viewportHeight - anchor.bottom - gap - margin
        );
        const spaceAbove = Math.max(
            0,
            anchor.top - gap - margin
        );

        const openAbove =
            rect.height > spaceBelow && spaceAbove > spaceBelow;

        const availableHeight = openAbove ? spaceAbove : spaceBelow;

        box.style.maxHeight = `${availableHeight}px`;

        const height = box.getBoundingClientRect().height;
        const top = openAbove
            ? anchor.top - height - gap
            : anchor.bottom + gap;

        box.style.left = `${left}px`;
        box.style.top = `${Math.max(margin, top)}px`;
    }

    function openTooltip(toggle) {
        closeTooltip();

        const box = tooltips.get(toggle);
        if (!box) return;

        activeToggle = toggle;

        if (mobile.matches) {
            // Move the tooltip outside the responsive table.
            box.classList.add("exhibition-mobile-tooltip");
            document.body.appendChild(box);
        }

        box.classList.remove("d-none");
        toggle.setAttribute("aria-expanded", "true");

        positionTooltip();
    }

    tooltips.forEach((box, toggle) => {
        toggle.addEventListener("click", (event) => {
            event.stopPropagation();

            if (activeToggle === toggle) {
                closeTooltip();
            } else {
                openTooltip(toggle);
            }
        });

        toggle.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                toggle.click();
            }
        });
    });

    document.addEventListener("click", (event) => {
        if (!activeToggle) return;

        const box = tooltips.get(activeToggle);

        if (
            !box.contains(event.target) &&
            !activeToggle.contains(event.target)
        ) {
            closeTooltip();
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closeTooltip();
        }
    });

    window.addEventListener("scroll", positionTooltip, true);
    window.addEventListener("resize", closeTooltip);
});
