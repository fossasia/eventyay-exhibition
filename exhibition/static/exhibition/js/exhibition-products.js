document.addEventListener("DOMContentLoaded", () => {
    const table = document.querySelector(".exhibition-products-table");
    if (!table) return;

    const syncBoothToggle = (row, purposeChanged = false) => {
        const purpose = row.querySelector(".exhibition-purpose-input");
        const toggle = row.querySelector(".exhibition-booth-toggle");
        const input = row.querySelector(".exhibition-booth-input");
        if (!purpose || !toggle || !input) return;

        if (purpose.value === "sponsorship") {
            if (purposeChanged) {
                input.checked = row.dataset.sponsorshipBooth !== "off";
            }
            input.disabled = false;
            toggle.classList.remove("is-disabled");
            return;
        }

        input.checked = purpose.value === "exhibition";
        input.disabled = true;
        toggle.classList.add("is-disabled");
    };

    table.querySelectorAll(".exhibition-product-row").forEach((row) => {
        const purpose = row.querySelector(".exhibition-purpose-input");
        const booth = row.querySelector(".exhibition-booth-input");
        if (booth) {
            if (purpose && purpose.value === "sponsorship") {
                row.dataset.sponsorshipBooth = booth.checked ? "on" : "off";
            }
            booth.addEventListener("change", () => {
                row.dataset.sponsorshipBooth = booth.checked ? "on" : "off";
            });
        }
        if (purpose) {
            purpose.addEventListener("change", () => syncBoothToggle(row, true));
        }
        syncBoothToggle(row);
    });
});
