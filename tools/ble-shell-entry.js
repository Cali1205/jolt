/* Eintrag fuer das gebuendelte Plugin-Paket, siehe package.json -> huelle.
 * Was hier steht, landet als window.joltBlePlugin in frontend/ble-plugin.js. */
import { BleClient } from '@capacitor-community/bluetooth-le';
import { KeepAwake } from '@capacitor-community/keep-awake';
import { Capacitor } from '@capacitor/core';

/* Das Hintergrund-Standort-Plugin bringt keinen eigenen JS-Teil mit, nur den
 * nativen: Es wird ueber den Namen angesprochen, unter dem es sich in Swift
 * anmeldet. Im Browser gibt es dieses Plugin nicht - dort greift
 * `navigator.geolocation`, siehe frontend/live.js. */
const BackgroundGeolocation = Capacitor.registerPlugin('BackgroundGeolocation');

/* Die Live Activity: nimmt das Anzeigemodell (frontend/display.js) entgegen und
 * zeigt es auf dem Sperrbildschirm und im CarPlay-Dashboard. Der native Teil
 * liegt in plugins/jolt-anzeige. */
const JoltAnzeige = Capacitor.registerPlugin('JoltAnzeige');

window.joltBlePlugin = { BleClient, KeepAwake, Capacitor, BackgroundGeolocation, JoltAnzeige };
