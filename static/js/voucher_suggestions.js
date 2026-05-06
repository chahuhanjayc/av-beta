/**
 * Smart Voucher Suggestion Engine - JS Integration (Fixed for Split Dr/Cr)
 */

(function() {
    const smartInput = document.getElementById('smartEntry');
    const smartButton = document.getElementById('smartEntryBtn');
    const suggestionBox = document.getElementById('smartSuggestions');
    const suggestionChips = document.getElementById('suggestionChips');
    let debounceTimer;

    if (!smartInput) return;

    function tryApplyStock(query) {
        if (typeof window.tryApplyStockSmartEntry !== 'function') return false;
        const applied = window.tryApplyStockSmartEntry(query);
        if (applied) {
            smartInput.value = '';
            suggestionBox.classList.add('d-none');
        }
        return applied;
    }

    smartInput.addEventListener('input', function() {
        clearTimeout(debounceTimer);
        const query = this.value.trim();
        
        if (query.length < 2) {
            suggestionBox.classList.add('d-none');
            return;
        }

        debounceTimer = setTimeout(() => {
            fetchSuggestions(query);
        }, 50);
    });

    smartInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            const query = smartInput.value.trim();
            if (query && tryApplyStock(query)) return;
            const firstChip = suggestionChips.querySelector('.suggestion-chip');
            if (firstChip) {
                firstChip.click();
            } else if (query) {
                fetchSuggestions(query, true);
            }
        }
    });

    if (smartButton) {
        smartButton.addEventListener('click', function() {
            const query = smartInput.value.trim();
            if (!query) {
                showSuggestionMessage('Type something like "rent 2500" first.');
                smartInput.focus();
                return;
            }
            if (tryApplyStock(query)) return;
            fetchSuggestions(query, true);
        });
    }

    async function fetchSuggestions(query, autoApply = false) {
        try {
            const response = await fetch(`/vouchers/suggestion-api/?q=${encodeURIComponent(query)}`);
            const data = await response.json();
            const suggestions = data.suggestions || [];
            renderSuggestions(suggestions);
            if (autoApply) {
                if (suggestions.length > 0) {
                    applySuggestion(suggestions[0]);
                    showSuggestionMessage('Applied the best matching ledger. Add the opposite Dr/Cr line to balance the voucher.', 'success');
                } else {
                    showSuggestionMessage('No matching ledger found. Use the + button beside Particulars to create one.');
                }
            }
        } catch (error) {
            console.error('Suggestion fetch failed', error);
            showSuggestionMessage('Could not load suggestions. Check the ledger list or try again.');
        }
    }

    function renderSuggestions(suggestions) {
        suggestionChips.innerHTML = '';
        if (suggestions.length === 0) {
            suggestionBox.classList.add('d-none');
            return;
        }

        suggestions.forEach(s => {
            const chip = document.createElement('div');
            chip.className = 'suggestion-chip badge rounded-pill bg-primary-subtle text-primary border border-primary-subtle p-2 cursor-pointer me-1 mb-1';
            chip.style.cursor = 'pointer';
            const groupLabel = s.nature ? `${s.group} / ${s.nature}` : s.group;
            chip.innerHTML = `<strong>${s.name}</strong> <span class="text-muted">${groupLabel} - Rs.${s.amount}</span>`;
            chip.onclick = () => applySuggestion(s);
            suggestionChips.appendChild(chip);
        });

        suggestionBox.classList.remove('d-none');
    }

    function applySuggestion(s) {
        const rows = document.querySelectorAll('.item-row');
        let targetRow = null;

        // Find first empty row or use last row
        for (let row of rows) {
            const ledgerSelect = row.querySelector('.ledger-select');
            const drInput = row.querySelector('.dr-field');
            const crInput = row.querySelector('.cr-field');
            
            if (!ledgerSelect.value && !drInput.value && !crInput.value) {
                targetRow = row;
                break;
            }
        }

        if (!targetRow) {
            // Find the Add Row logic - in our template we just need to trigger the logic that adds a row
            // The current template adds a row when the last ledger is filled.
            // But we can manually trigger addNewRow if it was global.
            // For now, let's just use the last row if no empty row found.
            targetRow = rows[rows.length - 1];
        }

        if (targetRow) {
            const ledgerSelect = targetRow.querySelector('.ledger-select');
            const drInput = targetRow.querySelector('.dr-field');
            const crInput = targetRow.querySelector('.cr-field');

            // Set Ledger
            if (!ledgerSelect.querySelector(`option[value="${s.id}"]`)) {
                const opt = new Option(s.name, s.id, true, true);
                ledgerSelect.add(opt);
            }
            ledgerSelect.value = s.id;

            // Determine Debit/Credit based on Group and Voucher Type
            const vtype = document.querySelector('[name="voucher_type"]').value;
            let isDebit = true;

            const nature = s.nature || s.group;
            if (['Expense', 'Asset'].includes(nature)) {
                isDebit = (vtype !== 'Receipt');
            } else {
                isDebit = (vtype === 'Receipt');
            }

            if (isDebit) {
                drInput.value = s.amount;
                crInput.value = '';
            } else {
                crInput.value = s.amount;
                drInput.value = '';
            }

            // Trigger input events so the hidden fields and totals update
            drInput.dispatchEvent(new Event('input', { bubbles: true }));
            crInput.dispatchEvent(new Event('input', { bubbles: true }));
            ledgerSelect.dispatchEvent(new Event('change', { bubbles: true }));

            // Clear smart entry and hide suggestions
            smartInput.value = '';
            suggestionBox.classList.add('d-none');
            
            // Focus the next field
            if (isDebit) {
                drInput.focus();
                drInput.select();
            } else {
                crInput.focus();
                crInput.select();
            }
        }
    }

    function showSuggestionMessage(message, level = 'warning') {
        suggestionBox.classList.remove('d-none');
        suggestionChips.innerHTML = `<div class="small text-${level === 'success' ? 'success' : 'muted'}">${message}</div>`;
    }

    // Initialize with most frequent ledgers
    window.addEventListener('DOMContentLoaded', () => {
        fetchSuggestions(''); 
    });
})();
