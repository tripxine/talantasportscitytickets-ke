document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("booking-form");

    if (form) {
        const packageSelect = document.getElementById("package");
        const membersContainer = document.getElementById("members-container");
        const leaderSelect = document.getElementById("leader-index");
        const attendeeSection = document.getElementById("attendee-section");

        const packageNames = {
            SOLO: "Solo",
            GROUP5: "Group of 5",
            GROUP10: "Group of 10"
        };

        const packageSizes = {
            SOLO: 1,
            GROUP5: 5,
            GROUP10: 10
        };

        const money = value =>
            "KES " + Number(value).toLocaleString("en-KE");

        function renderMembers() {
            if (!packageSelect.value) {
                attendeeSection.hidden = true;
                membersContainer.replaceChildren();
                leaderSelect.replaceChildren();
                return;
            }
            attendeeSection.hidden = false;
            const count = packageSizes[packageSelect.value];

            membersContainer.innerHTML = "";
            leaderSelect.innerHTML = "";

            for (let i = 0; i < count; i++) {
                const section = document.createElement("section");
                section.className = "member-card";

                section.innerHTML = `
                    <h3>Attendee ${i + 1}</h3>
                    <label>Full name
                        <input name="member_name_${i}" required
                               minlength="2" autocomplete="off"
                               placeholder="Attendee full name">
                    </label>
                    <label>National ID or passport number
                        <input name="member_id_${i}" required
                               minlength="5" maxlength="25"
                               autocomplete="off"
                               placeholder="ID or passport number">
                    </label>
                    <label>Phone number
                        <input name="member_phone_${i}" required
                               type="tel" minlength="9" maxlength="16"
                               placeholder="07XXXXXXXX">
                    </label>
                `;

                membersContainer.appendChild(section);

                const option = document.createElement("option");
                option.value = String(i);
                option.textContent = `Attendee ${i + 1}`;
                leaderSelect.appendChild(option);
            }

            updateSummary();
        }

        const waveSelect = document.getElementById("wave");

        function updateSummary() {
            const count = packageSizes[packageSelect.value] || 1;
            const selected = form.querySelector(
                'input[name="category"]:checked'
            );

            const selectedWave = waveSelect ? waveSelect.value : "";
            const priceFor = input => {
                if (!input) return "";
                if (!waveSelect) return input.dataset.price || "";
                if (selectedWave === "WAVE1") return input.dataset.wave1Price || "";
                if (selectedWave === "WAVE2") return input.dataset.wave2Price || "";
                return "";
            };

            const priceValue = priceFor(selected);
            const hasPrice = priceValue !== "" &&
                Number.isFinite(Number(priceValue));
            const unit = hasPrice ? Number(priceValue) : 0;
            const discount = Math.max(0, (count - 1) * 200);
            const total = Math.max(0, (unit * count) - discount);

            document.getElementById("package-summary").textContent =
                packageNames[packageSelect.value] || "Solo";

            document.getElementById("quantity-summary").textContent = count;
            document.getElementById("unit-price").textContent =
                hasPrice ? money(unit) : "Price pending";
            document.getElementById("discount-price").textContent =
                money(discount);
            document.getElementById("total-price").textContent =
                hasPrice ? money(total) : "Price pending";

            form.querySelectorAll('input[name="category"]').forEach(input => {
                const label = input.closest(".ticket-option");
                const priceLabel = label?.querySelector(".ticket-price-label");
                if (!priceLabel) return;

                const value = priceFor(input);
                priceLabel.textContent = value !== "" &&
                    Number.isFinite(Number(value))
                    ? money(Number(value))
                    : (waveSelect && !selectedWave
                        ? "Choose a wave"
                        : "Price pending");
            });
        }

        packageSelect.addEventListener("change", renderMembers);

        form.querySelectorAll('input[name="category"]').forEach(input => {
            input.addEventListener("change", updateSummary);
        });

        if (waveSelect) {
            waveSelect.addEventListener("change", updateSummary);
        }

        updateSummary();

    }

    // Group-aware QR scanner.
    const scan = document.getElementById("scan-button");

    if (scan) {
        const tokenInput = document.getElementById("scan-token");
        const result = document.getElementById("scan-result");

        const memberPanel = document.createElement("div");
        memberPanel.id = "scan-members";
        result.insertAdjacentElement("afterend", memberPanel);

        async function sendScan(payload) {
            const response = await fetch("/api/scan", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload)
            });

            const data = await response.json();
            return {response, data};
        }

        function showResult(message, approved) {
            result.className = "scan-result " + (approved ? "approved" : "rejected");
            result.textContent = message;
        }

        function renderGroup(data, token) {
            memberPanel.replaceChildren();

            const heading = document.createElement("h3");
            heading.textContent = `Group ticket: ${data.reference}`;
            memberPanel.appendChild(heading);

            const progress = document.createElement("p");
            progress.textContent =
                `${data.checked_in} of ${data.total_members} attendees checked in · ${data.remaining} remaining`;
            memberPanel.appendChild(progress);

            data.members.forEach(member => {
                const row = document.createElement("div");
                row.className = "scan-member-row";

                const details = document.createElement("span");
                details.textContent =
                    `${member.name}${member.is_leader ? " (Group leader)" : ""} — ${member.entry_status}`;
                row.appendChild(details);

                if (member.entry_status !== "CHECKED_IN") {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "btn secondary small";
                    button.textContent = "Check in";

                    button.addEventListener("click", async () => {
                        button.disabled = true;
                        button.textContent = "Checking in…";

                        try {
                            const {response, data: checkin} = await sendScan({
                                token,
                                action: "checkin",
                                member_id: member.id
                            });

                            if (!response.ok || !checkin.ok) {
                                showResult(checkin.message || "Check-in failed.", false);
                            } else {
                                showResult(
                                    `${checkin.message}: ${checkin.name} (${checkin.reference})`,
                                    true
                                );
                            }

                            const refreshed = await sendScan({token, action: "lookup"});
                            if (refreshed.response.ok && refreshed.data.ok) {
                                renderGroup(refreshed.data, token);
                            }
                        } catch (error) {
                            showResult("Could not reach the server.", false);
                            button.disabled = false;
                            button.textContent = "Retry check-in";
                        }
                    });

                    row.appendChild(button);
                }

                memberPanel.appendChild(row);
            });

            if (data.remaining === 0) {
                const complete = document.createElement("p");
                complete.textContent = "All group members have checked in.";
                memberPanel.appendChild(complete);
            }
        }

        scan.addEventListener("click", async () => {
            const token = tokenInput.value.trim();

            memberPanel.replaceChildren();

            if (!token) {
                showResult("Enter a QR token first.", false);
                return;
            }

            scan.disabled = true;
            scan.textContent = "Looking up…";

            try {
                const {response, data} = await sendScan({token, action: "lookup"});

                if (!response.ok || !data.ok) {
                    showResult(data.message || "Ticket validation failed.", false);
                    return;
                }

                if (data.group) {
                    showResult(
                        `Valid group ticket — ${data.leader} (${data.reference})`,
                        true
                    );
                    renderGroup(data, token);
                } else if (data.legacy) {
                    showResult(
                        `Valid ticket — ${data.name} (${data.reference}). Confirm check-in below.`,
                        true
                    );

                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "btn primary";
                    button.textContent = "Check in ticket";

                    button.addEventListener("click", async () => {
                        button.disabled = true;
                        button.textContent = "Checking in…";

                        try {
                            const {response: checkResponse, data: checkin} =
                                await sendScan({token, action: "checkin"});

                            showResult(
                                checkin.message || "Check-in failed.",
                                checkResponse.ok && checkin.ok
                            );

                            if (!checkResponse.ok || !checkin.ok) {
                                button.disabled = false;
                                button.textContent = "Retry check-in";
                            } else {
                                button.remove();
                            }
                        } catch (error) {
                            showResult("Could not reach the server.", false);
                            button.disabled = false;
                            button.textContent = "Retry check-in";
                        }
                    });

                    memberPanel.appendChild(button);
                }
            } catch (error) {
                showResult("Could not reach the server.", false);
            } finally {
                scan.disabled = false;
                scan.textContent = "Look up ticket";
            }
        });
    }
});
