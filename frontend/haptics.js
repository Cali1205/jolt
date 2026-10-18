/* A little feedback under the finger, in the iOS app only.
 *
 * `window.joltHaptic.tap()` for a light touch (switching a tab, a button),
 * `.good()` / `.warn()` / `.bad()` for an outcome (a recording started, a
 * warning, an error). In a browser, or in an app built before the haptics
 * plugin was added, every call does nothing - nobody has to ask first.
 *
 * Throttled: a burst of messages must not turn into a buzzing phone.
 */
window.joltHaptic = (function () {
  "use strict";

  const MIN_GAP_MS = 400;
  let latest = 0;

  function plugin() {
    const shell = window.joltBlePlugin;
    if (!shell || !shell.Haptics || !shell.Capacitor || !shell.Capacitor.isNativePlatform()) return null;
    return shell.Haptics;
  }

  function fire(call, force) {
    const p = plugin();
    if (!p) return;
    const now = Date.now();
    if (!force && now - latest < MIN_GAP_MS) return;
    latest = now;
    try {
      const result = call(p);
      if (result && result.catch) result.catch(() => {});
    } catch (e) { /* no feedback is better than an error */ }
  }

  return {
    tap: () => fire((p) => p.impact({ style: "LIGHT" })),
    good: () => fire((p) => p.notification({ type: "SUCCESS" }), true),
    warn: () => fire((p) => p.notification({ type: "WARNING" })),
    bad: () => fire((p) => p.notification({ type: "ERROR" })),
    // For the message list: errors and warnings are felt, notes are not.
    report(kind) {
      if (kind === "fehler") this.bad();
      else if (kind === "warnung") this.warn();
    },
  };
})();
