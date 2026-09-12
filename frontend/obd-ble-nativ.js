/* Bluetooth nativ, in der Gestalt von Web Bluetooth.
 *
 * Safari kennt `navigator.bluetooth` auf iOS nicht, und daran wird sich
 * nichts ändern. In der Web-Oberfläche hilft heute Bluefy, eine fremde App,
 * die die Web-API nachbildet. In der iOS-App übernimmt das hier: Capacitor
 * spricht über das Plugin `@capacitor-community/bluetooth-le` mit
 * CoreBluetooth, und diese Datei setzt dem OBD-Kern dieselbe Oberfläche
 * davor, die er vom Browser kennt.
 *
 * **Warum nachgebildet statt umgeschrieben.** In `obd-kern.js` stecken
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
window.joltBleNativ = (function () {
  "use strict";

  /* Die gebündelte Hülle des Plugins, siehe frontend/ble-plugin.js. Sie
   * liegt als eigene Datei vor, weil die Oberfläche ohne Bauschritt
   * ausgeliefert wird - und weil sie den Draht nach nativ selbst kennt:
   * Werte gehen dort als Hex-Zeichenkette hinüber, durch eine
   * Warteschlange und mit normalisierten UUIDs. Das von Hand nachzubauen
   * wäre die Sorte Fehler, die erst im Auto auffällt. */
  function huelle() { return window.joltBlePlugin || null; }

  function nativ() {
    const h = huelle();
    return !!(h && h.Capacitor && h.Capacitor.isNativePlatform());
  }

  /* Die Gerätekennung überdauert den Neustart der App.
   *
   * Web Bluetooth merkt sich erteilte Erlaubnisse selbst - `getDevices()`
   * gibt sie ohne Zutun zurück. Das Plugin kann das nicht: Sein
   * `getDevices()` verlangt die Kennungen, nach denen es suchen soll. Ohne
   * diesen Merkposten käme nach jedem Start der Auswahldialog, und zwar
   * mitten in der Abfahrt. */
  const SCHLUESSEL = "jolt-ble-geraet";

  function gemerkteKennung() {
    try { return window.localStorage.getItem(SCHLUESSEL) || null; }
    catch (fehler) { return null; }
  }

  function kennungMerken(kennung) {
    try { window.localStorage.setItem(SCHLUESSEL, kennung); }
    catch (fehler) { /* privater Modus - dann eben jedes Mal der Dialog */ }
  }

  let bereit = false;

  async function vorbereiten() {
    if (bereit) return;
    await huelle().BleClient.initialize();
    bereit = true;
  }

  /* ---------- Charakteristik ---------- */

  /* `startNotifications()` und `addEventListener` sind in Web Bluetooth zwei
   * Schritte, beim Plugin ist es einer: Der Rückruf wird beim Anmelden
   * übergeben. Der Kern ruft aber erst `startNotifications()` und hängt
   * sich **danach** ein. Deshalb sammelt dieses Objekt die Zuhörer und
   * verteilt an sie, was das Plugin liefert - unabhängig davon, wann sie
   * dazugekommen sind. */
  function charakteristik(kennung, dienstUuid, charUuid, eigenschaften) {
    const zuhoerer = [];
    let angemeldet = false;

    function verteilen(wert) {
      // Web Bluetooth reicht ein Ereignis mit `target.value` herein, und
      // genau darauf greift `beiDaten` im Kern zu. `wert` ist bereits ein
      // DataView, den `TextDecoder` direkt verarbeitet.
      const ereignis = { target: { value: wert } };
      for (const ruf of zuhoerer.slice()) {
        try { ruf(ereignis); } catch (fehler) { /* ein Zuhörer darf scheitern */ }
      }
    }

    return {
      uuid: charUuid,
      properties: eigenschaften,

      async startNotifications() {
        if (!angemeldet) {
          await huelle().BleClient.startNotifications(
            kennung, dienstUuid, charUuid, verteilen);
          angemeldet = true;
        }
        return this;
      },

      addEventListener(name, ruf) {
        if (name === "characteristicvaluechanged") zuhoerer.push(ruf);
      },

      removeEventListener(name, ruf) {
        if (name !== "characteristicvaluechanged") return;
        const i = zuhoerer.indexOf(ruf);
        if (i >= 0) zuhoerer.splice(i, 1);
      },

      writeValue(daten) {
        return huelle().BleClient.write(
          kennung, dienstUuid, charUuid, alsDataView(daten));
      },

      writeValueWithoutResponse(daten) {
        return huelle().BleClient.writeWithoutResponse(
          kennung, dienstUuid, charUuid, alsDataView(daten));
      },
    };
  }

  /* Der Kern schickt ein Uint8Array (aus `TextEncoder`), das Plugin will
   * einen DataView. `byteOffset` und `byteLength` gehören mit übergeben:
   * Ein Uint8Array kann ein Ausschnitt eines grösseren Puffers sein, und
   * ohne die beiden ginge dann der ganze Puffer hinaus. */
  function alsDataView(daten) {
    if (daten instanceof DataView) return daten;
    const feld = daten instanceof Uint8Array ? daten : new Uint8Array(daten);
    return new DataView(feld.buffer, feld.byteOffset, feld.byteLength);
  }

  /* ---------- Gerät ---------- */

  function geraet(kennung, name) {
    const abrisszuhoerer = [];
    let verbunden = false;

    function abrissMelden() {
      verbunden = false;
      for (const ruf of abrisszuhoerer.slice()) {
        try { ruf(); } catch (fehler) { /* siehe oben */ }
      }
    }

    const gatt = {
      get connected() { return verbunden; },

      async connect() {
        await vorbereiten();
        // Der Abriss-Rückruf gehört hier hinein und nicht in ein eigenes
        // Ereignis: Das Plugin kennt nur diesen einen Weg, und der Kern
        // hängt sich über `gattserverdisconnected` ein, bevor er verbindet.
        await huelle().BleClient.connect(kennung, abrissMelden);
        verbunden = true;
        return server;
      },

      async disconnect() {
        verbunden = false;
        try { await huelle().BleClient.disconnect(kennung); }
        catch (fehler) { /* schon getrennt ist kein Fehler */ }
      },
    };

    const server = {
      get connected() { return verbunden; },

      async getPrimaryServices() {
        const dienste = await huelle().BleClient.getServices(kennung);
        return dienste.map((d) => ({
          uuid: d.uuid,
          getCharacteristics() {
            return Promise.resolve((d.characteristics || []).map(
              (c) => charakteristik(kennung, d.uuid, c.uuid,
                                    c.properties || {})));
          },
        }));
      },
    };

    return {
      id: kennung,
      name: name || null,
      gatt,
      addEventListener(ereignis, ruf) {
        if (ereignis === "gattserverdisconnected") abrisszuhoerer.push(ruf);
      },
      removeEventListener(ereignis, ruf) {
        if (ereignis !== "gattserverdisconnected") return;
        const i = abrisszuhoerer.indexOf(ruf);
        if (i >= 0) abrisszuhoerer.splice(i, 1);
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
  async function requestDevice(optionen) {
    await vorbereiten();
    const dienste = (optionen && optionen.optionalServices) || [];
    let gewaehlt;
    try {
      gewaehlt = await huelle().BleClient.requestDevice({
        optionalServices: dienste,
      });
    } catch (fehler) {
      throw alsAbbruch(fehler);
    }
    kennungMerken(gewaehlt.deviceId);
    return geraet(gewaehlt.deviceId, gewaehlt.name);
  }

  /* Ein weggetippter Dialog muss aussehen wie in Web Bluetooth.
   *
   * Der Kern bricht seine Variantenschleife ab, wenn er einen
   * `NotFoundError` mit "cancel" darin sieht - sonst öffnet er den Dialog
   * noch drei weitere Male. Das Plugin meldet den Abbruch anders, also
   * wird er hier in die Gestalt gebracht, auf die der Kern prüft. */
  function alsAbbruch(fehler) {
    const text = (fehler && fehler.message) || String(fehler);
    if (/cancel|abbruch|abort|dismiss|denied/i.test(text)) {
      const neu = new Error("User cancelled the requestDevice() chooser.");
      neu.name = "NotFoundError";
      return neu;
    }
    return fehler instanceof Error ? fehler : new Error(text);
  }

  /* Ohne Dialog wiederfinden, was schon einmal gewählt wurde. Gibt eine
   * Liste zurück wie Web Bluetooth, damit der Kern nicht unterscheiden
   * muss - sie ist nur nie länger als ein Eintrag. */
  async function getDevices() {
    const kennung = gemerkteKennung();
    if (!kennung) return [];
    await vorbereiten();
    try {
      const gefunden = await huelle().BleClient.getDevices([kennung]);
      return (gefunden || []).map((g) => geraet(g.deviceId, g.name));
    } catch (fehler) {
      return [];
    }
  }

  return {
    /* Wahr nur in der nativen App **und** wenn die Hülle geladen ist.
     * Beides einzeln reicht nicht: Im Browser fehlt Capacitor, und ohne
     * die gebündelte Datei gäbe es kein BleClient. */
    verfuegbar() { return nativ() && !!huelle().BleClient; },
    bluetooth: { requestDevice, getDevices },
    /* Für die Fehlersuche auf der Diagnoseseite. */
    kennung: gemerkteKennung,
  };
})();
