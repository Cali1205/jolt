/* Bluetooth nativ, in der Gestalt von Web Bluetooth.
 *
 * Safari kennt `navigator.bluetooth` auf iOS nicht, und daran wird sich
 * nichts ändern. In der Web-Oberfläche hilft heute Bluefy, eine fremde App,
 * die die Web-API nachbildet. In der iOS-App übernimmt das hier: Capacitor
 * spricht über das Plugin `@capacitor-community/bluetooth-le` mit
 * CoreBluetooth, und diese Datei setzt dem OBD-Kern dieselbe Oberfläche
 * davor, die er vom Browser kennt.
 *
 * **Warum nachgebildet statt umgeschrieben.** In `obd-core.js` stecken
 * siebzehn Messwerte mit ihren Datenkennungen, die Adressumschaltung
 * zwischen 11 und 29 Bit und die Byte-Formeln - teils am Fahrzeug
 * erarbeitet, nicht abgeschrieben. Genau dort gehen Vorzeichen, Skalierung
 * und Bytereihenfolge bei einer Portierung still daneben, und ein falscher
 * Wert sieht plausibel aus. Diese Datei lässt den Kern deshalb unberührt:
 * Er redet weiter mit `requestDevice`, `gatt.connect` und
 * `characteristicvaluechanged`, nur beantwortet das hier CoreBluetooth
 * statt WebKit. Nachgebildet ist ausschliesslich, was der Kern benutzt -
 * das ist kein vollständiger Ersatz der Web-Bluetooth-Norm und will keiner
 * sein.
 *
 * Ist kein Capacitor da (normaler Browser, Bluefy), meldet `verfuegbar()`
 * falsch, und der Kern nimmt unverändert das echte `navigator.bluetooth`.
 */
