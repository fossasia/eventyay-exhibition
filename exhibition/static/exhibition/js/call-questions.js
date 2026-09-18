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
