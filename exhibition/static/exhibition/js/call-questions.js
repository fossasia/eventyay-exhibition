document.addEventListener("DOMContentLoaded", () => {
    const table = document.querySelector(".dragsort-table");
    if (!table) return;

    const syncRequiredDropdown = (row) => {
        const activeInput = row.querySelector(".field-active-input");
        const wrapper = row.querySelector(".required-status-wrapper");
        const dropdown = wrapper ? wrapper.querySelector(".required-status-dropdown") : null;
        if (!activeInput || !wrapper || !dropdown || dropdown.dataset.locked === "1") return;

        const isActive = activeInput.checked;
        dropdown.disabled = !isActive;
        wrapper.classList.toggle("is-disabled", !isActive);
    };

    table.querySelectorAll("tbody tr").forEach((row) => {
        const activeInput = row.querySelector(".field-active-input");
        if (activeInput) {
            activeInput.addEventListener("change", () => syncRequiredDropdown(row));
        }
    });

    table.querySelectorAll(".required-status-dropdown").forEach((dropdown) => {
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
    const toggles = Array.from(document.querySelectorAll('.info-toggle[data-toggle="info-box"]'));
    if (!toggles.length) return;

    const boxOf = (toggle) => {
        const box = toggle.nextElementSibling;
        return box && box.classList.contains("inline-info-box") ? box : null;
    };

    const setOpen = (toggle, open) => {
        const box = boxOf(toggle);
        if (!box) return;
        box.classList.toggle("d-none", !open);
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
    };

    const closeAll = (except) => {
        toggles.forEach((toggle) => {
            if (toggle !== except) setOpen(toggle, false);
        });
    };

    toggles.forEach((toggle) => {
        toggle.addEventListener("click", (event) => {
            event.stopPropagation();
            const isOpen = toggle.getAttribute("aria-expanded") === "true";
            closeAll(toggle);
            setOpen(toggle, !isOpen);
        });
        toggle.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                toggle.click();
            }
        });
    });

    document.addEventListener("click", (event) => {
        if (!event.target.closest(".inline-info-box")) closeAll(null);
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeAll(null);
    });
});
