"""
Element discovery JS for open-typesafe-camoufox — the visibility backbone of the
video-agent pattern: "find where to act, circle it, act on it."

Probes the live DOM for actionable elements (inputs, buttons, links) in
reading order, tags each with a stable per-page-load selector
([data-jev="<idx>"]), and reports a viewport-normalized center the planner
can use to aim the humanized cursor.

Called from JevCapability.find_elements() on every step so the map always
matches the page the cursor is on (navigation-invalidated tags refresh).
"""

ELEMENT_PROBE_JS = """
() => {
  const vw = window.innerWidth || 1280;
  const vh = window.innerHeight || 800;
  const sel = 'input, button, [role="button"], select, textarea, a[href]';
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
    // Links with no own text (icon/title wrapped in headings): fall back to
    // the nearest heading text so results stay choosable instead of "?".
    if (tag === 'a' && !elText) {
      const h = el.querySelector('h1,h2,h3') || el.closest('h1,h2,h3');
      if (h) elText = (h.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    }
    raw.push({
      el: el,
      doc_top: Math.round(docTop),
      kind: tag === 'a' ? 'link' : tag,
      type: elType,
      id: elId,
      label: elLabel,
      placeholder: elPlaceholder,
      text: elText,
      value: elVal,
      value_len: elVal.length,
      cx: Math.round((r.left + r.width / 2) / vw * 1000) / 1000,
      cy: Math.round((r.top + r.height / 2) / vh * 1000) / 1000,
    });
  });
  raw.sort((a, b) => a.doc_top - b.doc_top);
  const out = raw.map((o, i) => {
    o.el.setAttribute('data-jev', String(i));
    return {
      idx: i,
      sel: '[data-jev="' + i + '"]',
      kind: o.kind,
      type: o.type,
      id: o.id,
      label: o.label,
      placeholder: o.placeholder,
      text: o.text,
      value: o.value,
      value_len: o.value_len,
      doc_top: o.doc_top,
      cx: o.cx,
      cy: o.cy,
    };
  });
  return out.slice(0, 40);
}
"""