window.joltBleNative = (function () {
  "use strict";

  /* Die gebündelte Hülle des Plugins, siehe frontend/ble-plugin.js. Sie
   * liegt als eigene Datei vor, weil die Oberfläche ohne Bauschritt
   * ausgeliefert wird - und weil sie den Draht nach nativ selbst kennt:
   * Werte gehen dort als Hex-Zeichenkette hinüber, durch eine
   * Warteschlange und mit normalisierten UUIDs. Das von Hand nachzubauen
   * wäre die Sorte Fehler, die erst im Auto auffällt. */
  function shell() { return window.joltBlePlugin || null; }

  function native() {
    const h = shell();
    return !!(h && h.Capacitor && h.Capacitor.isNativePlatform());
  }

  /* Die Gerätekennung überdauert den Neustart der App.
   *
   * Web Bluetooth merkt sich erteilte Erlaubnisse selbst - `getDevices()`
   * gibt sie ohne Zutun zurück. Das Plugin kann das nicht: Sein
   * `getDevices()` verlangt die Kennungen, nach denen es suchen soll. Ohne
   * diesen Merkposten käme nach jedem Start der Auswahldialog, und zwar
   * mitten in der Abfahrt. */
  const KEY = "jolt-ble-geraet";

  function rememberedId() {
    try { return window.localStorage.getItem(KEY) || null; }
    catch (failure) { return null; }
  }

  function idRemember(ident) {
    try { window.localStorage.setItem(KEY, ident); }
    catch (failure) { /* privater Modus - dann eben jedes Mal der Dialog */ }
  }

  function idForget() {
    try { window.localStorage.removeItem(KEY); }
    catch (failure) { /* nichts gemerkt, nichts zu tun */ }
  }

  let ready = false;

  async function prepare() {
    if (ready) return;
    await shell().BleClient.initialize();
    ready = true;
  }

  /* ---------- Charakteristik ---------- */

  /* `startNotifications()` und `addEventListener` sind in Web Bluetooth zwei
   * Schritte, beim Plugin ist es einer: Der Rückruf wird beim Anmelden
   * übergeben. Der Kern ruft aber erst `startNotifications()` und hängt
   * sich **danach** ein. Deshalb sammelt dieses Objekt die Zuhörer und
   * verteilt an sie, was das Plugin liefert - unabhängig davon, wann sie
   * dazugekommen sind. */
  function charakteristik(ident, serviceUuid, charUuid, attrs) {
    const listener = [];
    let signed_in = false;

    function distribute(val) {
      // Web Bluetooth reicht ein Ereignis mit `target.value` herein, und
      // genau darauf greift `beiDaten` im Kern zu. `wert` ist bereits ein
      // DataView, den `TextDecoder` direkt verarbeitet.
      const event = { target: { value: val } };
      for (const call of listener.slice()) {
        try { call(event); } catch (failure) { /* ein Zuhörer darf scheitern */ }
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

  /* Der Kern schickt ein Uint8Array (aus `TextEncoder`), das Plugin will
   * einen DataView. `byteOffset` und `byteLength` gehören mit übergeben:
   * Ein Uint8Array kann ein Ausschnitt eines grösseren Puffers sein, und
   * ohne die beiden ginge dann der ganze Puffer hinaus. */
  function asDataView(records) {
    if (records instanceof DataView) return records;
    const field = records instanceof Uint8Array ? records : new Uint8Array(records);
    return new DataView(field.buffer, field.byteOffset, field.byteLength);
  }

  /* ---------- Gerät ---------- */

  function device(ident, name) {
    const dropout_listener = [];
    let linked = false;

    function reportDropout() {
      linked = false;
      for (const call of dropout_listener.slice()) {
        try { call(); } catch (failure) { /* siehe oben */ }
      }
    }

    const gatt = {
      get connected() { return linked; },

      async connect() {
        await prepare();
        // Der Abriss-Rückruf gehört hier hinein und nicht in ein eigenes
        // Ereignis: Das Plugin kennt nur diesen einen Weg, und der Kern
        // hängt sich über `gattserverdisconnected` ein, bevor er verbindet.
        await shell().BleClient.connect(ident, reportDropout);
        linked = true;
        return server;
      },

      async disconnect() {
        linked = false;
        try { await shell().BleClient.disconnect(ident); }
        catch (failure) { /* schon getrennt ist kein Fehler */ }
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

  /* ---------- Die nachgebildete Schnittstelle ---------- */

  /* Der Kern probiert vier Gestalten von `requestDevice` durch, weil sich
   * die Browser darin unterscheiden. Nativ gibt es diesen Unterschied
   * nicht: Das Plugin zeigt eine eigene Geräteliste. Die Filter werden
   * deshalb bewusst **nicht** übersetzt - eine ungefilterte Liste, aus der
   * einmal der Dongle gewählt wird, ist hier das Richtige, und danach
   * greift ohnehin die gemerkte Kennung. */
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

  /* Ein weggetippter Dialog muss aussehen wie in Web Bluetooth.
   *
   * Der Kern bricht seine Variantenschleife ab, wenn er einen
   * `NotFoundError` mit "cancel" darin sieht - sonst öffnet er den Dialog
   * noch drei weitere Male. Das Plugin meldet den Abbruch anders, also
   * wird er hier in die Gestalt gebracht, auf die der Kern prüft. */
  function asAbort(failure) {
    const text = (failure && failure.message) || String(failure);
    if (/cancel|abbruch|abort|dismiss|denied/i.test(text)) {
      const fresh = new Error("User cancelled the requestDevice() chooser.");
      fresh.name = "NotFoundError";
      return fresh;
    }
    return failure instanceof Error ? failure : new Error(text);
  }

  /* Ohne Dialog wiederfinden, was schon einmal gewählt wurde. Gibt eine
   * Liste zurück wie Web Bluetooth, damit der Kern nicht unterscheiden
   * muss - sie ist nur nie länger als ein Eintrag. */
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
    /* Wahr nur in der nativen App **und** wenn die Hülle geladen ist.
     * Beides einzeln reicht nicht: Im Browser fehlt Capacitor, und ohne
     * die gebündelte Datei gäbe es kein BleClient. */
    obtainable() { return native() && !!shell().BleClient; },
    bluetooth: { requestDevice, getDevices },
    /* Für die Fehlersuche auf der Diagnoseseite. */
    ident: rememberedId,
    /* Das gemerkte Gerät löschen (Einstellungen): danach fragt der nächste
     * Aufbau wieder mit dem Auswahldialog. */
    forget: idForget,
  };
})();
