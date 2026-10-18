#!/usr/bin/env node
// Checks the confirmation sheet (frontend/sheet.js) against a small stand-in DOM.
//
//     node tools/check_sheet.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let failure = 0;
function verify(ok, text, detail) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "  -> " + detail));
  if (!ok) failure++;
}

function build() {
  const listeners = {};
  const body = { children: [], appendChild(c) { this.children.push(c); c.parent = this; } };
  function element(tag) {
    return { tag, className: "", textContent: "", attrs: {}, children: [], handlers: {}, focused: false,
             setAttribute(k, v) { this.attrs[k] = v; },
             appendChild(c) { this.children.push(c); c.parent = this; },
             addEventListener(n, f) { this.handlers[n] = f; },
             focus() { this.focused = true; },
             remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); } };
  }
  const document = {
    body, createElement: element,
    addEventListener(n, f) { listeners[n] = f; },
    removeEventListener(n) { delete listeners[n]; },
  };
  const timeframe = {};
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", "sheet.js"), "utf8"),
                  vm.createContext(Object.assign(Object.create(null), { window: timeframe, document })));
  const back = () => body.children[0];
  const sheet = () => back().children[0];
  const button = (label) => sheet().children.find((c) => c.tag === "button" && c.textContent === label);
  return { sheet: timeframe.joltSheet, body, listeners, back, sheetEl: sheet, button };
}

(async () => {
  let t = build();
  let answer = t.sheet.confirm({ title: "Fahrt beenden?", text: "Das lässt sich nicht rückgängig machen.",
                                 ok: "Beenden", danger: true });
  verify(t.body.children.length === 1 && t.sheetEl().attrs.role === "dialog",
         "das Sheet erscheint als Dialog");
  verify(t.sheetEl().children.some((c) => c.tag === "h3" && c.textContent === "Fahrt beenden?"),
         "mit Titel und Text (als Text, nicht als HTML)");
  verify(/gefahr/.test(t.button("Beenden").className), "die bestätigende Taste ist als gefährlich markiert");
  verify(t.button("Abbrechen").focused && !t.button("Beenden").focused,
         "der Fokus liegt auf Abbrechen - ein versehentliches Enter bestätigt nichts");
  t.button("Beenden").handlers.click();
  verify((await answer) === true && t.body.children.length === 0, "Bestätigen liefert true und räumt auf");

  t = build();
  answer = t.sheet.confirm({ title: "x" });
  t.button("Abbrechen").handlers.click();
  verify((await answer) === false, "Abbrechen liefert false");

  t = build();
  answer = t.sheet.confirm({ title: "x" });
  t.back().handlers.click({ target: t.back() });
  verify((await answer) === false && t.body.children.length === 0, "ein Tippen neben das Sheet bricht ab");

  t = build();
  answer = t.sheet.confirm({ title: "x" });
  t.back().handlers.click({ target: t.sheetEl() });
  verify(t.body.children.length === 1, "ein Tippen auf das Sheet selbst lässt es stehen");
  t.listeners.keydown({ key: "Escape" });
  verify((await answer) === false && !t.listeners.keydown, "Escape bricht ab und der Tastenfang wird abgehängt");

  t = build();
  const first = t.sheet.confirm({ title: "eins" });
  const second = t.sheet.confirm({ title: "zwei" });
  verify((await first) === false && t.body.children.length === 1
         && t.sheetEl().children[1].textContent === "zwei",
         "es ist immer nur eines offen: das zweite ersetzt das erste (das erste gilt als abgelehnt)");
  t.button("OK").handlers.click();
  verify((await second) === true, "und das zweite lässt sich normal beantworten");

  if (failure) { console.log("\n" + failure + " Pruefung(en) fehlgeschlagen."); process.exit(1); }
  console.log("\nAlle Pruefungen bestanden.");
})();
