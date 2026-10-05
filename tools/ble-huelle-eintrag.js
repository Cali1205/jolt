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

window.joltBlePlugin = { BleClient, KeepAwake, Capacitor, BackgroundGeolocation };
