/* Bluetooth natively, in the shape of Web Bluetooth.
 *
 * Safari does not know `navigator.bluetooth` on iOS, and that will not
 * change. In the web UI Bluefy helps today, a third-party app that
 * emulates the Web API. In the iOS app this file takes over: Capacitor
 * talks to CoreBluetooth through the plugin
 * `@capacitor-community/bluetooth-le`, and this file puts the same
 * surface in front of the OBD core that it knows from the browser.
 *
 * **Why emulated instead of rewritten.** `obd-core.js` contains seventeen
 * readings with their data identifiers, the address switching between 11
 * and 29 bit and the byte formulas - partly worked out at the vehicle,
 * not copied. This is exactly where sign, scaling and byte order silently
 * go wrong in a port, and a wrong value looks plausible. This file
 * therefore leaves the core untouched: it keeps talking to
 * `requestDevice`, `gatt.connect` and `characteristicvaluechanged`, it is
 * just CoreBluetooth answering instead of WebKit. Only what the core uses
 * is emulated - this is not a complete replacement of the Web Bluetooth
 * standard and does not want to be one.
 *
 * If there is no Capacitor (normal browser, Bluefy), `obtainable()`
 * reports false, and the core unchangedly takes the real
 * `navigator.bluetooth`.
 */
window.joltBleNative = (function () {
  "use strict";

  /* The bundled shell of the plugin, see frontend/ble-plugin.js. It exists
   * as its own file because the UI is delivered without a build step - and
   * because it knows the wire to native itself: values cross over there as
   * a hex string, through a queue and with normalized UUIDs. Rebuilding
   * that by hand would be the kind of error that only shows up in the
   * car. */
  function shell() { return window.joltBlePlugin || null; }

  function native() {
    const h = shell();
    return !!(h && h.Capacitor && h.Capacitor.isNativePlatform());
  }

  /* The device identifier survives restarting the app.
   *
   * Web Bluetooth remembers granted permissions itself - `getDevices()`
   * returns them without further ado. The plugin cannot do that: its
   * `getDevices()` requires the identifiers to search for. Without this
   * memo, the selection dialog would appear after every start, in the
   * middle of the departure. */
  const KEY = "jolt-ble-geraet";

  function rememberedId() {
    try { return window.localStorage.getItem(KEY) || null; }
    catch (failure) { return null; }
  }

  function idRemember(ident) {
    try { window.localStorage.setItem(KEY, ident); }
    catch (failure) { /* private mode - then the dialog every time */ }
  }

  function idForget() {
    try { window.localStorage.removeItem(KEY); }
    catch (failure) { /* nothing remembered, nothing to do */ }
  }

  let ready = false;

  async function prepare() {
    if (ready) return;
    await shell().BleClient.initialize();
    ready = true;
  }

  /* ---------- Characteristic ---------- */

  /* `startNotifications()` and `addEventListener` are two steps in Web
   * Bluetooth; with the plugin it is one: the callback is passed on
   * subscribing. The core, however, first calls `startNotifications()` and
   * hooks in **afterwards**. That is why this object collects the
   * listeners and distributes to them whatever the plugin delivers -
   * regardless of when they were added. */
  function charakteristik(ident, serviceUuid, charUuid, attrs) {
    const listener = [];
    let signed_in = false;

    function distribute(val) {
      // Web Bluetooth passes in an event with `target.value`, and that is
      // exactly what `atData` in the core accesses. `val` is already a
      // DataView, which `TextDecoder` processes directly.
      const event = { target: { value: val } };
      for (const call of listener.slice()) {
        try { call(event); } catch (failure) { /* a listener may fail */ }
      }
    }

    return {
      uuid: charUuid,
      properties: attrs,

      async startNotifications() {
        if (!signed_in) {
          await shell().BleClient.startNotifications(
            ident, serviceUuid, charUuid, distribute);
          signed_in = true;
        }
        return this;
      },

      addEventListener(name, call) {
        if (name === "characteristicvaluechanged") listener.push(call);
      },

      removeEventListener(name, call) {
        if (name !== "characteristicvaluechanged") return;
        const i = listener.indexOf(call);
        if (i >= 0) listener.splice(i, 1);
      },

      writeValue(records) {
        return shell().BleClient.write(
          ident, serviceUuid, charUuid, asDataView(records));
      },

      writeValueWithoutResponse(records) {
        return shell().BleClient.writeWithoutResponse(
          ident, serviceUuid, charUuid, asDataView(records));
      },
    };
  }

  /* The core sends a Uint8Array (from `TextEncoder`), the plugin wants a
   * DataView. `byteOffset` and `byteLength` have to be passed along: a
   * Uint8Array can be a slice of a larger buffer, and without the two the
   * whole buffer would go out. */
  function asDataView(records) {
    if (records instanceof DataView) return records;
    const field = records instanceof Uint8Array ? records : new Uint8Array(records);
    return new DataView(field.buffer, field.byteOffset, field.byteLength);
  }

  /* ---------- Device ---------- */

  function device(ident, name) {
    const dropout_listener = [];
    let linked = false;

    function reportDropout() {
      linked = false;
      for (const call of dropout_listener.slice()) {
        try { call(); } catch (failure) { /* see above */ }
      }
    }

    const gatt = {
      get connected() { return linked; },

      async connect() {
        await prepare();
        // The dropout callback belongs in here and not in an event of its own:
        // the plugin knows only this one way, and the core hooks in via
        // `gattserverdisconnected` before it connects.
        await shell().BleClient.connect(ident, reportDropout);
        linked = true;
        return server;
      },

      async disconnect() {
        linked = false;
        try { await shell().BleClient.disconnect(ident); }
        catch (failure) { /* already disconnected is not an error */ }
      },
    };

    const server = {
      get connected() { return linked; },

      async getPrimaryServices() {
        const services = await shell().BleClient.getServices(ident);
        return services.map((d) => ({
          uuid: d.uuid,
          getCharacteristics() {
            return Promise.resolve((d.characteristics || []).map(
              (c) => charakteristik(ident, d.uuid, c.uuid,
                                    c.properties || {})));
          },
        }));
      },
    };

    return {
      id: ident,
      name: name || null,
      gatt,
      addEventListener(event, call) {
        if (event === "gattserverdisconnected") dropout_listener.push(call);
      },
      removeEventListener(event, call) {
        if (event !== "gattserverdisconnected") return;
        const i = dropout_listener.indexOf(call);
        if (i >= 0) dropout_listener.splice(i, 1);
      },
    };
  }

  /* ---------- The emulated interface ---------- */

  /* The core tries four shapes of `requestDevice` in turn, because
   * browsers differ in that. Natively there is no such difference: the
   * plugin shows a device list of its own. The filters are therefore
   * deliberately **not** translated - an unfiltered list from which the
   * dongle is chosen once is the right thing here, and after that the
   * remembered identifier takes over anyway. */
  async function requestDevice(options) {
    await prepare();
    const services = (options && options.optionalServices) || [];
    let chosen;
    try {
      chosen = await shell().BleClient.requestDevice({
        optionalServices: services,
      });
    } catch (failure) {
      throw asAbort(failure);
    }
    idRemember(chosen.deviceId);
    return device(chosen.deviceId, chosen.name);
  }

  /* A dismissed dialog must look like in Web Bluetooth.
   *
   * The core breaks off its variant loop when it sees a `NotFoundError`
   * with "cancel" in it - otherwise it opens the dialog three more times.
   * The plugin reports the cancellation differently, so it is brought into
   * the shape the core checks for here. */
  function asAbort(failure) {
    const text = (failure && failure.message) || String(failure);
    if (/cancel|abbruch|abort|dismiss|denied/i.test(text)) {
      const fresh = new Error("User cancelled the requestDevice() chooser.");
      fresh.name = "NotFoundError";
      return fresh;
    }
    return failure instanceof Error ? failure : new Error(text);
  }

  /* Find again without a dialog what was chosen once before. Returns a
   * list like Web Bluetooth, so that the core need not distinguish - it is
   * just never longer than one entry. */
  async function getDevices() {
    const ident = rememberedId();
    if (!ident) return [];
    await prepare();
    try {
      const found = await shell().BleClient.getDevices([ident]);
      return (found || []).map((g) => device(g.deviceId, g.name));
    } catch (failure) {
      return [];
    }
  }

  return {
    /* True only in the native app **and** when the shell is loaded. Either
     * alone is not enough: in the browser Capacitor is missing, and without
     * the bundled file there would be no BleClient. */
    obtainable() { return native() && !!shell().BleClient; },
    bluetooth: { requestDevice, getDevices },
    /* For troubleshooting on the diagnostics page. */
    ident: rememberedId,
    /* Delete the remembered device (settings): the next setup then asks
     * with the selection dialog again. */
    forget: idForget,
  };
})();
