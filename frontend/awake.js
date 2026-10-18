/* Keep the screen on while a trip is recorded.
 *
 * A module of its own because it needs nothing from the live view but
 * `K.report`. `hold()` when a recording starts or the page comes back to the
 * foreground, `release()` when it ends.
 */
window.joltAwake = (function () {
  "use strict";

  const K = window.jolt;

  /* Without this iOS turns the screen off after a minute, and with the
   * screen the page sleeps: `watchPosition` delivers nothing any more, the
   * Bluetooth loop stands still, and on waking up the piece in between is missing.
   *
   * The diagnostics page under /obd had this from the start, this view
   * did not - and recording happens here. In the first real test drive
   * there is exactly for that reason a gap of eight minutes with a single
   * measurement point in it.
   *
   * The lock is lost as soon as the page goes to the background, and
   * does not come back by itself; hence it is acquired again on
   * returning. */
  let wake_lock = null;
  let keepAwakeVideo = null;

  /* On iOS the Wake Lock API remains unreliable in a page launched as an
   * app from the home screen ("standalone", see manifest.json) -
   * sometimes the lock is not granted at all, sometimes it drops after a short
   * time by itself without triggering a "release" event. A
   * silent, invisible video running in an endless loop on the other hand
   * keeps the screen awake reliably - the same technique NoSleep.js
   * uses. Both paths run in parallel, neither harms the other.
   *
   * An empty canvas as the source (`captureStream()`) was not enough: iOS
   * apparently did not consistently count that as real playback, the
   * screen went off anyway. An actual, if tiny,
   * video (1 second, 2x2 pixels, black, no sound - 1.5 kB) works more
   * reliably. Embedded instead of as a separate file, so nothing has to be
   * loaded from the network before the lock takes effect. */
  const KEEP_AWAKE_MP4 = "data:video/mp4;base64,AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAMXbW9vdgAAAGxtdmhkAAAA"
    + "AAAAAAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAB"
    + "AAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAkF0"
    + "cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAAAAAA"
    + "AAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAIAAAACAAAAAAAkZWR0"
    + "cwAAABxlbHN0AAAAAAAAAAEAAAPoAAAAAAABAAAAAAG5bWRpYQAAACBtZGhkAAAAAAAA"
    + "AAAAAAAAAABAAAAAQABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABW"
    + "aWRlb0hhbmRsZXIAAAABZG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAA"
    + "HGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAASRzdGJsAAAAwHN0c2QAAAAAAAAAAQAA"
    + "ALBhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAIAAgBIAAAASAAAAAAAAAABFUxh"
    + "dmM1OS4zNy4xMDAgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANmF2Y0MBZAAK/+EAGWdk"
    + "AAqs2V+IiMBEAAADAAQAAAMACDxIllgBAAZo6+PLIsD9+PgAAAAAEHBhc3AAAAABAAAA"
    + "AQAAABRidHJ0AAAAAAAAFigAABYoAAAAGHN0dHMAAAAAAAAAAQAAAAEAAEAAAAAAHHN0"
    + "c2MAAAAAAAAAAQAAAAEAAAABAAAAAQAAABRzdHN6AAAAAAAAAsUAAAABAAAAFHN0Y28A"
    + "AAAAAAAAAQAAA0cAAABidWR0YQAAAFptZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGly"
    + "YXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAAJal0b28AAAAdZGF0YQAAAAEAAAAATGF2ZjU5"
    + "LjI3LjEwMAAAAAhmcmVlAAACzW1kYXQAAAKtBgX//6ncRem95tlIt5Ys2CDZI+7veDI2"
    + "NCAtIGNvcmUgMTY0IHIzMDk1IGJhZWU0MDAgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVj"
    + "IC0gQ29weWxlZnQgMjAwMy0yMDIyIC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2"
    + "NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MyBkZWJsb2NrPTE6MDowIGFuYWx5"
    + "c2U9MHgzOjB4MTEzIG1lPWhleCBzdWJtZT03IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAg"
    + "bWl4ZWRfcmVmPTEgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0xIDh4OGRj"
    + "dD0xIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zm"
    + "c2V0PS0yIHRocmVhZHM9MSBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRz"
    + "PTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29u"
    + "c3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTMgYl9weXJhbWlkPTIgYl9hZGFwdD0xIGJf"
    + "Ymlhcz0wIGRpcmVjdD0xIHdlaWdodGI9MSBvcGVuX2dvcD0wIHdlaWdodHA9MiBrZXlp"
    + "bnQ9MjUwIGtleWludF9taW49MSBzY2VuZWN1dD00MCBpbnRyYV9yZWZyZXNoPTAgcmNf"
    + "bG9va2FoZWFkPTQwIHJjPWNyZiBtYnRyZWU9MSBjcmY9MjMuMCBxY29tcD0wLjYwIHFw"
    + "bWluPTAgcXBtYXg9NjkgcXBzdGVwPTQgaXBfcmF0aW89MS40MCBhcT0xOjEuMDAAgAAA"
    + "ABBliIQAFf/+98nvwKbr29+B";

  /* Positioned outside the visible area (negative coordinates)
   * it did not hold - presumably a video that is not in the
   * visible area at all does not count as real playback for iOS. Now
   * it actually sits in the top left corner, just a single, almost
   * transparent pixel in size - that goes unnoticed, but counts. */
  function fetchVideoKeepAwake() {
    if (keepAwakeVideo) return keepAwakeVideo;
    const video = document.createElement("video");
    video.muted = true;
    video.loop = true;
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.setAttribute("webkit-playsinline", "");
    video.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;"
      + "opacity:0.01;pointer-events:none;z-index:-1;";
    video.src = KEEP_AWAKE_MP4;
    document.body.appendChild(video);
    keepAwakeVideo = video;
    return video;
  }

  /* Both paths report back, instead of only writing to the invisible console log -
   * at the wheel nobody can get at the console, and without a
   * visible response a failure could only be guessed at rather than seen. */
  /* In the iOS app this is one line, and it holds.
   *
   * `isIdleTimerDisabled` simply tells the system not to let the lock
   * timer run - no Wake Lock that gets revoked, no video that has to
   * count as playback. The path below stays in place anyway:
   * the UI also keeps running in the browser, and there is nothing
   * better there than Wake Lock and the video workaround. */
  async function nativeAwakeHold() {
    const h = window.joltBlePlugin;
    if (!h || !h.Capacitor || !h.Capacitor.isNativePlatform()) return false;
    try {
      await h.KeepAwake.keepAwake();
      return true;
    } catch (failure) {
      console.log("[live] KeepAwake ging nicht:", failure.message);
      return false;
    }
  }

  async function hold() {
    if (await nativeAwakeHold()) return;
    let hint = "";
    if (!wake_lock && "wakeLock" in navigator) {
      try {
        wake_lock = await navigator.wakeLock.request("screen");
        wake_lock.addEventListener("release", () => { wake_lock = null; });
      } catch (failure) {
        hint = "Wake Lock: " + failure.message;
      }
    } else if (!("wakeLock" in navigator)) {
      hint = "Wake Lock: vom Browser nicht unterstützt";
    }
    try {
      await fetchVideoKeepAwake().play();
    } catch (failure) {
      hint += (hint ? " / " : "") + "Video: " + failure.message;
    }
    if (hint) {
      console.log("[live] Bildschirm wachhalten:", hint);
      K.report("Bildschirm bleibt evtl. nicht an (" + hint + ") - "
        + "sicherheitshalber Automatische Sperre in den iOS-Einstellungen "
        + "auf „Nie” stellen.", "warnung");
    }
  }

  function release() {
    // After the trip the phone should lock normally again. The
    // native path is withdrawn first; the two below do no
    // harm if they never took hold in the first place.
    const h = window.joltBlePlugin;
    if (h && h.Capacitor && h.Capacitor.isNativePlatform()) {
      h.KeepAwake.allowSleep().catch(() => {});
    }
    if (wake_lock) {
      try { wake_lock.release(); } catch (e) {}
      wake_lock = null;
    }
    if (keepAwakeVideo) {
      try { keepAwakeVideo.pause(); } catch (e) {}
    }
  }

  return { hold, release };
})();
