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
    let openInfoBox = null;

    const closeOpenInfoBox = () => {
        if (openInfoBox) {
            openInfoBox.classList.add("d-none");
            openInfoBox = null;
        }
    };

    const toggleInfoBox = (toggle) => {
        const infoBox = toggle.nextElementSibling;
        if (!infoBox || !infoBox.classList.contains("inline-info-box")) return;
        if (openInfoBox && openInfoBox !== infoBox) {
            openInfoBox.classList.add("d-none");
        }
        infoBox.classList.toggle("d-none");
        openInfoBox = infoBox.classList.contains("d-none") ? null : infoBox;
    };

    document.addEventListener("click", (event) => {
        const toggle = event.target.closest('.info-toggle[data-toggle="info-box"]');
        if (toggle) {
            toggleInfoBox(toggle);
            event.stopPropagation();
            return;
        }
        if (!event.target.closest(".inline-info-box")) {
            closeOpenInfoBox();
        }
    });

    document.addEventListener("keydown", (event) => {
        const toggle = event.target.closest('.info-toggle[data-toggle="info-box"]');
        if (toggle && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            toggleInfoBox(toggle);
            return;
        }
        if (event.key === "Escape") {
            closeOpenInfoBox();
        }
    });
});
