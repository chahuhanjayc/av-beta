/**
 * Sidebar persistence and stability logic.
 */

document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;

    const scrollKey = 'sidebar_scroll';
    const openSectionsKey = 'sidebar_open_sections';
    const legacyOpenKey = 'sidebar_open';
    const collapses = Array.from(sidebar.querySelectorAll('.nav-collapse, .nav-subcollapse'));

    const storageGet = (key) => {
        try {
            return window.localStorage.getItem(key);
        } catch {
            return null;
        }
    };

    const storageSet = (key, value) => {
        try {
            window.localStorage.setItem(key, value);
        } catch {
            // Storage restrictions should not break navigation.
        }
    };

    const storageRemove = (key) => {
        try {
            window.localStorage.removeItem(key);
        } catch {
            // Ignore storage restrictions.
        }
    };

    const getTrigger = (sectionId) => (
        sidebar.querySelector(`[data-bs-target="#${sectionId}"]`)
    );

    const syncTrigger = (section, isOpen) => {
        const trigger = getTrigger(section.id);
        if (!trigger) return;

        trigger.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        trigger.classList.toggle('collapsed', !isOpen);
    };

    const getStoredOpenSectionIds = () => {
        const stored = storageGet(openSectionsKey);
        const sectionIds = new Set();

        if (stored) {
            try {
                const parsed = JSON.parse(stored);
                if (Array.isArray(parsed)) {
                    parsed.forEach((id) => {
                        if (typeof id === 'string') sectionIds.add(id);
                    });
                }
            } catch {
                // Fall through to the legacy single-section state.
            }
        }

        const legacyOpenId = storageGet(legacyOpenKey);
        if (legacyOpenId) sectionIds.add(legacyOpenId);

        return sectionIds;
    };

    const persistOpenSections = () => {
        const openIds = collapses
            .filter((section) => section.classList.contains('show'))
            .map((section) => section.id);

        storageSet(openSectionsKey, JSON.stringify(openIds));
        storageRemove(legacyOpenKey);
    };

    const preserveScrollPosition = () => {
        const scrollTop = sidebar.scrollTop;
        requestAnimationFrame(() => {
            sidebar.scrollTop = scrollTop;
        });
    };

    const scrollElementByWheel = (element, deltaY) => {
        const maxScroll = element.scrollHeight - element.clientHeight;
        if (maxScroll <= 0) return false;

        const nextScroll = Math.min(maxScroll, Math.max(0, element.scrollTop + deltaY));
        if (nextScroll === element.scrollTop) return false;

        element.scrollTop = nextScroll;
        return true;
    };

    sidebar.classList.add('no-transition');

    getStoredOpenSectionIds().forEach((sectionId) => {
        const section = document.getElementById(sectionId);
        if (!section || !sidebar.contains(section)) return;

        section.classList.add('show');
        section.style.height = '';
        syncTrigger(section, true);
    });

    collapses.forEach((section) => {
        syncTrigger(section, section.classList.contains('show'));
    });
    persistOpenSections();

    const savedScroll = parseInt(storageGet(scrollKey) || '', 10);
    if (!Number.isNaN(savedScroll)) {
        sidebar.scrollTop = savedScroll;
    }

    requestAnimationFrame(() => {
        sidebar.classList.remove('no-transition');
    });

    sidebar.addEventListener('show.bs.collapse', (event) => {
        if (!event.target.classList.contains('nav-collapse') && !event.target.classList.contains('nav-subcollapse')) return;
        preserveScrollPosition();
        syncTrigger(event.target, true);
    });

    sidebar.addEventListener('shown.bs.collapse', (event) => {
        if (!event.target.classList.contains('nav-collapse') && !event.target.classList.contains('nav-subcollapse')) return;
        syncTrigger(event.target, true);
        persistOpenSections();
        preserveScrollPosition();

        if (event.target.id === 'nav-ca-gst' || event.target.classList.contains('nav-subcollapse-scroll')) {
            event.target.querySelector('.nav-link.active')?.scrollIntoView({ block: 'nearest' });
        }
    });

    sidebar.addEventListener('hide.bs.collapse', (event) => {
        if (!event.target.classList.contains('nav-collapse') && !event.target.classList.contains('nav-subcollapse')) return;
        preserveScrollPosition();
        syncTrigger(event.target, false);
    });

    sidebar.addEventListener('hidden.bs.collapse', (event) => {
        if (!event.target.classList.contains('nav-collapse') && !event.target.classList.contains('nav-subcollapse')) return;
        syncTrigger(event.target, false);
        persistOpenSections();
        preserveScrollPosition();
    });

    let scrollSaveQueued = false;
    sidebar.addEventListener('scroll', () => {
        if (scrollSaveQueued) return;

        scrollSaveQueued = true;
        requestAnimationFrame(() => {
            storageSet(scrollKey, String(sidebar.scrollTop));
            scrollSaveQueued = false;
        });
    }, { passive: true });

    sidebar.addEventListener('wheel', (event) => {
        const scrollableSubmenu = event.target.closest?.('.nav-subcollapse.show');
        if (scrollableSubmenu && sidebar.contains(scrollableSubmenu) && scrollElementByWheel(scrollableSubmenu, event.deltaY)) {
            event.preventDefault();
            return;
        }

        if (scrollElementByWheel(sidebar, event.deltaY)) {
            event.preventDefault();
        }
    }, { passive: false });

    const toggleBtn = document.getElementById('sidebarToggle');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', (event) => {
            event.preventDefault();
            sidebar.classList.toggle('show');
        });
    }

    const activeLink = sidebar.querySelector('.nav-link.active');
    if (activeLink) {
        activeLink.closest('.nav-collapse')?.classList.add('active-menu-parent');
    }
});
