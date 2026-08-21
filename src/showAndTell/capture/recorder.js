(() => {
  // The in-page recorder: observes real user gestures and reports them
  // through the __showAndTellRecord binding with a ranked selector set, AX
  // role/name inference, drag-target resolution, and IME-aware text
  // semantics. showAndTell.capture.runtime injects it into every captured
  // page and frame; the header stays inside the IIFE so the file remains
  // a single evaluable expression.
  if (window.__showAndTellRecorderInstalled) return;
  window.__showAndTellRecorderInstalled = true;
  const binding = window.__showAndTellRecord;
  // Two-phase click identity. Every click is announced immediately as
  // click_begin (identity + authored order — unloseable), then patched one
  // task later by click_settled with final event/control state. The host
  // merges the pair into one persisted click; if navigation destroys this
  // document first, the begin still persists with settlement "unavailable".
  const docToken = Math.random().toString(36).slice(2, 10);
  let clickCounter = 0;
  let captureSequence = 0;
  let activeClick = null;
  let labelForward = null;
  // The last two click transactions in this document, for pairing with a
  // trusted dblclick. Suppressed clicks (label forwards, drag compatibility
  // clicks) never enter the ring: one physical gesture, one entry.
  const recentClicks = [];
  const MODIFIER_KEYS = ["alt", "ctrl", "meta", "shift"];
  // Outcome evidence keeps origin + path only; queries carry tokens.
  const sanitizeUrl = raw => String(raw || "").split(/[?#]/)[0];
  const attrQuote = value => JSON.stringify(String(value));
  // Unique means "resolves to this element": exactly one match, or exactly
  // one VISIBLE match that is this element — SPAs keep hidden twins of
  // dialog controls in the DOM (a hidden form page has the same fields as
  // the quick-entry dialog on top of it).
  const unique = (selector, el) => {
    try {
      const all = [...document.querySelectorAll(selector)];
      if (all.length === 1) return all[0] === el;
      const visible = all.filter(x => x.getClientRects().length > 0);
      return visible.length === 1 && visible[0] === el;
    } catch { return false; }
  };
  const cssPath = el => {
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && parts.length < 12; node = node.parentElement) {
      let part = node.tagName.toLowerCase();
      const siblings = node.parentElement
        ? [...node.parentElement.children].filter(x => x.tagName === node.tagName) : [];
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      parts.unshift(part);
      const candidate = parts.join(" > ");
      if (unique(candidate, el)) return candidate;
    }
    return parts.join(" > ");
  };
  const anchor = node => {
    // Ancestor ids get the churn test the element's own id gets below. A
    // generated wrapper -- a popover, a dialog, a portal root -- is exactly
    // the ancestor a control has no other way to reach, so its id is the one
    // most likely to be reached for and the one that expires soonest.
    if (node.id && !guidLike(node.id)) return `#${CSS.escape(node.id)}`;
    for (const name of ["data-testid", "data-test", "data-qa", "data-cy",
                        "data-id", "data-fieldname", "data-name"]) {
      if (node.hasAttribute(name)) return `[${name}=${attrQuote(node.getAttribute(name))}]`;
    }
    return null;
  };
  // ---- playwright selectorGenerator.ts (v1.61) philosophy, ported --------
  // Ladder (lower score wins): testid 1-2, role+name 100, placeholder 120,
  // text 180, non-GUID #id 500, attr CSS 520, ancestor-anchored 600,
  // classes 650, cssPath last. Every candidate is verified in-page before
  // emission; replay walks the ranked list in order.
  const normText = value => String(value || "").replace(/\s+/g, " ").trim();
  // Ids that read like GUIDs/hashes churn per render (playwright isGuidLike:
  // character-class transition density).
  const guidLike = id => {
    // Ember's default element ids are counters regardless of their current
    // width. A short-lived page can still be at `ember365`, below the generic
    // four-digit counter threshold, and reuse that id for a different control
    // after a rerender.
    if (/^ember\d+$/i.test(id)) return true;
    // Transition density measures ALTERNATION, so it cannot see a counter:
    // `popover976954` makes one lower->digit transition over length 13 and
    // scores as calm, hand-written text. The run itself is the tell, and it
    // reaches us punctuated as often as glued (`mui-12345`,
    // `dialog_1699887766`), so where it sits cannot be the discriminator.
    // This is the same rule the class rung has always applied.
    //
    // It costs an id like `#SAL-ORD-2026-00128` its rung, and that trade is
    // deliberate: a rejected id falls through to five other rungs and a
    // guaranteed structural floor, while an ACCEPTED per-render id resolves
    // at capture and skips on replay. The errors are not symmetric.
    if (/\d{4,}/.test(id)) return true;
    let last, transitions = 0, length = 0;
    for (const ch of id) {
      if (ch === "-" || ch === "_") continue;
      length += 1;
      const kind = /[a-z]/.test(ch) ? "lower" : /[A-Z]/.test(ch) ? "upper"
        : /[0-9]/.test(ch) ? "digit" : "other";
      if (kind === "lower" && last === "upper") { last = kind; continue; }
      if (last && last !== kind) transitions += 1;
      last = kind;
    }
    return transitions >= length / 4;
  };
  const trimWordBoundary = (text, max) => {
    if (text.length <= max) return text;
    const cut = text.slice(0, max);
    const match = cut.match(/^(.*)\b(.+?)$/);
    return match ? match[1].trimEnd() : "";
  };
  // Leading/trailing counters rot ("Inbox 24"); prefer the stripped stem.
  const textAlternatives = text => {
    const out = [];
    const lead = text.match(/^([\d.,]+)[^.,\w]/);
    if (lead) {
      const alt = trimWordBoundary(text.slice(lead[1].length).trimStart(), 80);
      if (alt) out.push({text: alt, bonus: alt.length <= 30 ? 2 : 1});
    }
    const tail = text.match(/[^.,\w]([\d.,]+)$/);
    if (tail) {
      const alt = trimWordBoundary(text.slice(0, text.length - tail[1].length).trimEnd(), 80);
      if (alt) out.push({text: alt, bonus: alt.length <= 30 ? 2 : 1});
    }
    if (text.length <= 30) out.push({text, bonus: 0});
    else {
      const t80 = trimWordBoundary(text, 80);
      if (t80) out.push({text: t80, bonus: 0});
      const t30 = trimWordBoundary(text, 30);
      if (t30) out.push({text: t30, bonus: 1});
    }
    if (!out.length && text) out.push({text: text.slice(0, 80), bonus: 0});
    const seen = new Set();
    return out.filter(a => !seen.has(a.text) && seen.add(a.text));
  };
  const labelledByText = el => {
    const ids = (el.getAttribute("aria-labelledby") || "").trim().split(/\s+/).filter(Boolean);
    return ids.map(id => document.getElementById(id))
      .filter(Boolean).map(node => node.innerText || node.textContent || "")
      .join(" ").trim();
  };
  // Preserve WHICH human-facing channel supplied the identity. Replay can
  // then ask Playwright to re-evaluate that same semantic contract instead of
  // guessing from unrelated state such as a text field's current value.
  const accessibleIdentityOf = el => {
    const labelledBy = labelledByText(el);
    if (labelledBy) return {name: labelledBy, source: "accessible", kind: "aria-labelledby"};
    const ariaLabel = (el.getAttribute("aria-label") || "").trim();
    if (ariaLabel) return {name: ariaLabel, source: "accessible", kind: "aria-label"};
    const label = (el.labels && el.labels.length
      ? (el.labels[0].innerText || el.labels[0].textContent || "") : "").trim();
    if (label) return {name: label, source: "accessible", kind: "label"};
    const text = (el.innerText || "").trim();
    if (text) return {name: text, source: "accessible", kind: "text"};
    const title = (el.getAttribute("title") || "").trim();
    if (title) return {name: title, source: "accessible", kind: "title"};
    const placeholder = (el.getAttribute("placeholder") || "").trim();
    if (placeholder) return {name: placeholder, source: "accessible", kind: "placeholder"};
    return {name: "", source: "", kind: ""};
  };
  const nameOf = el => accessibleIdentityOf(el).name;
  // Bootstrap-style icon buttons move their tooltip text from `title` into
  // a framework data attribute. Those attributes are not ARIA names, so keep
  // them out of role-selector generation while retaining their operator-facing
  // tooltip text as capture identity and a stable exact-attribute selector.
  const identityOf = el => {
    const accessible = accessibleIdentityOf(el);
    if (accessible.name) return accessible;
    const tooltip = (el.getAttribute("data-original-title")
      || el.getAttribute("data-bs-title") || "").trim();
    if (tooltip) return {name: tooltip, source: "tooltip", kind: "tooltip"};
    const attribute = (el.getAttribute("name") || "").trim();
    return {name: attribute, source: attribute ? "attribute" : "",
            kind: attribute ? "name-attribute" : ""};
  };
  // ARIA role approximation for role= candidates: explicit role, else the
  // strict HTML mapping. Unmapped tags get NO role selector (playwright
  // would compute the full AX role; divs/spans never earn one either way).
  const roleOf = el => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    if (el.tagName === "A") return el.hasAttribute("href") ? "link" : null;
    if (el.tagName === "INPUT") {
      const type = el.getAttribute("type") || "text";
      return ({button: "button", submit: "button", reset: "button",
               checkbox: "checkbox", radio: "radio"})[type] || "textbox";
    }
    return ({BUTTON: "button", SELECT: "combobox", TEXTAREA: "textbox",
             OPTION: "option"})[el.tagName] || null;
  };
  // The replay side runs playwright's real engines; these walks only have to
  // agree with them often enough for ranking — the ranked list absorbs drift.
  const ROLE_PROBES = {
    link: "a[href],[role=link]",
    button: "button,[role=button],input[type=button],input[type=submit],input[type=reset]",
    checkbox: "input[type=checkbox],[role=checkbox]",
    radio: "input[type=radio],[role=radio]",
    combobox: "select,[role=combobox]",
    textbox: "input,textarea,[role=textbox]",
    option: "option,[role=option]",
  };
  const uniqueAmong = (matches, el) => {
    if (matches.length === 1) return matches[0] === el;
    const visible = matches.filter(x => x.getClientRects().length > 0);
    return visible.length === 1 && visible[0] === el;
  };
  const verifyRole = (roleName, value, el, exact) => {
    let matches;
    try {
      matches = [...document.querySelectorAll(ROLE_PROBES[roleName] || `[role=${roleName}]`)];
    } catch { return false; }
    const wanted = value.toLowerCase();
    matches = matches.filter(x => {
      const current = normText(nameOf(x)).toLowerCase();
      return exact ? current === wanted : current.includes(wanted);
    });
    return uniqueAmong(matches, el);
  };
  // One DOM scan serves every text candidate of a click: ERPNext desk pages
  // carry multi-megabyte inline boot blobs, and a full-document rescan per
  // alternative measured 100ms+ of main-thread jank per recorded click.
  let textScan = null;
  const textScanFor = () => {
    if (textScan) return textScan;
    textScan = [];
    for (const x of document.querySelectorAll("*")) {
      if (["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"].includes(x.tagName)) continue;
      const norm = normText(x.textContent);
      if (norm) textScan.push({el: x, norm, lower: norm.toLowerCase()});
    }
    return textScan;
  };
  const verifyText = (value, el, exact) => {
    const wanted = exact ? normText(value) : value.toLowerCase();
    const matched = [];
    for (const entry of textScanFor()) {
      if (exact ? entry.norm !== wanted : !entry.lower.includes(wanted)) continue;
      matched.push(entry.el);
    }
    // smallest-element semantics: drop anything with a matching child
    const set = new Set(matched);
    const leaves = matched.filter(x => ![...x.children].some(c => set.has(c)));
    return uniqueAmong(leaves, el);
  };
  const selectorCandidates = el => {
    const scored = [];
    textScan = null;  // page content may have changed since the last click
    // Reports whether the candidate earned a place, so a caller walking a
    // ladder of its own can stop at the first rung that resolves.
    const push = (selector, score, verify) => {
      if (scored.some(s => s.selector === selector)) return false;
      if (!verify()) return false;
      scored.push({selector, score});
      return true;
    };
    for (const attr of ["data-testid", "data-test-id", "data-test", "data-qa",
                        "data-cy", "data-id", "data-fieldname", "data-name"]) {
      const value = el.getAttribute(attr);
      if (value) {
        const sel = `[${attr}=${attrQuote(value)}]`;
        if (push(sel, attr === "data-testid" ? 1 : 2, () => unique(sel, el))) continue;
        // A record's id is stamped on several of its own cells: frappe's
        // list row carries dataset.name on the checkbox, the subject link
        // and the like toggle alike, so the bare attribute names the ROW
        // and is dropped for ambiguity. The target's tag is what separates
        // the cells, and it is as durable as the attribute it qualifies --
        // without it a row click falls all the way to a positional path.
        const qualified = `${el.tagName.toLowerCase()}${sel}`;
        push(qualified, attr === "data-testid" ? 2 : 3,
             () => unique(qualified, el));
      }
    }
    // Fan-out is capped at two alternatives per engine so role=/text=
    // variants of one accessible name cannot crowd the CSS rungs out of the
    // final list (real-engine name computation can diverge from nameOf's
    // approximation; CSS is the resilience floor).
    const ariaRole = roleOf(el);
    const accName = normText(nameOf(el)).slice(0, 300);
    if (ariaRole && !["none", "presentation"].includes(ariaRole) && accName) {
      for (const alt of textAlternatives(accName).slice(0, 2))
        push(`role=${ariaRole}[name*=${attrQuote(alt.text)}]`, 100 - alt.bonus,
             () => verifyRole(ariaRole, alt.text, el, false));
      if (accName.length <= 80)
        push(`role=${ariaRole}[name=${attrQuote(accName)}]`, 105,
             () => verifyRole(ariaRole, accName, el, true));
    }
    for (const attr of ["title", "data-original-title", "data-bs-title"]) {
      const value = el.getAttribute(attr);
      if (value) {
        const sel = `[${attr}=${attrQuote(value)}]`;
        // Last of the label channels, at playwright's own title score: this
        // is prose written for a hovering human, so it is wordier, gets
        // translated, and in ERPNext often carries a modified-on timestamp.
        // Every name the operator could actually read outranks it; it exists
        // to reach the icon-only control that has no other name at all.
        push(sel, 205, () => unique(sel, el));
      }
    }
    if (el.getAttribute("placeholder")) {
      const sel = `[placeholder=${attrQuote(el.getAttribute("placeholder"))}]`;
      push(sel, 120, () => unique(sel, el));
    }
    const tag = el.tagName.toLowerCase();
    const ownText = el.matches("input,textarea,select") ? "" : normText(el.innerText);
    if (ownText) {
      for (const alt of textAlternatives(ownText).slice(0, 2)) {
        // Unquoted text= grammar hazards (verified against the real engine):
        // a leading / parses as a REGEX literal, and a leading or trailing
        // quote re-quotes the body. The quoted-exact candidate below covers
        // those strings safely.
        if (/["\n>]/.test(alt.text)) continue;
        if (/^['"\/]/.test(alt.text) || /['"]$/.test(alt.text)) continue;
        push(`text=${alt.text}`, 180 - alt.bonus,
             () => verifyText(alt.text, el, false));
      }
      if (ownText.length <= 80)
        push(`text=${attrQuote(ownText)}`, 185,
             () => verifyText(ownText, el, true));
    }
    if (el.id && !guidLike(el.id)) {
      const sel = `#${CSS.escape(el.id)}`;
      push(sel, 500, () => unique(sel, el));
    }
    if (el.getAttribute("name")) {
      const sel = `${tag}[name=${attrQuote(el.getAttribute("name"))}]`;
      push(sel, 520, () => unique(sel, el));
    }
    if (el.getAttribute("aria-label")) {
      const sel = `[aria-label=${attrQuote(el.getAttribute("aria-label"))}]`;
      push(sel, 520, () => unique(sel, el));
    }
    // Controls with no attributes of their own (framework-generated dialog
    // inputs) anchor through attributed ancestors: nearest single anchors
    // first, then further-ancestor pairs when a sibling section collides.
    const anchors = [];
    for (let node = el.parentElement; node && node.nodeType === 1 && anchors.length < 4; node = node.parentElement) {
      const sel = anchor(node);
      if (sel) anchors.push(sel);
    }
    for (const near of anchors) {
      const sel = `${near} ${tag}`;
      push(sel, 600, () => unique(sel, el));
    }
    for (let i = 0; i < anchors.length - 1; i++)
      for (let j = i + 1; j < anchors.length; j++) {
        const sel = `${anchors[j]} ${anchors[i]} ${tag}`;
        push(sel, 610, () => unique(sel, el));
      }
    // A repeated label/value pair (for example Assignee / Assignee) is not
    // globally unique, but its position inside a stable widget is far more
    // durable than a full-page nth-of-type chain. This mirrors Playwright's
    // parent-scoped semantic selector followed by a small nth discriminator.
    if (ownText) {
      for (const near of anchors) {
        let scopes;
        try { scopes = [...document.querySelectorAll(near)]; }
        catch { continue; }
        const matched = textScanFor()
          .filter(entry => entry.norm === ownText
            && scopes.some(scope => scope === entry.el || scope.contains(entry.el)))
          .map(entry => entry.el);
        const set = new Set(matched);
        const leaves = matched.filter(node =>
          ![...node.children].some(child => set.has(child)));
        const visible = leaves.filter(node => node.getClientRects().length > 0);
        const candidates = visible.length ? visible : leaves;
        const index = candidates.indexOf(el);
        if (index < 0 || index > 5) continue;
        const scoped = `${near} >> text=${attrQuote(ownText)}`;
        if (candidates.length === 1) {
          push(scoped, 620, () => candidates[0] === el);
        } else {
          push(`${scoped} >> nth=${index}`, 10620,
               () => candidates[index] === el);
        }
      }
    }
    const volatileClass = value => /^(?:active|selected|current|open|closed|show|shown|hide|hidden|disabled|enabled|checked|focus|focused|hover|loading|loaded|expanded|collapsed|invalid|valid|dirty|pristine|touched|untouched|is-.+|has-.+)$/i.test(value);
    const classes = [...el.classList].filter(x =>
      !/\d{4,}|^[a-f0-9]{8,}$/i.test(x) && !volatileClass(x)).slice(0, 8);
    // A framework writes its style tokens first and its semantic token last
    // ("text-muted btn btn-default next-doc"), so a fixed leading pair is the
    // one subset guaranteed to match every sibling in a toolbar. Try each
    // class alone before widening, and take the first subset that resolves:
    // the smallest resolving set carries the fewest tokens that can churn.
    const subsets = classes.map(x => [x]);
    for (let i = 2; i <= classes.length; i++) subsets.push(classes.slice(0, i));
    for (const subset of subsets) {
      const sel = `${tag}.${subset.map(CSS.escape).join(".")}`;
      if (push(sel, 650, () => unique(sel, el))) break;
    }
    const path = cssPath(el);
    push(path, 1000000, () => unique(path, el));
    scored.sort((a, b) => a.score - b.score);
    // Resilience floor: the final list always carries at least one CSS
    // selector — replay's last recourse when engine semantics drift.
    const isEngine = s => s.selector.startsWith("role=") || s.selector.startsWith("text=");
    let chosen = scored.slice(0, 6);
    if (chosen.length && chosen.every(isEngine)) {
      const css = scored.find(s => !isEngine(s));
      if (css) chosen = [...chosen.slice(0, 5), css];
    }
    return chosen;
  };
  const role = el => el.getAttribute("role") || ({
    BUTTON: "button", A: "link", INPUT: "textbox", SELECT: "combobox",
    TEXTAREA: "textbox", OPTION: "option"
  }[el.tagName] || el.tagName.toLowerCase());
  // Identity only, never state: a typed value baked into the accessible name
  // makes the role fallback unresolvable on a fresh page (the field is empty
  // again on replay).
  const describe = el => {
    const identity = identityOf(el);
    const candidates = selectorCandidates(el);
    const selectorKind = candidate => {
      if (candidate.selector.startsWith("role=")
          || candidate.selector.startsWith("text=")
          || ["[placeholder=", "[title=", "[data-original-title=",
              "[data-bs-title=", "[aria-label="].some(prefix =>
                candidate.selector.startsWith(prefix))) return "semantic";
      if (candidate.score < 600) return "contract";
      if (candidate.score < 1000000) return "weak";
      return "structural";
    };
    return {
      tag: el.tagName.toLowerCase(), role: role(el),
      aria_role: roleOf(el) || "",
      name: identity.name.slice(0, 300), name_source: identity.source,
      identity_kind: identity.kind,
      input_type: el.getAttribute("type") || "", autocomplete: el.getAttribute("autocomplete") || "",
      selectors: candidates.map(candidate => candidate.selector),
      selector_kinds: candidates.map(selectorKind),
      selector_scores: candidates.map(candidate => candidate.score),
    };
  };
  // Identity of the focused element, for settlement evidence. Same
  // identity-not-state convention (and truncation) as click targets.
  const focusIdentity = () => {
    const el = document.activeElement;
    if (!el || el.nodeType !== 1 || el === document.body
        || el === document.documentElement) return null;
    const identity = identityOf(el);
    return {role: role(el), aria_role: roleOf(el) || "",
            name: identity.name.slice(0, 300),
            name_source: identity.source, identity_kind: identity.kind};
  };
  // A Boolean control's click is not an instruction to blindly toggle it on
  // replay: an application can cancel the activation, and a fresh fixture can
  // already have the recorded state. Native controls expose `checked`; custom
  // ARIA controls expose the equivalent state through aria-checked. Switches
  // remain ordinary clicks because Playwright's set_checked contract covers
  // checkbox/radio roles, not role=switch.
  const checkedState = el => {
    if (!el || el.nodeType !== 1) return null;
    const inputType = (el.getAttribute("type") || "").toLowerCase();
    if (el.tagName === "INPUT" && ["checkbox", "radio"].includes(inputType))
      return !!el.checked;
    const ariaRole = (el.getAttribute("role") || "").toLowerCase();
    const ariaChecked = el.getAttribute("aria-checked");
    if (["checkbox", "radio"].includes(ariaRole)
        && ["true", "false"].includes(ariaChecked))
      return ariaChecked === "true";
    return null;
  };
  const booleanPressStates = new WeakMap();
  const rememberBooleanBefore = el => {
    const checked = checkedState(el);
    if (checked !== null) booleanPressStates.set(el, checked);
  };
  // The last value reported, and for which element. A gesture on ANOTHER
  // element ends the run: the operator moved on, so the same value arriving
  // afterwards is a fresh instruction rather than a second report of one edit.
  // Enter and blur belong to the field they commit, and must not end it.
  let lastValueSent = null;
  // Every authored action stamps a per-document sequence at send time; the
  // pending-edit flushes at the top of each gesture handler keep send order
  // equal to authored order. Settlement patches never consume a sequence.
  const send = (type, el, extra = {}) => {
    if (!el || el.nodeType !== 1) return;
    if (lastValueSent && lastValueSent.el !== el) lastValueSent = null;
    try {
      binding({type, target: describe(el), ...extra,
               capture_sequence: ++captureSequence, timestamp: Date.now()});
    } catch (_) { /* recorder teardown/navigation race */ }
  };
  // Canvas editors route text through a hidden textarea and consume its
  // content as they process.  Its value is only transient, so semantic edit
  // events (beforeinput/paste/composition) are the source of truth; keydown is
  // retained only as a compatibility fallback and for command keys.
  const imeLike = el => {
    if (!el || el.nodeType !== 1) return false;
    if (!(el.matches("input, textarea") || el.isContentEditable)) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (rect.width * rect.height <= 16 || rect.right <= 0 || rect.bottom <= 0
        || style.opacity === "0" || style.visibility === "hidden") return true;
    // OnlyOffice's #area_id is a normal-sized, fully opaque textarea hidden
    // by z-order UNDER the canvas — covered-by-canvas at its own center is
    // the reliable tell for that family of editors.
    const at = document.elementFromPoint(
      rect.left + rect.width / 2, rect.top + rect.height / 2);
    return !!at && at !== el && at.tagName === "CANVAS";
  };
  const typeBuffers = new WeakMap();
  const editStates = new WeakMap();
  const pendingTypes = new Set();
  let nextEditSequence = 1;
  const editState = el => {
    let state = editStates.get(el);
    if (!state) {
      state = {
        composing: false, compositionText: "", recentText: "",
        recentAt: -Infinity, recentSource: "", keyFallback: null,
        pasteFallback: null, shortcutFallback: null
      };
      editStates.set(el, state);
    }
    return state;
  };
  const queueType = (el, text, source, inputType, eventTime) => {
    if (!text) return;
    const buf = typeBuffers.get(el) || {
      text: "", timer: 0, sources: new Set(), inputTypes: new Set(),
      firstEventTime: eventTime, lastEventTime: eventTime
    };
    buf.text += text;
    if (source) buf.sources.add(source);
    if (inputType) buf.inputTypes.add(inputType);
    if (buf.firstEventTime === undefined) buf.firstEventTime = eventTime;
    buf.lastEventTime = eventTime;
    clearTimeout(buf.timer);
    buf.timer = setTimeout(() => flushType(el), 600);
    typeBuffers.set(el, buf);
    pendingTypes.add(el);
  };
  const flushType = el => {
    const buf = typeBuffers.get(el);
    if (!buf || !buf.text) return;
    clearTimeout(buf.timer);
    const text = buf.text;
    typeBuffers.delete(el);
    pendingTypes.delete(el);
    send("type", el, {
      text,
      edit_sequence: nextEditSequence++,
      input_source: buf.sources.size === 1 ? [...buf.sources][0] : "semantic",
      input_types: [...buf.inputTypes],
      event_time_start: buf.firstEventTime,
      event_time_end: buf.lastEventTime
    });
  };
  const rememberSemantic = (state, text, eventTime, source) => {
    state.recentText = text;
    state.recentAt = eventTime;
    state.recentSource = source;
  };
  const recentlyRecorded = (state, text, eventTime, sources) => (
    !!text && sources.includes(state.recentSource)
    && state.recentText === text && eventTime - state.recentAt <= 50
  );
  const cancelKeyFallback = el => {
    const state = editState(el);
    if (!state.keyFallback) return;
    clearTimeout(state.keyFallback.timer);
    state.keyFallback = null;
  };
  const flushKeyFallback = el => {
    const state = editState(el);
    const fallback = state.keyFallback;
    if (!fallback) return;
    clearTimeout(fallback.timer);
    state.keyFallback = null;
    queueType(el, fallback.text, "keydown-fallback", "insertText",
              fallback.eventTime);
  };
  const consumePasteFallback = el => {
    const state = editState(el);
    const fallback = state.pasteFallback;
    if (!fallback) return "";
    clearTimeout(fallback.timer);
    state.pasteFallback = null;
    return fallback.text;
  };
  const cancelShortcutFallback = el => {
    const state = editState(el);
    if (!state.shortcutFallback) return;
    clearTimeout(state.shortcutFallback.timer);
    state.shortcutFallback = null;
  };
  const flushShortcutFallback = el => {
    const state = editState(el);
    const fallback = state.shortcutFallback;
    if (!fallback) return;
    clearTimeout(fallback.timer);
    state.shortcutFallback = null;
    send("press", el, {key: fallback.key, modifiers: fallback.modifiers});
  };
  const flushPendingTypes = except => {
    for (const el of [...pendingTypes]) if (el !== except) flushType(el);
  };
  const timers = new WeakMap();
  const draggedValueUntil = new WeakMap();
  // Tokenizing controls (Roundcube recipients, tag pickers) can move the
  // typed text into a chip and clear their real input before our debounce
  // fires. Keep the value as it was during the input event, not whatever the
  // widget has rewritten it to 450 ms later.
  const pendingValueSnapshots = new WeakMap();
  // Elements whose debounce is still armed. A queued edit belongs BEFORE the
  // gesture that follows it, so the next gesture flushes it rather than letting
  // the timer land after — replay applies events in the order it receives them.
  const pendingValues = new Set();
  const sendValue = el => {
    clearTimeout(timers.get(el));
    pendingValues.delete(el);
    if ((draggedValueUntil.get(el) || 0) >= Date.now()) {
      pendingValueSnapshots.delete(el);
      return;
    }
    if (imeLike(el)) return;
    if (["checkbox", "radio", "button", "submit"].includes(el.type)) return;
    const secret = el.type === "password";
    const username = /email|user/i.test(`${el.name} ${el.id} ${el.autocomplete}`);
    const type = el.tagName === "SELECT" ? "select" : "fill";
    const snapshot = pendingValueSnapshots.has(el)
      ? pendingValueSnapshots.get(el) : undefined;
    pendingValueSnapshots.delete(el);
    const rawValue = snapshot === undefined
      ? (el.isContentEditable ? el.innerText : el.value) : snapshot;
    const value = secret ? "<password>" : String(rawValue);
    const signature = `${type}\u0000${value}`;
    // One edit is reported twice — the debounced input, then change on blur —
    // and that pair must collapse to one event. A value the operator enters
    // again after doing something else is NOT that pair: a spreadsheet drives
    // every cell through one Name Box, so "go to A5" recurs on each worksheet.
    // Suppressing the repeat drops the navigation while keeping the tab switch
    // that made it necessary, and every later value then lands in whatever cell
    // the new sheet had selected — with nothing raised anywhere.
    if (lastValueSent && lastValueSent.el === el
        && lastValueSent.signature === signature) return;
    send(type, el, {
      value,
      value_source: secret ? "password" : (username ? "email" : "literal")
    });
    lastValueSent = {el, signature};
  };
  // Emit every queued edit except the one still being typed into.
  const flushPendingValues = except => {
    for (const el of [...pendingValues]) if (el !== except) sendValue(el);
  };
  // playwright's retarget list (selectorGenerator.ts) plus canvas. Bare
  // [role]/[tabindex] hoisting is gone: a giant [role=listbox] container is
  // a worse target than the element actually clicked.
  const INTERACTIVE = "button,select,input,textarea,[role=button],[role=checkbox],[role=radio],[role=option],a,[role=link],canvas";
  // Rich-text editors dispatch clicks from replaceable descendants (a Quill
  // paragraph today can be a span tomorrow).  The stable semantic target is
  // the editing host: the outermost editable element whose parent is not
  // editable.  isContentEditable intentionally excludes contenteditable=false
  // islands; ordinary interactive descendants are resolved before this helper
  // so links and buttons embedded in an editor retain their own behavior.
  const editingHost = node => {
    if (!node || node.nodeType !== 1 || !node.isContentEditable) return null;
    let host = node;
    while (host.parentElement && host.parentElement.isContentEditable)
      host = host.parentElement;
    return host;
  };
  const relativePoint = (el, event) => {
    const rect = el.getBoundingClientRect();
    return {x: Math.round(event.clientX - rect.left), y: Math.round(event.clientY - rect.top)};
  };
  // Delegated-click UIs (Frappe list rows) put the handler on a container;
  // the element the operator MEANT is the interactive descendant nearest the
  // pointer — same visual row first, then plain distance, both bounded so an
  // empty-area click cannot adopt a far-away control.
  const nearestInteractiveDescendant = (container, clientX, clientY) => {
    const rows = [], others = [];
    for (const node of container.querySelectorAll(
        "button,select,input,textarea,[role=button],[role=checkbox],[role=radio],[role=option],a,[role=link]")) {
      if (node.matches(":disabled")) continue;
      if (!node.getClientRects().length) continue;
      const rect = node.getBoundingClientRect();
      const dx = clientX < rect.left ? rect.left - clientX
        : clientX > rect.right ? clientX - rect.right : 0;
      const dy = clientY < rect.top ? rect.top - clientY
        : clientY > rect.bottom ? clientY - rect.bottom : 0;
      const entry = {node, left: rect.left, dist: Math.hypot(dx, dy)};
      (dy === 0 ? rows : others).push(entry);
    }
    const nearest = list => list.reduce((a, b) => (b.dist < a.dist ? b : a), list[0]);
    if (rows.length) {
      const close = nearest(rows);
      // On or right beside a control the pointer wins; from row WHITESPACE
      // the operator meant the row's action, and a delegated row's primary
      // control is its leftmost (Frappe's subject link), not whichever
      // badge happens to sit nearer the pointer.
      if (close.dist <= 24) return close.dist <= 800 ? close.node : null;
      const leftmost = rows.reduce((a, b) => (b.left < a.left ? b : a), rows[0]);
      if (leftmost.dist <= 800) return leftmost.node;
      return close.dist <= 800 ? close.node : null;
    }
    if (others.length && nearest(others).dist <= 100) return nearest(others).node;
    return null;
  };
  const dragTarget = (node, clientX, clientY) => {
    if (!node || node.nodeType !== 1) return null;
    return node.closest(`[draggable="true"],${INTERACTIVE}`)
      || nearestInteractiveDescendant(node, clientX, clientY)
      || node;
  };
  // The element a click is authored against: retarget up to an interactive
  // ancestor, then to a rich-text editing host, else down to the nearest
  // interactive descendant (delegated rows), else the raw node. Shared by
  // click and dblclick so a multi-click pair uses the exact same promotion.
  const clickTarget = event => event.target.closest(INTERACTIVE)
    || editingHost(event.target)
    || nearestInteractiveDescendant(event.target, event.clientX, event.clientY)
    || event.target;
  let dragState = null;
  let suppressClick = null;
  let commitClick = null;
  const precise = value => Math.round(value * 1000) / 1000;
  const dragPoint = (state, event) => ({
    x: precise(event.clientX - state.rect.left),
    y: precise(event.clientY - state.rect.top)
  });
  // A pointer can land at x=4.6 inside a five-pixel resize handle. Rounding
  // that to five produces a Playwright position outside the element. Preserve
  // sub-pixel evidence and clamp only the initial press to the source interior.
  const sourcePoint = (rect, event) => ({
    x: Math.min(Math.max(precise(event.clientX - rect.left), 0),
                Math.max(0, precise(rect.width) - 0.001)),
    y: Math.min(Math.max(precise(event.clientY - rect.top), 0),
                Math.max(0, precise(rect.height) - 0.001))
  });
  const sampleDrag = (state, event, force = false) => {
    const fromStart = Math.hypot(
      event.clientX - state.startX, event.clientY - state.startY);
    if (fromStart >= 6) state.moved = true;
    if (!state.moved) return;
    const fromLast = Math.hypot(
      event.clientX - state.lastX, event.clientY - state.lastY);
    if (state.path.length < 120
        && (force || (fromLast >= 3 && event.timeStamp - state.lastSampleAt >= 12))) {
      const point = dragPoint(state, event);
      const last = state.path[state.path.length - 1];
      if (!last || last.x !== point.x || last.y !== point.y) state.path.push(point);
      state.lastX = event.clientX;
      state.lastY = event.clientY;
      state.lastSampleAt = event.timeStamp;
    }
  };
  const finishDrag = (state, event) => {
    sampleDrag(state, event, true);
    if (!state.moved) return;
    const underPointer = document.elementFromPoint(event.clientX, event.clientY);
    const destination = dragTarget(
      underPointer || event.target, event.clientX, event.clientY) || state.source;
    const destinationRect = destination.getBoundingClientRect();
    // Range sliders and similar controls emit input/change during the same
    // physical drag. Keep the gesture as one first-class action rather than a
    // drag followed by a duplicate fill of its final value.
    clearTimeout(timers.get(state.source));
    pendingValues.delete(state.source);
    pendingValueSnapshots.delete(state.source);
    draggedValueUntil.set(state.source, Date.now() + 300);
    send("drag", state.source, {
      drag_mode: state.native ? "native" : "pointer",
      destination: describe(destination),
      source_position: state.path[0],
      target_position: {
        x: precise(event.clientX - destinationRect.left),
        y: precise(event.clientY - destinationRect.top)
      },
      path: state.path,
      pointer_type: event.pointerType || "mouse"
    });
    suppressClick = {x: event.clientX, y: event.clientY, at: Date.now()};
  };
  document.addEventListener("pointerdown", event => {
    if (event.button !== 0 || !event.isPrimary) return;
    // Remember the editor a background click is about to blur. The focus is
    // read HERE because pointerdown is the last moment it still holds: by the
    // time the click arrives the browser has already moved it to <body>.
    const focused = document.activeElement;
    commitClick = focused && focused !== event.target
      && (focused.matches("input,textarea,select") || focused.isContentEditable)
      && !event.target.closest(INTERACTIVE)
      ? {x: event.clientX, y: event.clientY, at: Date.now(),
         target: event.target, focused} : null;
    const source = dragTarget(event.target, event.clientX, event.clientY);
    if (!source) return;
    rememberBooleanBefore(source);
    flushPendingValues(source);
    flushPendingTypes(null);
    const rect = source.getBoundingClientRect();
    dragState = {
      pointerId: event.pointerId, source, rect,
      startX: event.clientX, startY: event.clientY,
      lastX: event.clientX, lastY: event.clientY,
      lastSampleAt: event.timeStamp,
      path: [sourcePoint(rect, event)],
      moved: false
    };
  }, true);
  document.addEventListener("pointermove", event => {
    const state = dragState;
    if (!state || state.pointerId !== event.pointerId) return;
    sampleDrag(state, event);
  }, true);
  document.addEventListener("pointerup", event => {
    const state = dragState;
    if (!state || state.pointerId !== event.pointerId) return;
    dragState = null;
    finishDrag(state, event);
  }, true);
  // Native HTML drag-and-drop cancels the pointer stream as soon as dragstart
  // fires. Continue the same gesture through dragover/dragend so draggable
  // cards and rows are not lost.
  document.addEventListener("dragstart", event => {
    if (!dragState) return;
    dragState.native = true;
    dragState.moved = true;
  }, true);
  document.addEventListener("dragover", event => {
    if (dragState && dragState.native) sampleDrag(dragState, event);
  }, true);
  document.addEventListener("dragend", event => {
    const state = dragState;
    if (!state || !state.native) return;
    dragState = null;
    finishDrag(state, event);
  }, true);
  document.addEventListener("pointercancel", event => {
    if (dragState && dragState.pointerId === event.pointerId && !dragState.native)
      dragState = null;
  }, true);
  document.addEventListener("click", event => {
    if (suppressClick
        && Date.now() - suppressClick.at <= 300
        && Math.hypot(event.clientX - suppressClick.x,
                      event.clientY - suppressClick.y) <= 6) {
      suppressClick = null;
      return;
    }
    suppressClick = null;
    if (labelForward && labelForward.control === event.target) {
      // The browser-forwarded duplicate of a label click whose transaction
      // is already recorded; a second begin would persist one gesture twice.
      return;
    }
    const commit = commitClick;
    commitClick = null;
    const commitsFocusedEdit = commit
      && Date.now() - commit.at <= 1000
      && Math.hypot(event.clientX - commit.x, event.clientY - commit.y) <= 6;
    const active = document.activeElement;
    if (active && active !== event.target && (active.matches("input,textarea,select") || active.isContentEditable))
      sendValue(active);
    flushPendingValues(event.target);
    flushPendingTypes(null);
    const target = clickTarget(event);
    let extra = {};
    if (target.tagName === "CANVAS") {
      extra = {position: relativePoint(target, event)};
    } else if (target === editingHost(event.target)) {
      // Promoting a generated descendant must not move the caret. Preserve
      // the physical click, recalculated against the final replay target.
      extra = {position: relativePoint(target, event)};
    } else if (target === event.target && !target.matches(INTERACTIVE)) {
      // Nothing to anchor to. Small elements replay faithfully with a
      // center click; a large container's center is NOT where the operator
      // clicked, so keep the raw offset for a positioned replay click.
      const rect = target.getBoundingClientRect();
      if (rect.width > 200 || rect.height > 80)
        extra = {position: relativePoint(target, event)};
    }
    // Blank form space, clicked only to blur the editor above. Recording it as
    // a coordinate is worse than recording nothing: replay resolves the point
    // against whatever occupies it later. Discarding the position is only safe
    // where the gesture cannot have meant anything else, so every guard below
    // narrows what "blank space" is allowed to be.
    if (commitsFocusedEdit
        // The pointer went down on THIS element. An overlay opening mid-press
        // throws the click up to a common ancestor -- usually <body>, which
        // passes every other guard here.
        && commit.target === event.target
        && target === event.target && !target.closest(INTERACTIVE)
        // Blank space BELONGING to the form encloses the field it commits. A
        // modal backdrop does not, and clicking one dismisses the dialog.
        && target.contains(commit.focused)
        // ...but Bootstrap's .modal encloses its own dialog AND dismisses it,
        // so enclosure alone cannot see that one. Form space is in the
        // document flow; a dismiss-on-click overlay is lifted out of it. The
        // dialog's own padding stays eligible -- that is not fixed.
        && getComputedStyle(target).position !== "fixed") {
      const rect = target.getBoundingClientRect();
      // Both dimensions, unlike the positional fallback above: that branch
      // preserves the operator's point when unsure, this one throws it away.
      // A wide-but-short strip is a toolbar or a row, not a gutter.
      if (rect.width > 200 && rect.height > 80)
        extra = {commit_only: true};
    }
    // A label click makes the browser dispatch a second trusted click on the
    // label's control after this dispatch completes. That control owns the
    // gesture's boolean state, and its click must fold into THIS transaction.
    const label = event.target.closest ? event.target.closest("label") : null;
    const control = (label && label.control && !label.control.disabled
                     && label.control !== event.target
                     && !label.control.contains(event.target))
      ? label.control : null;
    if (control) {
      labelForward = {control};
      setTimeout(() => { labelForward = null; }, 0);
    }
    const stateTarget = control || target;
    let checkedBefore = booleanPressStates.get(stateTarget);
    booleanPressStates.delete(stateTarget);
    if (checkedBefore === undefined && control) {
      // Label dispatch precedes the control's activation toggle, so the
      // control still shows its pre-click state here.
      const state = checkedState(control);
      if (state !== null) checkedBefore = state;
    }
    if (checkedBefore !== undefined) extra.checked_before = checkedBefore;
    const focusBefore = focusIdentity();
    if (focusBefore) extra.focus_before = focusBefore;
    const actionId = docToken + ":" + (++clickCounter);
    send("click_begin", target, {
      ...extra,
      action_id: actionId,
      url_before: location.href,
      event_timestamp: event.timeStamp,
      button: event.button,
      buttons: event.buttons,
      detail: event.detail,
      is_trusted: event.isTrusted,
      cancelable: event.cancelable,
      pointer_type: event.pointerType || "",
      // -1 is the spec value for activation without a pointing device.
      pointer_id: "pointerId" in event ? event.pointerId : -1,
      client_position: {x: event.clientX, y: event.clientY},
      modifiers: MODIFIER_KEYS.filter(key => event[key + "Key"]),
    });
    activeClick = {actionId, target};
    recentClicks.push({actionId, element: target, detail: event.detail,
                       button: event.button, isTrusted: event.isTrusted});
    if (recentClicks.length > 2) recentClicks.shift();
    // Settlement: the next task observes synchronous handlers, browser
    // activation, and their microtasks. Checkbox checkedness is toggled
    // before click listeners run, then restored after dispatch when a later
    // listener calls preventDefault(); defaultPrevented is likewise only
    // final now. A successful cross-document navigation destroys this timer
    // with the document — which is exactly why begin was sent already.
    setTimeout(() => {
      if (activeClick && activeClick.actionId === actionId) activeClick = null;
      const settled = {
        type: "click_settled",
        action_id: actionId,
        default_prevented: event.defaultPrevented,
        url_after: location.href,
        target_connected: target.isConnected,
        timestamp: Date.now(),
      };
      const checkedAfter = checkedState(stateTarget);
      if (checkedAfter !== null) settled.checked_after = checkedAfter;
      const focusAfter = focusIdentity();
      if (focusAfter) settled.focus_after = focusAfter;
      try { binding(settled); }
      catch (_) { /* recorder teardown/navigation race */ }
    }, 0);
  }, true);
  // Browser-confirmed multi-click: a trusted dblclick names the two click
  // transactions it closes. Element REFERENCE equality is the compatibility
  // gate — a target rerendered between the clicks breaks the pair and both
  // clicks stay independent. The host attaches this as evidence; it is
  // never an authored action, and two fast clicks alone never qualify.
  document.addEventListener("dblclick", event => {
    if (!event.isTrusted || event.detail !== 2) return;
    if (recentClicks.length < 2) return;
    const [first, second] = recentClicks;
    const target = clickTarget(event);
    if (first.element !== target || second.element !== target) return;
    if (!first.isTrusted || !second.isTrusted) return;
    if (first.detail !== 1 || second.detail !== 2) return;
    if (first.button !== event.button || second.button !== event.button) return;
    try {
      binding({type: "click_multi",
               action_ids: [first.actionId, second.actionId],
               detail: event.detail, timestamp: Date.now()});
    } catch (_) { /* recorder teardown/navigation race */ }
  }, true);
  // Navigation intent — links, pushState/replaceState, fragment moves, form
  // submissions, downloads — reported by the document itself while a click
  // transaction is active. Feature-detected; observation only: the recorder
  // never intercepts or cancels navigation.
  if (window.navigation && typeof window.navigation.addEventListener === "function") {
    window.navigation.addEventListener("navigate", event => {
      const current = activeClick;
      if (!current) return;
      const destination = event.destination;
      const evidence = {
        class: "navigation_intent",
        kind: "navigation",
        navigation_type: event.navigationType || "",
        same_document: !!(destination && destination.sameDocument),
        user_initiated: !!event.userInitiated,
        hash_change: !!event.hashChange,
        destination: sanitizeUrl(destination ? destination.url : ""),
        has_form_data: !!event.formData,
        download_requested: event.downloadRequest != null,
      };
      if ("sourceElement" in event)
        evidence.source_matches_click_target = !!(event.sourceElement
          && (event.sourceElement === current.target
              || current.target.contains(event.sourceElement)
              || event.sourceElement.contains(current.target)));
      try {
        binding({type: "click_outcome", action_id: current.actionId,
                 evidence, timestamp: Date.now()});
      } catch (_) { /* recorder teardown/navigation race */ }
    });
  }
  // Clipboard contents are read only from the user-authorized paste event.
  // beforeinput normally follows synchronously and consumes this fallback;
  // the zero-delay timer covers editors/browsers that omit beforeinput.
  document.addEventListener("paste", event => {
    const el = event.target;
    if (!imeLike(el)) return;
    const text = event.clipboardData ? event.clipboardData.getData("text/plain") : "";
    if (!text) return;
    // A semantic paste is stronger evidence than the Command/Ctrl+V key that
    // initiated it: it survives clipboard and focus differences at replay.
    // Cancel the shortcut fallback so one physical paste remains one action.
    cancelShortcutFallback(el);
    const state = editState(el);
    if (state.pasteFallback) clearTimeout(state.pasteFallback.timer);
    const fallback = {text, eventTime: event.timeStamp, timer: 0};
    fallback.timer = setTimeout(() => {
      if (state.pasteFallback !== fallback) return;
      state.pasteFallback = null;
      rememberSemantic(state, text, fallback.eventTime, "paste");
      queueType(el, text, "paste", "insertFromPaste", fallback.eventTime);
    }, 0);
    state.pasteFallback = fallback;
  }, true);
  document.addEventListener("compositionstart", event => {
    if (!imeLike(event.target)) return;
    const state = editState(event.target);
    cancelKeyFallback(event.target);
    state.composing = true;
    state.compositionText = "";
  }, true);
  document.addEventListener("compositionupdate", event => {
    if (!imeLike(event.target)) return;
    const state = editState(event.target);
    state.composing = true;
    state.compositionText = typeof event.data === "string"
      ? event.data : state.compositionText;
  }, true);
  document.addEventListener("compositionend", event => {
    const el = event.target;
    if (!imeLike(el)) return;
    const state = editState(el);
    cancelKeyFallback(el);
    const text = typeof event.data === "string"
      ? event.data : state.compositionText;
    state.composing = false;
    state.compositionText = "";
    if (text && !recentlyRecorded(
        state, text, event.timeStamp, ["beforeinput"])) {
      rememberSemantic(state, text, event.timeStamp, "composition");
      queueType(el, text, "composition", "insertCompositionText",
                event.timeStamp);
    }
  }, true);
  document.addEventListener("beforeinput", event => {
    const el = event.target;
    if (!imeLike(el)) return;
    const state = editState(el);
    const inputType = event.inputType || "";
    if (inputType === "insertCompositionText" || event.isComposing) {
      cancelKeyFallback(el);
      state.composing = true;
      state.compositionText = typeof event.data === "string"
        ? event.data : state.compositionText;
      return;
    }
    if (!inputType.startsWith("insert")
        || inputType === "insertLineBreak"
        || inputType === "insertParagraph") return;
    let text = event.data || "";
    if (!text && event.dataTransfer)
      text = event.dataTransfer.getData("text/plain") || "";
    if (inputType === "insertFromPaste") {
      const pasted = consumePasteFallback(el);
      text = text || pasted;
    } else
      cancelKeyFallback(el);
    if (!text || recentlyRecorded(
        state, text, event.timeStamp, ["composition"])) return;
    if (inputType === "insertFromPaste") cancelShortcutFallback(el);
    rememberSemantic(state, text, event.timeStamp, "beforeinput");
    queueType(el, text, "beforeinput", inputType, event.timeStamp);
  }, true);
  document.addEventListener("input", event => {
    if (imeLike(event.target)) {
      // Confirmation/fallback only.  A matching beforeinput or compositionend
      // has already recorded this mutation and must not become a second type.
      const el = event.target;
      const state = editState(el);
      const inputType = event.inputType || "";
      if (!event.isComposing && inputType.startsWith("insert")) {
        const text = event.data || "";
        if (text && !recentlyRecorded(
            state, text, event.timeStamp, ["beforeinput", "composition"])) {
          cancelKeyFallback(el);
          if (inputType === "insertFromPaste") {
            consumePasteFallback(el);
            cancelShortcutFallback(el);
          }
          rememberSemantic(state, text, event.timeStamp, "input-fallback");
          queueType(el, text, "input-fallback", inputType, event.timeStamp);
        }
      }
      return;
    }
    clearTimeout(timers.get(event.target));
    pendingValueSnapshots.set(event.target, String(
      event.target.isContentEditable ? event.target.innerText : event.target.value));
    pendingValues.add(event.target);
    timers.set(event.target, setTimeout(() => sendValue(event.target), 450));
  }, true);
  document.addEventListener("change", event => sendValue(event.target), true);
  const pressModifiers = event => [
    event.ctrlKey && "Control",
    event.metaKey && "Meta",
    event.altKey && "Alt",
    event.shiftKey && "Shift",
  ].filter(Boolean);
  document.addEventListener("keydown", event => {
    flushPendingValues(event.target);
    const el = event.target;
    // A following key ends the prior shortcut's chance to produce richer
    // semantic paste evidence. Preserve the physical order in the stream.
    flushShortcutFallback(el);
    if ([" ", "Spacebar"].includes(event.key)) rememberBooleanBefore(el);
    const modifiers = pressModifiers(event);
    const shortcutKey = String(event.key || "").toLowerCase();
    // Clipboard and spreadsheet commands often mutate a canvas without an
    // input/beforeinput event. Previously those actions disappeared entirely:
    // the capture retained the source/destination clicks but not Copy, Paste,
    // Fill Down, Undo, and similar commands between them. Record command-key
    // shortcuts. For paste into a text proxy, wait one browser task first so
    // a richer paste/beforeinput payload can replace the keypress.
    if ((event.metaKey || event.ctrlKey)
        && ["a", "c", "d", "v", "x", "y", "z"].includes(shortcutKey)) {
      flushKeyFallback(el);
      flushType(el);
      if (shortcutKey === "v" && imeLike(el)) {
        const state = editState(el);
        cancelShortcutFallback(el);
        const fallback = {key: event.key, modifiers, timer: 0};
        fallback.timer = setTimeout(() => {
          if (state.shortcutFallback !== fallback) return;
          flushShortcutFallback(el);
        }, 0);
        state.shortcutFallback = fallback;
      } else {
        send("press", el, {key: event.key, modifiers});
      }
      return;
    }
    if (imeLike(el)) {
      if (event.key && event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
        const state = editState(el);
        cancelKeyFallback(el);
        const fallback = {text: event.key, eventTime: event.timeStamp, timer: 0};
        // Semantic beforeinput/input fires later in the same browser task and
        // cancels this.  If it never arrives, retain the old reliable fallback.
        fallback.timer = setTimeout(() => {
          if (state.keyFallback !== fallback) return;
          state.keyFallback = null;
          queueType(el, fallback.text, "keydown-fallback", "insertText",
                    fallback.eventTime);
        }, 0);
        state.keyFallback = fallback;
        return;
      }
      if (["Enter", "Escape", "Tab", "Backspace", "Delete"].includes(event.key)) {
        flushKeyFallback(el);
        flushType(el);
        send("press", el, {key: event.key, modifiers});
      }
      return;
    }
    if (["Enter", "Escape"].includes(event.key)) {
      send("press", event.target, {
        key: event.key,
        modifiers,
      });
    }
  }, true);
  // Browser-tab activation is outside the page DOM, but Chromium exposes the
  // resulting top-document visibility transition. Emitting the newly visible
  // page preserves a switch even when the operator performs no later gesture.
  const reportTabSwitch = () => {
    try {
      binding({type: "tab_switch", title: document.title || "",
               timestamp: Date.now()});
    } catch (_) { /* recorder teardown/navigation race */ }
  };
  document.addEventListener("visibilitychange", () => {
    if (window !== window.top) return;
    if (document.visibilityState === "hidden") {
      flushPendingValues(null);
      flushPendingTypes(null);
      return;
    }
    if (document.visibilityState === "visible") reportTabSwitch();
  }, true);
  // Announce this document. A begin whose document token predates the
  // frame's current one can never settle — its settle timer died with the
  // old document — so the host finalizes it as settlement "unavailable"
  // the moment the successor document appears.
  try {
    binding({type: "document_ready", doc_token: docToken,
             url: sanitizeUrl(location.href), timestamp: Date.now()});
  } catch (_) { /* recorder teardown/navigation race */ }
})();
