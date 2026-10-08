# jolt as an iPhone app — setup without a Mac

This document walks you once through everything Apple requires before
`.github/workflows/ios-testflight.yml` can send a signed app to TestFlight.
**No Mac is needed.** Everything below runs in the browser and with `openssl`
on any machine — including the unRAID server.

After that, day-to-day use is one click: *Actions → iOS TestFlight → Run workflow*.

> **Status.** The files in the repository are finished and checked for internal
> consistency (`tools/check_ios.py`), but a signed build cannot be tried out
> without a developer account. The first real run is therefore the test; if it
> fails, the section [If something goes wrong](#if-something-goes-wrong) lists
> the usual causes.

## What you end up with

- The app `jolt` on the iPhone, installed through the Apple app **TestFlight**.
- Bluetooth to the OBD dongle natively via CoreBluetooth instead of via Bluefy.
- A UI that still comes **from the server** (`server.url` in
  `capacitor.config.json`): a changed line in `live.js` reaches the app through
  the container as before and needs **no** new app build. You only have to
  upload again when the shell changes — plugins, permissions, icon.

## Bundle ID

The app ID is **`de.thesmarthome.jolt`** (without a hyphen). Apple allows
hyphens, but Capacitor rejects them (`cap add ios` aborts with "Invalid
App ID") — with the originally planned ID (hyphen in "the-smarthome") the
iOS run on `main` therefore never turned green. The ID lives in exactly one place: `capacitor.config.json`.
The workflow reads it from there; you only need to change it if you want a
different one — then enter the same one in step 2.

---

## 1. Apple Developer Program

<https://developer.apple.com/programs/enroll/> — 99 USD/year; an individual
account is enough. Activation takes hours to two days.

Afterwards, note down the **Team ID** (10 characters, uppercase letters and
digits) under <https://developer.apple.com/account> → *Membership details*.

## 2. Create the App ID

<https://developer.apple.com/account/resources/identifiers/list> → **+** →
*App IDs* → *App* → continue.

- Description: `jolt`
- Bundle ID: *Explicit*, `de.thesmarthome.jolt`
- Capabilities: tick **none**. Bluetooth and location are declared in the
  `Info.plist` (`tools/ios_info_plist.sh`), not as a capability here.

## 3. and 4. only for the manual route

**The default is the automatic route: you need neither a certificate nor a profile.**
The workflow signs in to Apple with the API key from step 6, and Apple creates
both itself ("Cloud Managed Signing"). Skip straight to steps 5 and 6. Sections
3 and 4 are the **fallback** in case the automatic route fails (see the error
table): if the three additional secrets from step 7 are set, the workflow signs
with them instead.

### 3. Distribution certificate — with `openssl`

The certificate proves to Apple that the build comes from you. You generate the
private key yourself; it leaves your machine only as a repository secret.

```bash
mkdir -p ~/jolt-signing && cd ~/jolt-signing

# Private key + certificate signing request (CSR)
openssl genrsa -out jolt.key 2048
openssl req -new -key jolt.key -out jolt.csr \
  -subj "/emailAddress=YOUR@MAIL.COM/CN=jolt Distribution/C=DE"
```

In the portal: <https://developer.apple.com/account/resources/certificates/add> →
**Apple Distribution** → upload `jolt.csr` → download the generated
`distribution.cer`. Then combine it into the `.p12` that GitHub needs:

```bash
openssl x509 -inform DER -in distribution.cer -out distribution.pem

# OpenSSL 3 (most current systems): -legacy is required, otherwise
# `security import` on the Mac runner fails with "MAC verification failed".
# OpenSSL 1.1 does not know the option - leave it out there.
openssl pkcs12 -export -legacy -inkey jolt.key -in distribution.pem \
  -name "jolt Distribution" -out jolt.p12
# It asks for a password. Use a long, random one - it becomes the
# secret IOS_CERT_PASSWORD.
```

Only a few distribution certificates are possible per account, and the
certificate expires after a year. After it expires, repeat this step and step 4.

### 4. Provisioning profile

<https://developer.apple.com/account/resources/profiles/add> → *Distribution*
→ **App Store Connect** → App ID `de.thesmarthome.jolt` → the certificate from
step 3 → name: `jolt AppStore` → download `jolt_AppStore.mobileprovision`.

It must be an **App Store** profile, not a development or ad-hoc profile.
The workflow checks this and says so if it is not.

## 5. Create the app in App Store Connect

<https://appstoreconnect.apple.com/apps> → **+** → *Neue App* (New App).

- Platform iOS, name `jolt`, primary language German
- Bundle ID: `de.thesmarthome.jolt` (appears in the list as soon as
  step 2 is done)
- SKU: anything, e.g. `jolt`

Without this entry, App Store Connect rejects the upload.

## 6. API key for the upload

<https://appstoreconnect.apple.com/access/integrations/api> → *Team Keys* →
**+** → name `github-ci`, access **Admin**. Only with Admin may Apple create
certificates for the automatic route; App Manager is enough for plain uploading.

The `.p8` file can be downloaded **only once**. Also note down the
**Key ID** (column in the list) and the **Issuer ID** (top of the page).

The API key replaces your Apple ID password including two-factor — so the run
can upload without anyone typing in a code.

## 7. Secrets in the repository

GitHub → repository → *Settings → Secrets and variables → Actions → New
repository secret*.

**Automatic route — these four are enough:**

| Name | Content |
|---|---|
| `APPLE_TEAM_ID` | the Team ID from step 1 |
| `APPSTORE_KEY_ID` | the Key ID from step 6 |
| `APPSTORE_ISSUER_ID` | the Issuer ID from step 6 |
| `APPSTORE_KEY_P8_BASE64` | the `.p8` from step 6, base64-encoded |

**Manual fallback — these three in addition, and only all together:**

| Name | Content |
|---|---|
| `IOS_CERT_P12_BASE64` | `jolt.p12`, base64-encoded (see below) |
| `IOS_CERT_PASSWORD` | the password from step 3 |
| `IOS_PROFILE_BASE64` | the `.mobileprovision` from step 4, base64-encoded |

If all three are set, the workflow uses them and signs manually; if all are
missing, Apple signs. Two out of three is an error that the first step reports.

Base64 without line breaks (for the `.p8`, and for the fallback also the `.p12`
and `.mobileprovision`):

```bash
base64 -w0 AuthKey_XXXXXXXXXX.p8       # Linux / unRAID
base64 -i AuthKey_XXXXXXXXXX.p8        # macOS
```

Put the output on the clipboard and into the secret field. **Nowhere
else** — not into a chat, not into the repository, not into an
`.env`. Anyone who has `jolt.p12` together with its password, or the `.p8`, can
sign and upload apps in your name. After entering them, delete the files from
`~/jolt-signing` or move them into a password manager; GitHub does not show a
secret again after it has been saved.

## 8. The first run

*Actions → iOS TestFlight → Run workflow*. A run takes about ten
minutes and, because of the Mac runner, counts **ten times** against the
minutes quota of private repositories (see `ios.yml`) — so roughly a
hundred minutes. Don't start it out of curiosity.

The first step checks whether all seven secrets are present and says which
ones are missing. After that: generate project → set up certificate and profile →
archive → upload.

When the run is green, App Store Connect still processes the build for a few
minutes (status in the app under *TestFlight*). Then:

1. In App Store Connect → *Benutzer und Zugriff* (Users and Access), make sure
   you are listed as a user (as the account holder you are).
2. *TestFlight → Interne Tests* (Internal Testing) → create a group → add
   yourself → assign the build. Internal testers need **no** review by Apple.
3. Install the **TestFlight** app on the iPhone, sign in with the same
   Apple ID → `jolt` appears → install it.

Other people in the household: invite them under *Benutzer und Zugriff*, then
add them as in 2. Up to 100 internal testers, no review.

## What happens afterwards

- **New version of the shell:** change goes to `main`, then start the workflow
  again. The build number is run number × 10 + attempt and
  increases on its own.
- **New version of the UI:** nothing. It comes from the server.
- **TestFlight builds expire after 90 days.** Then simply upload again.
- **Certificate and profile expire after a year** — repeat steps 3 and 4 with
  new files and replace the two secrets.

---

## Enabling CarPlay EV Charging

Apple has assigned the entitlement to the account. For the app to be allowed to
use it, it must **additionally** be enabled for the App ID — otherwise the
signed build aborts during export (profile does not match the app's entitlements).
Until then everything builds as before; the CarPlay scene is already included
in the build, CarPlay just does not offer it.

1. [Identifiers](https://developer.apple.com/account/resources/identifiers/list)
   → open the App ID `de.thesmarthome.jolt`.
2. Under Capabilities, tick **CarPlay EV Charging** (it only appears there once
   Apple has approved the application), and save.
3. In the repository under *Settings → Secrets and variables → Actions → Variables*,
   create the variable `CARPLAY_ENTITLEMENT` with the value `true`. All runs of
   `ios-testflight.yml` then include the entitlement. For a single run, the
   **carplay** checkbox when starting it manually also works.
4. Start the workflow. With automatic signing, Apple creates the profile with
   the new entitlement itself during export; with manual signing, the profile
   (step 4) has to be regenerated after enabling it.

To test: connect the iPhone via wireless CarPlay, and jolt appears on the
CarPlay home screen. A recording running on the iPhone fills the list
("Jetzt" (Now), and for a planned trip also "Ladestopps" (Charging stops));
without a running trip it shows "Keine laufende Fahrt" (No trip in progress).

---

## If something goes wrong

| Message | Cause |
|---|---|
| `Es fehlen Repository-Geheimnisse: …` (Repository secrets are missing) | Step 7 incomplete; the name must match exactly. |
| `Von IOS_CERT_P12_BASE64, … sind nur 1 von 3 gesetzt` (only 1 of 3 set) | Manual fallback half set up: set all three secrets or delete all. |
| `No signing certificate … found` / `Communication with Apple failed` in automatic mode | The key's role is not **Admin** (step 6), or Key ID/Issuer ID are swapped. |
| `Your team has no devices from which to generate a provisioning profile` | Occurs during export even though the workflow builds the archive unsigned: then register the iPhone under [Devices](https://developer.apple.com/account/resources/devices/list) (UDID) or switch to the manual fallback (steps 3, 4, 7). |
| `MAC verification failed` on import (manual) | `.p12` created without `-legacy` (step 3). |
| `Das Profil ist für … die App heisst …` (The profile is for … the app is called …) | The profile belongs to a different Bundle ID than `capacitor.config.json`. |
| `Das Profil enthält eine Geräteliste` (The profile contains a device list) | Development/ad-hoc profile instead of App Store (step 4). |
| `No signing certificate "Apple Distribution" found` | The certificate in the profile is not the one from the `.p12`. Create both in the same pass. |
| `Erwartet: zwei Stellen mit 'CODE_SIGN_STYLE = Automatic;'` (Expected: two places with …) | Capacitor changed its template; adjust `tools/ios_signatur.sh`. |
| `Provisioning profile … doesn't include the com.apple.developer.carplay-charging entitlement` | The entitlement is in the app but not in the profile: enable "CarPlay EV Charging" for the App ID (see above), or turn off the checkbox / the variable `CARPLAY_ENTITLEMENT`. |
| `Invalid App ID` on `cap add ios` | Hyphen or digit at the start of a segment in `appId`. |
| Upload `Unable to authenticate` | Key ID/Issuer ID swapped, or the key's role is too weak (step 6). |
| Upload `bundle version must be higher` | Build number = run number × 10 + attempt; it only drops if the workflow was deleted and recreated. Then raise the number in `ios-testflight.yml` by a fixed amount. |
| Upload `Invalid large app icon` | `tools/ios-app-icon.png` has an alpha channel or is not 1024 × 1024; `tools/check_ios.py` checks this. |

## Open items

- **Check background location** (step 5 in the concept): the plugin
  `@capacitor-community/background-geolocation` is built in, and `live.js`
  uses it in the app instead of `watchPosition`. Whether iOS really keeps the
  app running with the screen locked can only be shown by a drive. Check it
  once like this: start a recording, lock the phone, drive for ten minutes,
  then look in the trip list to see whether the track is continuous. On first
  launch iOS asks for location; "Beim Verwenden der App" (While Using the App)
  is enough (the blue indicator in the status bar is intended), "Immer"
  (Always) is the more generous choice. An **older app without the plugin**
  keeps working as before via the browser location - it needs a new build
  (TestFlight) to get the plugin.
- **Queue against dead zones** is done (step 6).
- **Push notifications** continue to go through web push in the browser; the app
  has no push permission (that would need APNs and a capability).

Both items can only be built sensibly once the app is on the phone: whether a
background plugin really keeps running under iOS can only be shown by a drive.
