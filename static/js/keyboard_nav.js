/**
 * Accounting Keyboard Navigation Logic (Tally-style)
 */

document.addEventListener('keydown', function(event) {
    // 1. Enter -> Move to Next Field
    // We target inputs, selects, and textareas but allow normal behavior for submit buttons or specific cases
    if (event.key === 'Enter' && !event.shiftKey) {
        const active = document.activeElement;
        const tagName = active.tagName;
        
        if (tagName === 'INPUT' || tagName === 'SELECT' || tagName === 'TEXTAREA') {
            // Exceptions: 
            // - Don't prevent enter on buttons (let them click)
            // - Don't prevent enter on textareas if Ctrl is pressed (let it newline)
            if (tagName === 'TEXTAREA' && event.ctrlKey) return;
            if (active.type === 'submit' || active.type === 'button') return;

            event.preventDefault();
            const form = active.form;
            if (!form) return;

            // Find next focusable element
            const focusable = Array.from(form.elements).filter(el => {
                return !el.disabled && el.type !== 'hidden' && el.tabIndex !== -1 && !el.readOnly;
            });
            
            const index = focusable.indexOf(active);
            if (index > -1 && index < focusable.length - 1) {
                const next = focusable[index + 1];
                next.focus();
                if (next.select) next.select();
            }
        }
    }

    // 2. Ctrl+K -> Universal Search
    if (event.ctrlKey && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        const cpTrigger = document.getElementById('topbar-search-trigger');
        if (cpTrigger) {
            cpTrigger.click();
        }
    }

    // 3. Alt+C -> Create Ledger / Quick Add
    if (event.altKey && event.key.toLowerCase() === 'c') {
        event.preventDefault();
        
        // If we are in a voucher form with a quickAddModal, trigger it
        if (typeof openQuickAdd === 'function') {
            // Try to find a context-appropriate button or just trigger modal
            const qaBtn = document.querySelector('.btn-quick-add') || { closest: () => null };
            openQuickAdd(qaBtn);
        } else {
            // Otherwise redirect to full create page
            window.location.href = '/ledger/create/';
        }
    }
});

// Auto-focus first input on page load for speed
window.addEventListener('DOMContentLoaded', () => {
    const firstInput = document.querySelector('form input:not([type=hidden]):not([disabled])');
    if (firstInput) {
        firstInput.focus();
    }
});
