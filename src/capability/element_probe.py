"""
Element discovery JS for open-typesafe-camoufox — the visibility backbone:
"find where to act, circle it, act on it."

Probes the live DOM for actionable elements (inputs, buttons, links) in
reading order and reports viewport-px bounding boxes plus a normalized
center the planner uses to aim the humanized cursor.

Stealth rule: the probe NEVER mutates the DOM (no setAttribute, no markers).
Refs (e0, e1, ...) are ephemeral handles assigned Python-side per probe and
valid for that probe only — the playwright-cli contract. Called from
JevCapability.find_elements() on every step so the map always matches the
page the cursor is on.
"""

ELEMENT_PROBE_JS = """
() => {
  const vw = window.innerWidth || 1280;
  const vh = window.innerHeight || 800;
  const sel = 'input, button, [role="button"], select, textarea, a[href]';
  // Read-only durable selector for later resolution (generate-locator
  // pattern). Pure query — NEVER writes to the DOM (stealth). Anchors
  // prefer href (stable across re-renders); the final candidate is
  // re-queried to confirm it still addresses this tag, else ''.
  function genSel(el) {
    try {
      const tag = (el.tagName || '').toLowerCase();
      const uniq = (s) => {
        try { return document.querySelectorAll(s).length === 1; } catch (e) { return false; }
      };
      const sameTag = (s) => {
        try {
          const hit = document.querySelector(s);
          return hit && (hit.tagName || '').toLowerCase() === tag;
        } catch (e) { return false; }
      };
      const clean = (v, n) => String(v || '').slice(0, n).replace(/["\\\\]/g, '');
      if (el.id && /^[A-Za-z][\\w:.-]*$/.test(el.id)) {
        const s = '#' + ((window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id);
        if (uniq(s)) return s;
      }
      if (tag === 'a' && el.getAttribute) {
        const href = clean(el.getAttribute('href'), 120);
        if (href) {
          const s = 'a[href="' + href + '"]';
          if (uniq(s) && sameTag(s)) return s;
        }
      }
      const name = el.getAttribute ? el.getAttribute('name') : '';
      if (name && /^[\\w:.-]+$/.test(name)) {
        const s = tag + '[name="' + name + '"]';
        if (uniq(s) && sameTag(s)) return s;
      }
      const aria = el.getAttribute ? String(el.getAttribute('aria-label') || '').trim() : '';
      if (aria) {
        const s = tag + '[aria-label="' + clean(aria, 40) + '"]';
        if (uniq(s) && sameTag(s)) return s;
      }
      const path = [];
      let node = el;
      for (let d = 0; d < 6 && node && node !== document.body && node !== document.documentElement; d++) {
        const t = (node.tagName || '').toLowerCase();
        let nth = 1, sib = node;
        while ((sib = sib.previousElementSibling)) {
          if ((sib.tagName || '').toLowerCase() === t) nth++;
        }
        path.unshift(t + ':nth-of-type(' + nth + ')');
        node = node.parentElement;
      }
      const s = path.join(' > ');
      return sameTag(s) ? s : '';
    } catch (e) { return ''; }
  }
  const nodes = Array.from(document.querySelectorAll(sel));
  const raw = [];
  nodes.forEach((el, i) => {
    const r = el.getBoundingClientRect();
    // Skip hidden / zero-sized / off-page elements the cursor could never reach.
    if (r.width <= 2 || r.height <= 2) return;
    const docTop = r.top + (window.scrollY || 0);
    if (docTop < -2000) return;
    const tag = (el.tagName || '').toLowerCase();
    let elId = '';
    let elLabel = '';
    let elPlaceholder = '';
    let elType = '';
    let elText = (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    if (el.id) elId = String(el.id);
    if (el.getAttribute) {
      elType = String(el.getAttribute('type') || '');
      elPlaceholder = String(el.getAttribute('placeholder') || '');
      const aria = el.getAttribute('aria-label');
      if (aria) elLabel = String(aria).slice(0, 40);
    }
    if (!elId && el.name) elId = 'name:' + String(el.name);
    // <label for="id"> association
    if (elId && !elId.startsWith('name:') && !elLabel) {
      const lbl = document.querySelector('label[for="' + elId + '"]');
      if (lbl) elLabel = (lbl.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    }
    if (tag === 'a') {
      elText = elText || (el.getAttribute ? String(el.getAttribute('title') || '') : '');
    }
    // Inputs: expose the live value so the decider sees filled vs empty.
    let elVal = '';
    if ((tag === 'input' || tag === 'textarea') && typeof el.value === 'string') {
      elVal = el.value.slice(0, 80);
    }
    // Links: capture href (resolved to absolute at parse time) so the
    // runner can mark already-visited targets in the criteria.
    let elHref = '';
    if (tag === 'a' && el.getAttribute) {
      elHref = String(el.getAttribute('href') || '').slice(0, 160);
    }
    // Region: nearest landmark (header/nav/main/footer + ARIA roles) so the
    // decider can tell page chrome from main content. Research results live
    // in main; the search box and nav links live in header/nav.
    let elRegion = '';
    try {
      const landmark = el.closest('header,footer,main,nav,[role="banner"],[role="navigation"],[role="main"],[role="contentinfo"]');
      if (landmark) {
        const lt = (landmark.tagName || '').toLowerCase();
        const lr = landmark.getAttribute ? String(landmark.getAttribute('role') || '') : '';
        elRegion = (lr || lt).slice(0, 24);
      }
    } catch (e) { elRegion = ''; }
    // Links with no own text (icon/title wrapped in headings): fall back to
    // the nearest heading text so results stay choosable instead of "?".
    if (tag === 'a' && !elText) {
      const h = el.querySelector('h1,h2,h3') || el.closest('h1,h2,h3');
      if (h) elText = (h.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    }
    raw.push({
      doc_top: Math.round(docTop),
      kind: tag === 'a' ? 'link' : tag,
      type: elType,
      id: elId,
      label: elLabel,
      durable: genSel(el),
      placeholder: elPlaceholder,
      text: elText,
      value: elVal,
      value_len: elVal.length,
      href: elHref,
      region: elRegion,
      box: [Math.round(r.left / vw * 10000) / 10000,
            Math.round(r.top / vh * 10000) / 10000,
            Math.round(r.width / vw * 10000) / 10000,
            Math.round(r.height / vh * 10000) / 10000],
      cx: Math.round((r.left + r.width / 2) / vw * 1000) / 1000,
      cy: Math.round((r.top + r.height / 2) / vh * 1000) / 1000,
    });
  });
  raw.sort((a, b) => a.doc_top - b.doc_top);
  // Ephemeral refs (e0, e1, ...): assigned per probe, valid for this probe
  // only. No DOM markers — the page is never touched (stealth).
  const out = raw.map((o, i) => {
    o.ref = 'e' + i;
    return o;
  });
  return out;
}
"""


RESOLVE_JS = """
(sel) => {
  // Read-only single-node lookup for a probe-generated durable selector.
  // Returns box (CSS px) + identity for the stale cross-check, or null.
  // Never writes to the DOM.
  let el = null;
  try { el = document.querySelector(sel); } catch (e) { return null; }
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const tag = (el.tagName || '').toLowerCase();
  const get = (a) => (el.getAttribute ? String(el.getAttribute(a) || '') : '');
  return {
    box: [r.left, r.top, r.width, r.height],
    kind: tag === 'a' ? 'link' : tag,
    label: get('aria-label').slice(0, 40),
    text: ((el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40)),
    placeholder: get('placeholder'),
    id: el.id || '',
  };
}
"""