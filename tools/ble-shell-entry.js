/* Entry point for the bundled plugin package, see package.json -> huelle.
 * Whatever is exported here ends up as window.joltBlePlugin in frontend/ble-plugin.js. */
import { BleClient } from '@capacitor-community/bluetooth-le';
import { KeepAwake } from '@capacitor-community/keep-awake';
import { Capacitor } from '@capacitor/core';

/* The background location plugin has no JS part of its own, only the
 * native one: it is addressed by the name it registers under in Swift.
 * The browser has no such plugin - there `navigator.geolocation` is used,
 * see frontend/live.js. */
const BackgroundGeolocation = Capacitor.registerPlugin('BackgroundGeolocation');

/* The Live Activity: takes the display model (frontend/display.js) and
 * shows it on the lock screen and in the CarPlay dashboard. The native part
 * lives in plugins/jolt-display. */
const JoltDisplay = Capacitor.registerPlugin('JoltDisplay');

window.joltBlePlugin = { BleClient, KeepAwake, Capacitor, BackgroundGeolocation, JoltDisplay };
