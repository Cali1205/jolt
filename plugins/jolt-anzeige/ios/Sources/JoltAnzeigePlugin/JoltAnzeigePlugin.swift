import Foundation
import Capacitor
#if canImport(ActivityKit)
import ActivityKit
#endif

/// Bringt das Anzeigemodell der Oberfläche in eine Live Activity.
///
/// Warum eine Live Activity: Sie hat einen Anfang und ein Ende - die Fahrt -,
/// wird von der App aktualisiert, die ohnehin läuft (Hintergrund-Standort,
/// Dongle), und erscheint auf dem Sperrbildschirm und im CarPlay-Dashboard,
/// ohne dass Apple die App als CarPlay-App freigeben muss.
///
/// **Beginnen darf die App sie nur im Vordergrund.** Die Aufzeichnung startet
/// der Fahrer mit einem Fingertipp, da stimmt das. Gelingt es einmal nicht
/// (etwa nach dem Neuladen im Hintergrund), meldet `aktualisieren` einen
/// Fehler, und der nächste Herzschlag der Oberfläche versucht es wieder.
@objc(JoltAnzeigePlugin)
public class JoltAnzeigePlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "JoltAnzeigePlugin"
    public let jsName = "JoltAnzeige"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "verfuegbar", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "aktualisieren", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "beenden", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "bereit", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "aktionErgebnis", returnType: CAPPluginReturnPromise)
    ]

    /// Das Plugin ist geladen, also läuft die Oberfläche: CarPlay darf ihr
    /// Aufträge geben (Aufzeichnung starten und beenden). Die Anfragen aus der
    /// Liste kommen als Benachrichtigung und gehen als Ereignis "carplayAktion"
    /// an das JavaScript (fahrten.js).
    public override func load() {
        JoltAnzeigeStore.shared.brueckeMelden(true)
        NotificationCenter.default.addObserver(
            forName: JoltAnzeigeStore.aktionName, object: nil, queue: .main
        ) { [weak self] meldung in
            guard let aktion = meldung.userInfo?["aktion"] as? String else { return }
            self?.notifyListeners("carplayAktion", data: ["aktion": aktion])
        }
    }

    /// Wie lange eine Anzeige ohne neue Meldung als frisch gilt. Die
    /// Oberfläche meldet mindestens jede Minute; danach zeigt die Activity
    /// "veraltet", statt einen alten Ladestand als aktuellen auszugeben.
    private let frischeSekunden: TimeInterval = 180

    @objc func verfuegbar(_ call: CAPPluginCall) {
        #if canImport(ActivityKit)
        if #available(iOS 16.2, *) {
            call.resolve(["verfuegbar": ActivityAuthorizationInfo().areActivitiesEnabled])
            return
        }
        #endif
        call.resolve(["verfuegbar": false])
    }

    /// `json`: das Anzeigemodell als Zeichenkette. Als Zeichenkette statt als
    /// Objekt, damit die Brücke nichts umformt - gelesen wird es hier genau
    /// einmal, mit denselben Schlüsseln wie in anzeige.js.
    @objc func aktualisieren(_ call: CAPPluginCall) {
        guard let text = call.getString("json"), let daten = text.data(using: .utf8) else {
            call.reject("json fehlt")
            return
        }
        let zustand: JoltAnzeige
        do {
            zustand = try JSONDecoder().decode(JoltAnzeige.self, from: daten)
        } catch {
            call.reject("Anzeigemodell nicht lesbar: \(error.localizedDescription)")
            return
        }
        // Erst für CarPlay, mit der Liste der Stopps - unabhängig davon, ob die
        // Live Activity gelingt (sie darf nur im Vordergrund beginnen).
        JoltAnzeigeStore.shared.setzen(zustand)

        #if canImport(ActivityKit)
        guard #available(iOS 16.2, *) else {
            call.reject("Live Activities brauchen iOS 16.2")
            return
        }
        // Die Live Activity trägt höchstens 4 KB; die Stoppliste gehört nur in
        // die CarPlay-Vorlage.
        var fuerActivity = zustand
        fuerActivity.stoppListe = nil
        Task {
            do {
                try await self.setzen(fuerActivity)
                call.resolve()
            } catch {
                call.reject("Live Activity: \(error.localizedDescription)")
            }
        }
        #else
        call.reject("ActivityKit fehlt")
        #endif
    }

    /// Die Oberfläche meldet, mit welchem Fahrzeug eine Aufzeichnung starten würde.
    @objc func bereit(_ call: CAPPluginCall) {
        JoltAnzeigeStore.shared.fahrzeugSetzen(call.getString("fahrzeug"))
        call.resolve()
    }

    /// Das Ergebnis einer Aktion aus CarPlay: ok, und bei einem Fehler der Grund.
    @objc func aktionErgebnis(_ call: CAPPluginCall) {
        JoltAnzeigeStore.shared.ergebnisMelden(
            aktion: call.getString("aktion") ?? "",
            ok: call.getBool("ok") ?? false,
            text: call.getString("text") ?? "")
        call.resolve()
    }

    @objc func beenden(_ call: CAPPluginCall) {
        JoltAnzeigeStore.shared.setzen(nil)
        #if canImport(ActivityKit)
        guard #available(iOS 16.2, *) else {
            call.resolve()
            return
        }
        Task {
            for aktiv in Activity<JoltFahrtAttributes>.activities {
                await aktiv.end(nil, dismissalPolicy: .immediate)
            }
            call.resolve()
        }
        #else
        call.resolve()
        #endif
    }

    #if canImport(ActivityKit)
    @available(iOS 16.2, *)
    private func setzen(_ zustand: JoltAnzeige) async throws {
        let inhalt = ActivityContent(
            state: zustand,
            staleDate: Date().addingTimeInterval(frischeSekunden))

        // Eine laufende Anzeige wird fortgeschrieben; mehr als eine gibt es
        // nicht, auch nicht nach einem Neuladen der Oberfläche.
        let vorhanden = Activity<JoltFahrtAttributes>.activities
        if let erste = vorhanden.first {
            await erste.update(inhalt)
            for weitere in vorhanden.dropFirst() {
                await weitere.end(nil, dismissalPolicy: .immediate)
            }
            return
        }

        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            throw NSError(domain: "jolt", code: 1, userInfo: [
                NSLocalizedDescriptionKey:
                    "Live-Aktivitäten sind in den iPhone-Einstellungen ausgeschaltet."])
        }
        _ = try Activity.request(
            attributes: JoltFahrtAttributes(name: "jolt"),
            content: inhalt,
            pushType: nil)
    }
    #endif
}
