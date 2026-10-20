/* ==========================================================================
   Aggregate - web client
   Type-ahead search, result rendering, filtering and lightbox zoom.
   ========================================================================== */
(function () {
  "use strict";

  /* ---------------------------------------------------------------------
     Helpers
     --------------------------------------------------------------------- */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const escapeHtml = (value) =>
    String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    }[ch]));

  const formatInt = (value) => {
    const n = Number(value || 0);
    return n.toLocaleString("en-IN");
  };

  const formatPrice = (value) => {
    const n = Number(value || 0);
    return "₹" + n.toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  };

  const debounce = (fn, wait) => {
    let timer;
    return function () {
      const args = arguments;
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait);
    };
  };

  const config = (() => {
    const node = $("#app-config");
    if (!node) return {};
    try {
      return JSON.parse(node.textContent) || {};
    } catch (err) {
      console.error("Bad app config", err);
      return {};
    }
  })();

  const state = {
    query: config.query || "",
    category: config.category || "",
    vendor: config.vendor || "",
    inStock: !!config.inStock,
    specFilters: {},
    requestId: 0,
    page: Math.max(1, parseInt(config.page, 10) || 1),
  };

  const resetPage = () => {
    state.page = 1;
  };

  /* ---------------------------------------------------------------------
     Spec filters (voltage, rpm, capacity_mah, ...)
     These come straight from the taxonomy: a category either declares
     filterable attributes or it does not.
     --------------------------------------------------------------------- */
  const specFilterDefs = () => {
    const cat = (config.categories || []).find((c) => c.slug === state.category);
    return (cat && cat.filters) || [];
  };

  const renderSpecFilters = () => {
    const group = $("[data-spec-filters]");
    const list = $("[data-spec-filter-list]");
    if (!group || !list) return;

    const defs = specFilterDefs();
    group.hidden = defs.length === 0;
    if (!defs.length) {
      list.innerHTML = "";
      return;
    }

    list.innerHTML = defs
      .map((def) => {
        const unit = def.unit ? `<span class="spec-filter__unit">${escapeHtml(def.unit)}</span>` : "";
        const label = `<span class="spec-filter__label">${escapeHtml(def.display_name || def.key)}${unit}</span>`;
        const current = state.specFilters[def.key] || {};

        if (def.type === "enum") {
          const values = def.values || [];
          const opts = values
            .map(
              (v) =>
                `<option value="${escapeHtml(v)}" ${current.eq === v ? "selected" : ""}>${escapeHtml(v)}</option>`
            )
            .join("");
          return (
            `<div class="spec-filter" data-spec-key="${escapeHtml(def.key)}">` +
            label +
            `<select class="spec-filter__input" data-spec-eq="${escapeHtml(def.key)}">` +
            `<option value="">Any</option>${opts}</select>` +
            `</div>`
          );
        }

        const min = current.gte != null ? current.gte : "";
        const max = current.lte != null ? current.lte : "";
        return (
          `<div class="spec-filter" data-spec-key="${escapeHtml(def.key)}">` +
          label +
          `<div class="spec-filter__row">` +
          `<input class="spec-filter__input" type="number" step="any" placeholder="min" ` +
          `data-spec-min="${escapeHtml(def.key)}" value="${escapeHtml(min)}">` +
          `<span class="spec-filter__sep">to</span>` +
          `<input class="spec-filter__input" type="number" step="any" placeholder="max" ` +
          `data-spec-max="${escapeHtml(def.key)}" value="${escapeHtml(max)}">` +
          `</div></div>`
        );
      })
      .join("");

    const reset = $("[data-clear-spec-filters]");
    if (reset) reset.hidden = Object.keys(state.specFilters).length === 0;
  };

  const collectSpecFilters = () => {
    const out = {};
    $$("[data-spec-eq]").forEach((el) => {
      const key = el.getAttribute("data-spec-eq");
      const value = el.value;
      if (value !== "") out[key] = { eq: value };
    });
    $$("[data-spec-min]").forEach((el) => {
      const key = el.getAttribute("data-spec-min");
      const value = el.value;
      if (value === "") return;
      const parsed = Number(value);
      if (Number.isNaN(parsed)) return;
      out[key] = out[key] || {};
      out[key].gte = parsed;
    });
    $$("[data-spec-max]").forEach((el) => {
      const key = el.getAttribute("data-spec-max");
      const value = el.value;
      if (value === "") return;
      const parsed = Number(value);
      if (Number.isNaN(parsed)) return;
      out[key] = out[key] || {};
      out[key].lte = parsed;
    });
    return out;
  };

  /* ---------------------------------------------------------------------
     Icon markup (mirrors the server-side macro)
     --------------------------------------------------------------------- */
  const ICON_PATHS = {
    search:
      '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    package:
      '<path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    layers:
      '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    chip:
      '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>',
    tag: '<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5" fill="currentColor"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    arrowRight: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    trending: '<path d="M16 7h6v6"/><path d="m22 7-8.5 8.5-5-5L2 17"/>',
    activity:
      '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
    alert:
      '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    layout:
      '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M9 3v18"/><path d="M15 3v18"/>',
    store:
      '<path d="m2 7 4.41-4.41A2 2 0 0 1 7.83 2h8.34a2 2 0 0 1 1.42.59L22 7"/><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><path d="M15 22v-4a2 2 0 0 0-2-2h-2a2 2 0 0 0-2 2v4"/><path d="M2 7h20"/><path d="M22 7v3a2 2 0 0 1-2 2a4 4 0 0 1-8 0 4 4 0 0 1-8 0 2 2 0 0 1-2-2V7"/>',
    database:
      '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
    bolt: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    warning:
      '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
    minus: '<path d="M5 12h14"/>',
    chevronRight: '<path d="m9 18 6-6-6-6"/>',
    grid: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
  };

  const icon = (name, size) => {
    const path = ICON_PATHS[name] || ICON_PATHS.package;
    return (
      '<svg class="icon" width="' +
      (size || 16) +
      '" height="' +
      (size || 16) +
      '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      path +
      "</svg>"
    );
  };

  const vendorClass = (vendor) => {
    const v = String(vendor || "").toLowerCase();
    if (v === "robu") return "vendor--robu";
    if (v === "quartzcomponents") return "vendor--quartzcomponents";
    if (v === "makerbazar") return "vendor--makerbazar";
    if (v === "robokits") return "vendor--robokits";
    if (v === "rees52") return "vendor--rees52";
    return "vendor--default";
  };

  const categoryMeta = (slug) => {
    const found = (config.categories || []).find((c) => c.slug === slug);
    return found || null;
  };

  /* ---------------------------------------------------------------------
     State panels
     --------------------------------------------------------------------- */
  const states = {};
  $$("[data-state]").forEach((node) => {
    states[node.getAttribute("data-state")] = node;
  });

  const showState = (name) => {
    Object.keys(states).forEach((key) => {
      if (states[key]) states[key].hidden = key !== name;
    });
  };

  /* ---------------------------------------------------------------------
     Landing / results view switching
     --------------------------------------------------------------------- */
  const setView = (name) => {
    const landing = $('[data-view="landing"]');
    const results = $('[data-view="results"]');
    if (landing) landing.hidden = name !== "landing";
    if (results) results.hidden = name !== "results";
    document.body.classList.toggle("has-results", name === "results");
  };

  const currentView = () => {
    const landing = $('[data-view="landing"]');
    return landing && !landing.hidden ? "landing" : "results";
  };

  const focusVisibleSearch = () => {
    const view = currentView();
    const root = view === "results" ? $('[data-view="results"]') : $('[data-view="landing"]');
    const input = root ? $("[data-search-input]", root) : null;
    if (input) {
      input.value = state.query;
      const clear = $("[data-search-clear]", root);
      if (clear) clear.classList.toggle("is-visible", state.query.length > 0);
    }
  };

  /* ---------------------------------------------------------------------
     URL sync
     --------------------------------------------------------------------- */
  const syncUrl = (push) => {
    const params = new URLSearchParams();
    if (state.query) params.set("q", state.query);
    if (state.category) params.set("category", state.category);
    if (state.vendor) params.set("vendor", state.vendor);
    if (state.inStock) params.set("in_stock", "1");
    if (state.page > 1) params.set("page", String(state.page));
    if (Object.keys(state.specFilters).length) {
      params.set("spec_filters", JSON.stringify(state.specFilters));
    }
    const url = params.toString() ? "/?" + params.toString() : "/";
    if (push) history.pushState({ ...state }, "", url);
    else history.replaceState({ ...state }, "", url);
  };

  const readUrl = () => {
    const params = new URLSearchParams(window.location.search);
    let specFilters = {};
    const raw = params.get("spec_filters");
    if (raw) {
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") specFilters = parsed;
      } catch (err) {
        specFilters = {};
      }
    }
    return {
      query: (params.get("q") || "").trim(),
      category: params.get("category") || "",
      vendor: params.get("vendor") || "",
      inStock: params.get("in_stock") === "1",
      page: Math.max(1, parseInt(params.get("page") || "1", 10) || 1),
      specFilters: specFilters,
    };
  };

  /* ---------------------------------------------------------------------
     Suggestions
     --------------------------------------------------------------------- */
  const suggestRoots = $$("[data-search-root]");

  const closeSuggest = (root) => {
    const panel = $("[data-suggest]", root);
    if (!panel) return;
    panel.classList.remove("is-open");
    const input = $("[data-search-input]", root);
    if (input) input.setAttribute("aria-expanded", "false");
  };

  const renderSuggestions = (root, items) => {
    const list = $("[data-suggest-list]", root);
    const footer = $("[data-suggest-footer]", root);
    const panel = $("[data-suggest]", root);
    if (!list || !panel) return;

    if (!items.length) {
      list.innerHTML =
        '<p class="suggest__empty">No matches yet. Try a part number like <strong>ESP32</strong> or <strong>LM2596</strong>.</p>';
      if (footer) footer.hidden = true;
      panel.classList.add("is-open");
      return;
    }

    const typeMeta = {
      mpn: { icon: "chip", label: "Part number" },
      product: { icon: "package", label: "Product" },
      category: { icon: "grid", label: "Category" },
    };

    list.innerHTML = items
      .map((item, index) => {
        const meta = typeMeta[item.type] || typeMeta.product;
        const meta2 = [];
        if (item.hint && item.type !== "category") {
          meta2.push("<span>" + escapeHtml(item.hint) + "</span>");
        }
        if (item.vendor && item.type !== "category") {
          meta2.push("<span>" + escapeHtml(item.vendor) + "</span>");
        }
        if (item.type === "category") {
          meta2.push("<span>" + escapeHtml(item.hint || "") + "</span>");
        }
        let aside = "";
        if (item.type === "mpn" && item.available && item.price) {
          aside = escapeHtml(formatPrice(item.price));
        } else if (item.type === "category") {
          const cat = categoryMeta(item.value);
          aside = cat ? formatInt(cat.count) + " parts" : "";
        }
        return (
          '<button type="button" class="suggest__item" role="option" data-suggest-item data-suggest-index="' +
          index +
          '" data-suggest-type="' +
          escapeHtml(item.type) +
          '" data-suggest-value="' +
          escapeHtml(item.value) +
          '">' +
          '<span class="suggest__icon">' +
          icon(meta.icon, 16) +
          "</span>" +
          '<span class="suggest__body">' +
          '<span class="suggest__label">' +
          escapeHtml(item.label) +
          "</span>" +
          '<span class="suggest__meta">' +
          '<code>' +
          escapeHtml(meta.label) +
          "</code>" +
          meta2.join("") +
          "</span>" +
          "</span>" +
          '<span class="suggest__aside">' +
          aside +
          "</span>" +
          "</button>"
        );
      })
      .join("");

    if (footer) footer.hidden = false;
    panel.classList.add("is-open");
  };

  const fetchSuggestions = async (root, term) => {
    const trimmed = term.trim();
    if (trimmed.length < 2) {
      closeSuggest(root);
      return;
    }
    try {
      const res = await fetch(
        "/api/suggest?q=" + encodeURIComponent(trimmed) + "&limit=8",
        { headers: { Accept: "application/json" } }
      );
      if (!res.ok) return;
      const data = await res.json();
      renderSuggestions(root, data.suggestions || []);
    } catch (err) {
      /* suggestions are best effort */
    }
  };

  const applySuggestion = (root, item) => {
    if (!item) return;
    if (item.type === "category") {
      state.query = "";
      state.category = item.value;
      state.vendor = "";
      resetPage();
      closeSuggest(root);
      syncUrl(true);
      renderFilters();
      performSearch("");
      return;
    }
    const input = $("[data-search-input]", root);
    if (input) input.value = item.value;
    state.query = item.value;
    resetPage();
    closeSuggest(root);
    syncUrl(true);
    performSearch(item.value);
  };

  suggestRoots.forEach((root) => {
    const input = $("[data-search-input]", root);
    const clear = $("[data-search-clear]", root);
    if (!input) return;

    const run = debounce(() => fetchSuggestions(root, input.value), 130);
    input.addEventListener("input", () => {
      if (clear) clear.classList.toggle("is-visible", input.value.length > 0);
      run();
    });

    input.addEventListener("focus", () => {
      if (input.value.trim().length >= 2) fetchSuggestions(root, input.value);
    });

    input.addEventListener("keydown", (event) => {
      const panel = $("[data-suggest]", root);
      const items = $$("[data-suggest-item]", panel);
      if (!panel || !panel.classList.contains("is-open") || !items.length) return;

      const current = items.findIndex((el) => el.classList.contains("is-active"));
      if (event.key === "ArrowDown") {
        event.preventDefault();
        const next = current < 0 ? 0 : (current + 1) % items.length;
        items.forEach((el, i) => el.classList.toggle("is-active", i === next));
        items[next].scrollIntoView({ block: "nearest" });
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        const next = current <= 0 ? items.length - 1 : current - 1;
        items.forEach((el, i) => el.classList.toggle("is-active", i === next));
        items[next].scrollIntoView({ block: "nearest" });
      } else if (event.key === "Enter" && current >= 0) {
        event.preventDefault();
        const el = items[current];
        applySuggestion(root, {
          type: el.getAttribute("data-suggest-type"),
          value: el.getAttribute("data-suggest-value"),
        });
      } else if (event.key === "Escape") {
        closeSuggest(root);
      }
    });

    if (clear) {
      clear.addEventListener("click", () => {
        input.value = "";
        clear.classList.remove("is-visible");
        closeSuggest(root);
        input.focus();
      });
    }

    const panel = $("[data-suggest]", root);
    if (panel) {
      panel.addEventListener("click", (event) => {
        const el = event.target.closest("[data-suggest-item]");
        if (!el) return;
        applySuggestion(root, {
          type: el.getAttribute("data-suggest-type"),
          value: el.getAttribute("data-suggest-value"),
        });
      });
    }

    const form = $("[data-search-form]", root);
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const value = input.value.trim();
        closeSuggest(root);
        if (!value) {
          state.query = "";
          resetPage();
          syncUrl(true);
          renderFilters();
          performSearch("");
          return;
        }
        state.query = value;
        resetPage();
        syncUrl(true);
        performSearch(value);
      });
    }
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-search-root]")) {
      suggestRoots.forEach(closeSuggest);
    }
  });

  /* ---------------------------------------------------------------------
     Result rendering
     --------------------------------------------------------------------- */
  const cardsContainer = $("#cards-container");

  const stockTag = (available) =>
    available
      ? '<span class="tag tag--ok">' + icon("check", 12) + "In stock</span>"
      : '<span class="tag tag--off">Out of stock</span>';

  const thumbHtml = (url, title, cls) => {
    if (!url || !String(url).trim()) {
      return (
        '<div class="' +
        (cls || "card__thumb") +
        ' card__thumb--empty">' +
        icon("package", 20) +
        "</div>"
      );
    }
    return (
      '<div class="' +
      (cls || "card__thumb") +
      ' zoom-trigger" data-img-url="' +
      escapeHtml(url) +
      '" data-img-caption="' +
      escapeHtml(title) +
      '" title="Click to zoom">' +
      '<img src="' +
      escapeHtml(url) +
      '" alt="' +
      escapeHtml(title) +
      '" loading="lazy" onerror="this.parentElement.classList.add(\'card__thumb--empty\');this.remove();">' +
      "</div>"
    );
  };

  const trackAttrs = (offer, group, query) =>
    'class="btn btn--sm track-view-btn" data-query="' +
    escapeHtml(query) +
    '" data-group-id="' +
    escapeHtml(group.variant_key || group.canonical_name || "") +
    '" data-vendor="' +
    escapeHtml(offer.vendor || "") +
    '" data-product-id="' +
    escapeHtml(offer.external_id || offer.sku || "") +
    '"';

  const categoryTag = (slug) => {
    const meta = categoryMeta(slug);
    if (!meta) return "";
    return (
      '<a class="tag" href="/?q=' +
      encodeURIComponent(state.query) +
      "&category=" +
      encodeURIComponent(slug) +
      '">' +
      icon(meta.icon, 12) +
      escapeHtml(meta.label) +
      "</a>"
    );
  };

  const renderSingleCard = (group, offer, query) => {
    const price = Number(offer.price) || 0;
    return (
      '<article class="card">' +
      '<div class="card__head">' +
      thumbHtml(offer.image_url, offer.title) +
      '<div class="card__main">' +
      '<h3 class="card__title">' +
      escapeHtml(offer.title) +
      "</h3>" +
      '<div class="card__tags">' +
      '<span class="vendor ' +
      vendorClass(offer.vendor) +
      '">' +
      escapeHtml(offer.vendor_name || offer.vendor) +
      "</span>" +
      (group.mpn
        ? '<span class="tag tag--mono">' + escapeHtml(group.mpn) + "</span>"
        : "") +
      (offer.category_canonical ? categoryTag(offer.category_canonical) : "") +
      stockTag(offer.available) +
      "</div>" +
      "</div>" +
      "</div>" +
      '<div class="card__foot">' +
      '<div class="card__price"><strong>' +
      formatPrice(price) +
      "</strong></div>" +
      '<a href="' +
      escapeHtml(offer.product_url) +
      '" target="_blank" rel="noopener noreferrer" ' +
      trackAttrs(offer, group, query) +
      '>View ' +
      icon("external", 14) +
      "</a>" +
      "</div>" +
      "</article>"
    );
  };

  const renderComparisonCard = (group, query) => {
    const offers = (group.offers || []).slice().sort((a, b) => {
      const av = a.available ? 0 : 1;
      const bv = b.available ? 0 : 1;
      if (av !== bv) return av - bv;
      return (Number(a.price) || 0) - (Number(b.price) || 0);
    });

    const inStock = offers.filter((o) => o.available && Number(o.price) > 0);
    const best = inStock.length ? inStock[0] : null;

    let warning = "";
    if (inStock.length > 1) {
      const prices = inStock.map((o) => Number(o.price));
      const min = Math.min.apply(null, prices);
      const max = Math.max.apply(null, prices);
      if (max / Math.max(min, 1) > 3.5) {
        warning =
          '<div class="note">' +
          icon("warning", 16) +
          '<div class="note__body"><strong>Wide price spread across vendors</strong>' +
          escapeHtml(formatPrice(min) + " - " + formatPrice(max)) +
          ". These listings may be different variants, pack sizes or accessories - check the specs before buying." +
          "</div></div>";
      }
    }

    const distinct = (group.distinct_titles || []).filter(Boolean);
    const variantNote =
      distinct.length > 1
        ? '<div style="margin-top:6px;font-size:12.5px;color:var(--text-subtle)">Listed as: ' +
          escapeHtml(distinct.slice(0, 3).join(" / ")) +
          "</div>"
        : "";

    const rows = offers
      .map((offer) => {
        const price = Number(offer.price) || 0;
        const isBest = best && offer.available && price === Number(best.price);
        return (
          "<tr>" +
          '<td><span class="vendor ' +
          vendorClass(offer.vendor) +
          '">' +
          escapeHtml(offer.vendor_name || offer.vendor) +
          "</span></td>" +
          "<td>" +
          '<div class="offer-title">' +
          thumbHtml(offer.image_url, offer.title, "offer-thumb") +
          '<span class="offer-title__text" title="' +
          escapeHtml(offer.title) +
          '">' +
          escapeHtml(offer.title) +
          "</span>" +
          "</div></td>" +
          "<td>" +
          stockTag(offer.available) +
          "</td>" +
          '<td class="is-right"><span class="offer-price' +
          (isBest ? " is-best" : "") +
          '">' +
          formatPrice(price) +
          "</span>" +
          (isBest ? ' <span class="tag tag--best">Lowest</span>' : "") +
          "</td>" +
          '<td class="is-right"><a href="' +
          escapeHtml(offer.product_url) +
          '" target="_blank" rel="noopener noreferrer" ' +
          trackAttrs(offer, group, query) +
          ">View " +
          icon("external", 13) +
          "</a></td>" +
          "</tr>"
        );
      })
      .join("");

    return (
      '<article class="card">' +
      '<div class="card__head">' +
      thumbHtml(
        (offers.find((o) => o.image_url && o.image_url.trim()) || {}).image_url,
        group.canonical_name
      ) +
      '<div class="card__main">' +
      '<h3 class="card__title">' +
      escapeHtml(group.canonical_name) +
      "</h3>" +
      '<div class="card__tags">' +
      (group.mpn
        ? '<span class="tag tag--mono">' + escapeHtml(group.mpn) + "</span>"
        : "") +
      (group.category_canonical ? categoryTag(group.category_canonical) : "") +
      '<span class="tag">' +
      offers.length +
      (offers.length === 1 ? " vendor" : " vendors") +
      "</span>" +
      (group.in_stock_count
        ? '<span class="tag tag--ok">' + group.in_stock_count + " in stock</span>"
        : "") +
      "</div>" +
      variantNote +
      "</div>" +
      "</div>" +
      warning +
      '<div class="card__foot" style="padding-bottom:10px">' +
      '<div class="card__meta">' +
      (best
        ? "Lowest price " +
          '<span style="color:var(--text);font-weight:600">' +
          escapeHtml(formatPrice(best.price)) +
          "</span> at " +
          escapeHtml(best.vendor_name || best.vendor)
        : "Currently unavailable at every vendor") +
      "</div>" +
      "</div>" +
      '<table class="offers"><thead><tr>' +
      "<th>Vendor</th><th>Listing</th><th>Stock</th>" +
      '<th class="is-right">Price</th><th class="is-right"></th>' +
      "</tr></thead><tbody>" +
      rows +
      "</tbody></table>" +
      "</article>"
    );
  };

  const renderResults = (data) => {
    const groups = data.comparison_groups || [];
    if (!groups.length || !cardsContainer) return false;

    cardsContainer.innerHTML = groups
      .map((group) => {
        const offers = group.offers || [];
        if (!offers.length) return "";
        if (offers.length === 1) {
          return renderSingleCard(group, offers[0], state.query);
        }
        return renderComparisonCard(group, state.query);
      })
      .join("");

    return true;
  };

  const renderPriceBadge = (refresh) => {
    const badge = $("[data-price-badge]");
    const text = $("[data-price-badge-text]");
    if (!badge || !text) return;

    if (!refresh || refresh.status === "disabled") {
      badge.hidden = true;
      return;
    }
    const labels = {
      live: "Live prices verified",
      partial: "Live prices (partial)",
      refreshing: "Cached prices",
      fresh_cache: "Cached prices (fresh)",
      cached: "Cached prices",
      offline_cooldown: "Offline - cached prices",
      timeout: "Timeout - cached prices",
      error: "Cached prices",
    };
    badge.hidden = false;
    badge.className =
      "badge " +
      (refresh.status === "live" || refresh.status === "partial"
        ? "badge--live"
        : "badge--cached");
    text.textContent =
      (labels[refresh.status] || "Cached prices") +
      (refresh.refreshed_products ? " · " + refresh.refreshed_products : "");
  };

  const renderPager = (data) => {
    const pager = $("[data-pager]");
    const status = $("[data-page-status]");
    const prev = $("[data-page-prev]");
    const next = $("[data-page-next]");
    if (!pager) return;

    const groups = (data.comparison_groups || []).length;
    if (!groups) {
      pager.hidden = true;
      return;
    }

    const page = Math.max(1, Number(data.page) || 1);
    const pages = Number(data.total_pages) || 0;
    if (status) {
      status.textContent =
        pages > 0 && page <= pages ? "Page " + page + " of " + pages : "Page " + page;
    }
    if (prev) prev.disabled = !data.has_prev;
    if (next) next.disabled = !data.has_next;
    pager.hidden = !(data.has_prev || data.has_next);
  };

  const renderResultsMeta = (data) => {
    const title = $("[data-results-title]");
    const sub = $("[data-results-sub]");
    const groups = (data.comparison_groups || []).length;

    if (title) {
      if (state.query) {
        title.innerHTML = "Results for &ldquo;" + escapeHtml(state.query) + "&rdquo;";
      } else {
        const meta = categoryMeta(state.category);
        title.innerHTML = meta
          ? escapeHtml(meta.label)
          : state.vendor
            ? "Browse by vendor"
            : "Catalog";
      }
    }
    if (sub) {
      const parts = [];
      parts.push(
        "<strong>" + formatInt(data.total_results || 0) + "</strong> matching parts"
      );
      if (groups) parts.push("grouped into <strong>" + groups + "</strong> cards");
      if (state.category) {
        const meta = categoryMeta(state.category);
        if (meta) parts.push("in <strong>" + escapeHtml(meta.label) + "</strong>");
      }
      sub.innerHTML = parts.join(" · ");
    }
    renderPriceBadge(data.price_refresh);
  };

  /* ---------------------------------------------------------------------
     Filters
     --------------------------------------------------------------------- */
  const renderFilters = () => {
    $$("[data-category-slug]").forEach((el) => {
      el.classList.toggle("is-active", (el.getAttribute("data-category-slug") || "") === state.category);
    });
    $$("[data-vendor-slug]").forEach((el) => {
      el.classList.toggle("is-active", (el.getAttribute("data-vendor-slug") || "") === state.vendor);
    });
    const inStockBox = $("[data-filter-in-stock]");
    if (inStockBox) inStockBox.checked = state.inStock;

    const container = $("[data-active-filters]");
    if (!container) return;

    const chips = [];
    if (state.category) {
      const meta = categoryMeta(state.category);
      chips.push(
        '<span class="active-filter">' +
          icon(meta ? meta.icon : "grid", 13) +
          escapeHtml(meta ? meta.label : state.category) +
          '<button type="button" class="active-filter__x" data-clear-filter="category" aria-label="Clear category filter">' +
          icon("x", 12) +
          "</button></span>"
      );
    }
    if (state.vendor) {
      const v = (config.vendors || []).find((x) => x.vendor === state.vendor);
      chips.push(
        '<span class="active-filter">' +
          icon("store", 13) +
          escapeHtml(v ? v.vendor_name : state.vendor) +
          '<button type="button" class="active-filter__x" data-clear-filter="vendor" aria-label="Clear vendor filter">' +
          icon("x", 12) +
          "</button></span>"
      );
    }
    if (state.inStock) {
      chips.push(
        '<span class="active-filter">' +
          icon("check", 13) +
          "In stock only" +
          '<button type="button" class="active-filter__x" data-clear-filter="in_stock" aria-label="Clear stock filter">' +
          icon("x", 12) +
          "</button></span>"
      );
    }

    container.innerHTML = chips.join("");
    container.hidden = chips.length === 0;

    const reset = $("[data-clear-filters]");
    if (reset) reset.hidden = chips.length === 0;

    const badge = $("[data-filters-count]");
    if (badge) {
      badge.textContent = chips.length;
      badge.hidden = chips.length === 0;
    }

    renderSpecFilters();
  };

  /* ---------------------------------------------------------------------
     Search
     --------------------------------------------------------------------- */
  const buildSearchUrl = () => {
    const params = new URLSearchParams();
    if (state.query) params.set("q", state.query);
    params.set("source", "local");
    params.set("page", String(state.page));
    params.set("page_size", "20");
    params.set("refresh", "auto");
    if (state.category) params.set("category", state.category);
    if (state.vendor) params.set("vendor", state.vendor);
    if (state.inStock) params.set("in_stock", "1");
    const spec = collectSpecFilters();
    if (Object.keys(spec).length) params.set("spec_filters", JSON.stringify(spec));
    return "/api/search?" + params.toString();
  };

  const hasFilter = () => !!(state.category || state.vendor || state.inStock);

  const performSearch = async (query) => {
    const requestId = ++state.requestId;

    if (query !== undefined) state.query = query;

    if (!state.query && !hasFilter()) {
      setView("landing");
      showState("initial");
      if (cardsContainer) cardsContainer.innerHTML = "";
      renderResultsMeta({ total_results: 0, comparison_groups: [] });
      return;
    }

    setView("results");
    focusVisibleSearch();
    showState("loading");
    try {
      const res = await fetch(buildSearchUrl(), {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      if (requestId !== state.requestId) return;

      const rendered = renderResults(data);
      renderResultsMeta(data);
      renderPager(data);

      if (!rendered) {
        const title = $("[data-empty-title]");
        const text = $("[data-empty-text]");
        if (title) {
          title.textContent = state.query
            ? 'No components found for "' + state.query + '"'
            : "Nothing to show here";
        }
        if (text) {
          text.textContent =
            state.category || state.vendor
              ? "Nothing matches in this filter. Try removing a filter or using a shorter term."
              : "Try a shorter term, a part number, or pick a category above.";
        }
        showState("empty");
        return;
      }
      showState("results");
    } catch (err) {
      if (requestId !== state.requestId) return;
      console.error("Search failed", err);
      const pager = $("[data-pager]");
      if (pager) pager.hidden = true;
      showState("error");
    }
  };

  /* ---------------------------------------------------------------------
     Filter wiring
     --------------------------------------------------------------------- */
  document.addEventListener("click", (event) => {
    const catEl = event.target.closest("[data-category-slug]");
    if (catEl) {
      event.preventDefault();
      const slug = catEl.getAttribute("data-category-slug") || "";
      state.category = state.category === slug && slug ? "" : slug;
      // Spec filters belong to a category: drop them when it changes.
      state.specFilters = {};
      resetPage();
      renderFilters();
      syncUrl(true);
      performSearch();
      return;
    }

    const vendorEl = event.target.closest("[data-vendor-slug]");
    if (vendorEl) {
      event.preventDefault();
      const slug = vendorEl.getAttribute("data-vendor-slug") || "";
      state.vendor = state.vendor === slug ? "" : slug;
      resetPage();
      renderFilters();
      syncUrl(true);
      performSearch();
      return;
    }

    const quickEl = event.target.closest("[data-quick-search]");
    if (quickEl) {
      event.preventDefault();
      const term = (quickEl.getAttribute("data-quick-search") || "").trim();
      if (!term) return;
      state.query = term;
      state.category = "";
      state.vendor = "";
      resetPage();
      renderFilters();
      syncUrl(true);
      performSearch(term);
      return;
    }

    const clearFilter = event.target.closest("[data-clear-filter]");
    if (clearFilter) {
      const kind = clearFilter.getAttribute("data-clear-filter");
      if (kind === "category") state.category = "";
      if (kind === "vendor") state.vendor = "";
      if (kind === "in_stock") state.inStock = false;
      resetPage();
      renderFilters();
      syncUrl(true);
      performSearch();
      return;
    }

    if (event.target.closest("[data-clear-filters]")) {
      state.category = "";
      state.vendor = "";
      state.inStock = false;
      state.specFilters = {};
      resetPage();
      renderFilters();
      syncUrl(true);
      performSearch();
      return;
    }

    if (event.target.closest("[data-retry]")) {
      performSearch();
    }
  });

  const inStockBox = $("[data-filter-in-stock]");
  if (inStockBox) {
    inStockBox.addEventListener("change", () => {
      state.inStock = inStockBox.checked;
      resetPage();
      renderFilters();
      syncUrl(true);
      performSearch();
    });
  }

  const filtersToggle = $("[data-filters-toggle]");
  if (filtersToggle) {
    filtersToggle.addEventListener("click", () => {
      const panel = filtersToggle.closest("[data-filters]");
      if (!panel) return;
      const open = panel.classList.toggle("is-open");
      filtersToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Spec filter inputs: apply on change (and on Enter for the number fields).
  const applySpecFilters = () => {
    state.specFilters = collectSpecFilters();
    resetPage();
    const reset = $("[data-clear-spec-filters]");
    if (reset) reset.hidden = Object.keys(state.specFilters).length === 0;
    syncUrl(true);
    performSearch();
  };

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-spec-eq], [data-spec-min], [data-spec-max]")) {
      applySpecFilters();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (
      event.key === "Enter" &&
      event.target.matches("[data-spec-min], [data-spec-max]")
    ) {
      event.preventDefault();
      applySpecFilters();
    }
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-clear-spec-filters]")) return;
    state.specFilters = {};
    resetPage();
    renderSpecFilters();
    syncUrl(true);
    performSearch();
  });

  document.addEventListener("click", (event) => {
    const prev = event.target.closest("[data-page-prev]");
    const next = event.target.closest("[data-page-next]");
    if (!prev && !next) return;
    event.preventDefault();
    const target = prev ? state.page - 1 : state.page + 1;
    if (target < 1) return;
    state.page = target;
    syncUrl(true);
    performSearch();
    if (cardsContainer) {
      cardsContainer.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });

  /* ---------------------------------------------------------------------
     Click telemetry
     --------------------------------------------------------------------- */
  document.addEventListener("click", (event) => {
    const btn = event.target.closest(".track-view-btn");
    if (!btn) return;
    const payload = {
      query: btn.dataset.query || "",
      group_id: btn.dataset.groupId || "",
      vendor: btn.dataset.vendor || "",
      product_id: btn.dataset.productId || "",
    };
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(
          "/feedback",
          new Blob([JSON.stringify(payload)], { type: "application/json" })
        );
      } else {
        fetch("/feedback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          keepalive: true,
        }).catch(() => {});
      }
    } catch (err) {
      /* telemetry must never block navigation */
    }
  });

  /* ---------------------------------------------------------------------
     Lightbox
     --------------------------------------------------------------------- */
  const lightbox = $("#lightbox");
  const lightboxImg = $("#lightbox-img");
  const lightboxCaption = $("#lightbox-caption");

  const openLightbox = (url, caption) => {
    if (!lightbox || !url) return;
    if (lightboxImg) {
      lightboxImg.src = url;
      lightboxImg.alt = caption || "Product image";
    }
    if (lightboxCaption) lightboxCaption.textContent = caption || "";
    lightbox.classList.add("is-open");
    document.body.style.overflow = "hidden";
  };

  const closeLightbox = () => {
    if (!lightbox) return;
    lightbox.classList.remove("is-open");
    if (lightboxImg) lightboxImg.src = "";
    document.body.style.overflow = "";
  };

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest(".zoom-trigger");
    if (trigger) {
      openLightbox(trigger.dataset.imgUrl, trigger.dataset.imgCaption || "");
      return;
    }
    if (event.target.closest("[data-lightbox-close]")) closeLightbox();
    if (lightbox && event.target === lightbox) closeLightbox();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeLightbox();
  });

  /* ---------------------------------------------------------------------
     Boot
     --------------------------------------------------------------------- */
  window.addEventListener("popstate", () => {
    const params = readUrl();
    state.query = params.query;
    state.category = params.category;
    state.vendor = params.vendor;
    state.inStock = params.inStock;
    state.page = params.page;
    state.specFilters = params.specFilters;
    suggestRoots.forEach((root) => {
      const input = $("[data-search-input]", root);
      if (input) input.value = state.query;
    });
    renderFilters();
    performSearch();
  });

  renderFilters();

  if (state.query || hasFilter()) {
    syncUrl(false);
    performSearch();
  } else {
    showState("initial");
    renderResultsMeta({ total_results: 0, comparison_groups: [] });
  }
})();
