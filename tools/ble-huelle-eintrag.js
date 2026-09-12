/* Eintrag fuer das gebuendelte Plugin-Paket, siehe package.json -> huelle.
 * Was hier steht, landet als window.joltBlePlugin in frontend/ble-plugin.js. */
import { BleClient } from '@capacitor-community/bluetooth-le';
import { KeepAwake } from '@capacitor-community/keep-awake';
import { Capacitor } from '@capacitor/core';
window.joltBlePlugin = { BleClient, KeepAwake, Capacitor };
