document.addEventListener("DOMContentLoaded", () => {
    const purpose = document.querySelector("[data-exhibition-product-purpose]");
    const booth = document.querySelector("[data-exhibition-product-booth]");
    if (!purpose || !booth) return;

    // An exhibition product always includes a booth; a sponsorship keeps whatever the organizer chose.
    let sponsorshipBooth = booth.checked;

    const sync = () => {
        if (purpose.value === "exhibition") {
            booth.checked = true;
            booth.disabled = true;
        } else {
            booth.checked = sponsorshipBooth;
            booth.disabled = false;
        }
    };

    booth.addEventListener("change", () => {
        sponsorshipBooth = booth.checked;
    });
    purpose.addEventListener("change", sync);
    sync();
});
