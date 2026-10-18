/* A confirmation that comes up from the bottom of the screen.
 *
 * Instead of the system dialog (`window.confirm`), which looks foreign in the
 * app and cannot say which button is the dangerous one. One at a time; a tap
 * beside the sheet or the Escape key cancels. The button that does no harm
 * has the focus, so a stray Enter never confirms.
 *
 *     if (await window.joltSheet.confirm({ title: "Fahrt beenden?",
 *           text: "…", ok: "Beenden", danger: true })) { … }
 */
window.joltSheet = (function () {
  "use strict";

  let open = null;      // { close(result) } of the sheet that is on screen

  function confirm(options) {
    const o = options || {};
    if (open) open.close(false);
    return new Promise((fulfil) => {
      const back = document.createElement("div");
      back.className = "sheet-hintergrund";
      const sheet = document.createElement("div");
      sheet.className = "sheet";
      sheet.setAttribute("role", "dialog");
      sheet.setAttribute("aria-modal", "true");
      const grip = document.createElement("div");
      grip.className = "sheet-griff";
      const title = document.createElement("h3");
      title.textContent = o.title || "";
      const text = document.createElement("p");
      text.textContent = o.text || "";
      const yes = document.createElement("button");
      yes.className = "tat" + (o.danger ? " gefahr" : "");
      yes.textContent = o.ok || "OK";
      const no = document.createElement("button");
      no.className = "tat neben";
      no.textContent = o.cancel || "Abbrechen";
      for (const part of [grip, title, text, yes, no]) sheet.appendChild(part);
      back.appendChild(sheet);

      function onKey(event) {
        if (event.key === "Escape") close(false);
      }
      function close(result) {
        if (open !== handle) return;
        open = null;
        document.removeEventListener("keydown", onKey);
        back.remove();
        fulfil(result);
      }
      const handle = { close };
      open = handle;

      yes.addEventListener("click", () => { if (window.joltHaptic) window.joltHaptic.tap(); close(true); });
      no.addEventListener("click", () => { if (window.joltHaptic) window.joltHaptic.tap(); close(false); });
      back.addEventListener("click", (event) => { if (event.target === back) close(false); });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(back);
      no.focus();
    });
  }

  return { confirm };
})();
