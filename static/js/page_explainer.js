(function () {
  "use strict";

  const ready = (callback) => {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", callback, { once: true });
    } else {
      callback();
    }
  };

  const normalizePath = (value) => {
    if (!value) return "";
    try {
      const path = new URL(value, window.location.origin).pathname;
      return path.length > 1 ? path.replace(/\/+$/, "") : path;
    } catch (error) {
      const fallback = String(value).split("?")[0].split("#")[0];
      return fallback.length > 1 ? fallback.replace(/\/+$/, "") : fallback;
    }
  };

  const escapeHtml = (value) => String(value || "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[char]));

  const storage = {
    get(key) {
      try {
        return window.localStorage.getItem(key);
      } catch (error) {
        return null;
      }
    },
    set(key, value) {
      try {
        window.localStorage.setItem(key, value);
      } catch (error) {
        // Private browsing or locked-down browsers may block localStorage.
      }
    }
  };

  const getGuideItems = () => {
    const groups = Array.isArray(window.AKSHAYA_MENU_GUIDE) ? window.AKSHAYA_MENU_GUIDE : [];
    return groups.flatMap((group) => (group.items || []).map((item) => ({
      ...item,
      category: group.category,
      icon: group.icon,
      path: normalizePath(item.url)
    }))).filter((item) => item.path && item.title && item.text);
  };

  const findGuideForCurrentPage = () => {
    const currentPath = normalizePath(window.location.pathname);
    const items = getGuideItems();
    const exact = items.find((item) => item.path === currentPath);
    if (exact) return exact;

    return items
      .filter((item) => item.path !== "/" && currentPath.startsWith(`${item.path}/`))
      .sort((a, b) => b.path.length - a.path.length)[0] || null;
  };

  const isVisible = (element) => {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };

  const findMenuLink = (item) => {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar) return null;

    const links = Array.from(sidebar.querySelectorAll(".nav-link[href]"));
    const exact = links.find((link) => normalizePath(link.getAttribute("href")) === item.path);
    if (exact) return exact;

    const active = sidebar.querySelector(".nav-link.active[href]");
    if (active && normalizePath(active.getAttribute("href")) === item.path) return active;

    return null;
  };

  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

  const positionPanel = (panel, anchor) => {
    const useFallback = () => {
      panel.classList.add("page-explainer--fallback");
      panel.style.removeProperty("--page-explainer-left");
      panel.style.removeProperty("--page-explainer-top");
      panel.style.removeProperty("--page-explainer-arrow-top");
    };

    if (!anchor || !isVisible(anchor) || window.matchMedia("(max-width: 767.98px)").matches) {
      useFallback();
      return;
    }

    panel.classList.remove("page-explainer--fallback");
    anchor.scrollIntoView({ block: "nearest", inline: "nearest" });

    const viewportPadding = 12;
    const gap = 12;
    const anchorRect = anchor.getBoundingClientRect();
    const panelWidth = panel.offsetWidth || 360;
    const panelHeight = panel.offsetHeight || 140;
    const left = clamp(
      anchorRect.right + gap,
      viewportPadding,
      window.innerWidth - panelWidth - viewportPadding
    );
    const top = clamp(
      anchorRect.top + (anchorRect.height / 2) - (panelHeight / 2),
      viewportPadding,
      window.innerHeight - panelHeight - viewportPadding
    );
    const arrowTop = clamp(
      anchorRect.top + (anchorRect.height / 2) - top - 6,
      12,
      panelHeight - 18
    );

    panel.style.setProperty("--page-explainer-left", `${Math.round(left)}px`);
    panel.style.setProperty("--page-explainer-top", `${Math.round(top)}px`);
    panel.style.setProperty("--page-explainer-arrow-top", `${Math.round(arrowTop)}px`);
  };

  const setActiveGuideCategory = (category) => {
    document.querySelectorAll("#helpCategoryList .list-group-item").forEach((button) => {
      button.classList.toggle("active", button.dataset.category === category);
    });
  };

  const openGuide = (item) => {
    const modalEl = document.getElementById("helpCenterModal");
    const input = document.getElementById("helpSearchInput");
    if (!modalEl || !window.bootstrap || !window.renderHelp) return;

    const applyTopicFilter = () => {
      setActiveGuideCategory(item.category);
      if (input) input.value = item.title;
      window.renderHelp(item.title, item.category);
    };

    if (modalEl.classList.contains("show")) {
      applyTopicFilter();
    } else {
      modalEl.addEventListener("shown.bs.modal", applyTopicFilter, { once: true });
      window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }
  };

  const showExplainer = (item) => {
    if (document.querySelector(".page-explainer")) return;

    const key = `akshaya_explainer_seen:${item.path}`;
    if (storage.get(key) === "1") return;

    const anchor = findMenuLink(item);
    if (anchor) anchor.classList.add("page-explainer-target");

    const panel = document.createElement("section");
    panel.className = "page-explainer";
    panel.setAttribute("role", "status");
    panel.setAttribute("aria-live", "polite");
    panel.innerHTML = `
      <div class="page-explainer__body">
        <div class="page-explainer__label">${escapeHtml(item.category)}</div>
        <h2 class="page-explainer__title">${escapeHtml(item.title)}</h2>
        <p class="page-explainer__text">${escapeHtml(item.text)}</p>
      </div>
      <div class="page-explainer__actions">
        <button type="button" class="btn btn-xs btn-outline-primary page-explainer__guide">
          <i class="bi bi-journal-text me-1"></i> Guide
        </button>
        <button type="button" class="btn btn-sm btn-light border page-explainer__close" aria-label="Close explainer">
          <i class="bi bi-x-lg"></i>
        </button>
      </div>
    `;

    const sidebar = document.getElementById("sidebar");
    let frame = null;
    let isClosing = false;
    const schedulePosition = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = null;
        positionPanel(panel, anchor);
      });
    };

    const cleanup = () => {
      if (frame) window.cancelAnimationFrame(frame);
      document.removeEventListener("show.bs.modal", closeForHelpCenter);
      window.removeEventListener("resize", schedulePosition);
      sidebar?.removeEventListener("scroll", schedulePosition);
      anchor?.classList.remove("page-explainer-target");
    };

    const dismiss = () => {
      if (isClosing) return;
      isClosing = true;
      storage.set(key, "1");
      cleanup();
      panel.classList.remove("show");
      window.setTimeout(() => panel.remove(), 180);
    };

    const closeNow = () => {
      if (isClosing) return;
      isClosing = true;
      storage.set(key, "1");
      cleanup();
      panel.remove();
    };

    function closeForHelpCenter(event) {
      if (event.target?.id === "helpCenterModal") closeNow();
    }

    panel.querySelector(".page-explainer__close").addEventListener("click", dismiss);
    panel.querySelector(".page-explainer__guide").addEventListener("click", () => {
      closeNow();
      openGuide(item);
    });

    document.body.appendChild(panel);
    positionPanel(panel, anchor);
    document.addEventListener("show.bs.modal", closeForHelpCenter);
    window.addEventListener("resize", schedulePosition);
    sidebar?.addEventListener("scroll", schedulePosition, { passive: true });
    window.requestAnimationFrame(() => panel.classList.add("show"));
  };

  ready(() => {
    window.setTimeout(() => {
      const item = findGuideForCurrentPage();
      if (item) showExplainer(item);
    }, 0);
  });
}());
