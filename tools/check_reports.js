#!/usr/bin/env node
/* Checks the message list (K.report): the same sentence does not pile up.
 *
 * Trigger: with an unstable dongle "Seit drei Minuten kommt nichts mehr aus
 * dem Auto" was shown again and again until it filled the whole screen.
 *
 *     node tools/check_reports.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

let failure = 0;
function verify(ok, text, extra) {
  console.log((ok ? "  ok    " : "  FEHLT ") + text + (ok ? "" : "   " + (extra || "")));
  if (!ok) failure += 1;
}

function makeBox() {
  const box = { dataset: {}, className: "", textContent: "", parent: null, handlers: {},
                addEventListener(name, f) { this.handlers[name] = f; },
                remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); } };
  return box;
}
const container = {
  children: [],
  appendChild(box) {
    this.children = this.children.filter((c) => c !== box);
    box.parent = this;
    this.children.push(box);
  },
};
const sandbox = {
  window: {},
  document: { getElementById: (id) => (id === "reports" ? container : null),
              createElement: () => makeBox() },
  setTimeout: () => 0, clearTimeout: () => {}, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "frontend", "core.js"), "utf8"), sandbox);
const K = sandbox.window.jolt;

const text = "Seit drei Minuten kommt nichts mehr aus dem Auto.";
const first = K.report(text, "fehler");
K.report(text, "fehler");
K.report(text, "fehler");
verify(container.children.length === 1, "derselbe Fehlertext steht nur einmal da",
       String(container.children.length));
verify(/\(3×\)/.test(first.textContent), "und zählt mit, wie oft er kam", first.textContent);

K.report("Etwas anderes", "fehler");
K.report(text, "fehler");
verify(container.children.length === 2 && container.children[1] === first,
       "ein neuer Treffer rückt ans Ende, andere Meldungen bleiben");

K.report(text, "hinweis");
verify(container.children.length === 3, "gleicher Text in anderer Art ist eine eigene Meldung");

first.remove();
verify(!container.children.includes(first), "die Meldung lässt sich wieder wegnehmen (Auto antwortet wieder)");

// A tap closes a message; at most three stay, the oldest non-error goes first.
const tapped = K.report("Zum Wegtippen", "hinweis");
tapped.handlers.click();
verify(!container.children.includes(tapped), "ein Tippen schliesst die Meldung");

container.children.length = 0;
K.report("Fehler A", "fehler");
K.report("Hinweis B", "hinweis");
K.report("Hinweis C", "hinweis");
K.report("Hinweis D", "hinweis");
verify(container.children.length === 3 && container.children[0].textContent === "Fehler A"
       && !container.children.some((c) => c.textContent === "Hinweis B"),
       "hoechstens drei Meldungen: der aelteste Hinweis geht zuerst, der Fehler bleibt",
       container.children.map((c) => c.textContent).join(" | "));
K.report("Fehler E", "fehler");
K.report("Fehler F", "fehler");
verify(container.children.length === 3 && container.children.filter((c) => c.dataset.kind === "fehler").length >= 2,
       "bei lauter Fehlern geht erst der aelteste, nie die neuesten");

if (failure) { console.log("\n" + failure + " Pruefung(en) fehlgeschlagen."); process.exit(1); }
console.log("\nAlle Pruefungen bestanden.");
