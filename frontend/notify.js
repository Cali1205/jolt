/* Notifications: the local one for a changed plan, and the registration for
 * web push, which is the only route that reaches a phone with a dark screen.
 */
window.joltNotify = (function () {
  "use strict";

  const K = window.jolt;

  function notify(text) {
    // Without granted permission we neither ask nor notify:
    // whoever has the view open sees the message anyway. We ask
    // once at the start of the trip, where the question also means something.
    try {
      if (!("Notification" in window) || Notification.permission !== "granted") {
        return;
      }
      new Notification("jolt – Ladeplan geändert", { body: text, day: "jolt-plan" });
    } catch (e) { /* depending on browser and context not allowed - then so be it */ }
  }

  /* Ask once at the start of the trip and register the device.
   *
   * The route via the push service is the only one that reaches a phone with
   * a dark screen: the WebSocket connection goes to sleep with it. Hence
   * a real subscription here and not just the permission for the Notification API.
   *
   * If any step fails, the trip runs anyway - then just with
   * the message in the open view. */
  async function notificationsSetUp() {
    try {
      if (!("Notification" in window) || !("PushManager" in window)) return;

      const key = await K.api("/api/push/key");
      if (!key.configured) return;   // no VAPID key on the server

      if (Notification.permission === "default") {
        await Notification.requestPermission();
      }
      if (Notification.permission !== "granted") return;

      const registration = K.state.serviceWorker
        || (navigator.serviceWorker && await navigator.serviceWorker.ready);
      if (!registration || !registration.pushManager) return;

      // Reuse an existing subscription. Creating a new one would return
      // the same endpoint, but costs a detour.
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: keyAsBytes(key.public_key),
        });
      }

      const subscriptionJson = subscription.toJSON();
      await K.api("/api/push/subscription", { method: "POST", body: {
        endpoint: subscriptionJson.endpoint,
        p256dh: subscriptionJson.keys.p256dh,
        auth: subscriptionJson.keys.auth,
        device: navigator.userAgent.slice(0, 120),
      }});
    } catch (failure) {
      // Deliberately only to the log: whoever is just setting off does not want to read an error
      // message about a side function.
      if (window.console) console.warn("Benachrichtigungen:", failure.message);
    }
  }

  /* The public key arrives as base64url and must be passed as a
   * Uint8Array - the browser does not accept the string. */
  function keyAsBytes(text) {
    const filled = (text + "=".repeat((4 - text.length % 4) % 4))
      .replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(filled);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  return { show: notify, subscribe: notificationsSetUp };
})();
