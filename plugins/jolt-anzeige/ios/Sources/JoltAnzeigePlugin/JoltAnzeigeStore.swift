import Foundation

/// Die letzte Anzeige, für alles, was nicht die Live Activity ist - die
/// CarPlay-Liste.
///
/// Das Anzeigemodell entsteht im JavaScript der Oberfläche und kommt über das
/// Plugin hierher. Die CarPlay-Szene läuft im selben Prozess, aber ohne die
/// Oberfläche zu kennen: Sie liest von hier und lässt sich benachrichtigen,
/// wenn etwas Neues kommt.
///
/// Der Stand wird abgelegt, damit eine CarPlay-Szene, die vor der
/// Oberfläche startet (iPhone gesperrt, Auto startet die App), nicht leer
/// beginnt. Wie alt er ist, steht im Modell (`stand`); die Anzeige entscheidet,
/// ab wann sie ihn nicht mehr für voll nimmt.
public final class JoltAnzeigeStore {
    public static let shared = JoltAnzeigeStore()

    private let schluessel = "jolt.anzeige.letzte"
    private let sperre = NSLock()
    private var beobachter: [UUID: (JoltAnzeige?) -> Void] = [:]
    private var letzte: JoltAnzeige?

    // MARK: Aktionen aus CarPlay (Aufzeichnung starten und beenden)
    //
    // Die Oberfläche (JavaScript) kann die Aufzeichnung starten, CarPlay nicht.
    // CarPlay schickt deshalb eine Anfrage an das Plugin, das sie an die
    // Oberfläche weitergibt, und bekommt von dort das Ergebnis zurück.
    //
    // **Es geht nur, wenn die Oberfläche läuft.** Startet das Auto die App
    // allein (iPhone gesperrt, nichts geöffnet), gibt es keine Oberfläche: Das
    // Plugin ist nicht geladen, `brueckeAktiv` bleibt falsch, und CarPlay sagt
    // ehrlich, dass jolt auf dem iPhone einmal geöffnet werden muss.
    public static let aktionName = Notification.Name("JoltCarPlayAktion")

    public private(set) var brueckeAktiv = false
    /// Das Fahrzeug, mit dem die Aufzeichnung startet (zuletzt benutzt).
    public private(set) var fahrzeugName: String?
    private var ergebnisBeobachter: [UUID: (String, Bool, String) -> Void] = [:]

    private init() {
        if let daten = UserDefaults.standard.data(forKey: schluessel),
           let modell = try? JSONDecoder().decode(JoltAnzeige.self, from: daten) {
            letzte = modell
        }
    }

    public var aktuell: JoltAnzeige? {
        sperre.lock()
        defer { sperre.unlock() }
        return letzte
    }

    /// `nil`: Die Fahrt ist zu Ende, es gibt nichts mehr zu zeigen.
    public func setzen(_ modell: JoltAnzeige?) {
        sperre.lock()
        letzte = modell
        let alle = Array(beobachter.values)
        sperre.unlock()

        // Die Kachelbilder bleiben aussen vor: Sie sind knapp 200 KB gross und
        // kommen alle paar Sekunden neu. Abgelegt wird der Stand nur für den
        // Kaltstart, und der gilt nach drei Minuten ohnehin nicht mehr -
        // bis die Oberfläche das nächste Mal meldet, zeichnet Swift selbst.
        var zuSichern = modell
        zuSichern?.kachelBilder = nil
        if let sicher = zuSichern, let daten = try? JSONEncoder().encode(sicher) {
            UserDefaults.standard.set(daten, forKey: schluessel)
        } else {
            UserDefaults.standard.removeObject(forKey: schluessel)
        }
        // Die Vorlagen von CarPlay dürfen nur im Hauptthread angefasst werden.
        DispatchQueue.main.async {
            for beiAenderung in alle { beiAenderung(modell) }
        }
    }

    public func brueckeMelden(_ aktiv: Bool) {
        sperre.lock()
        brueckeAktiv = aktiv
        sperre.unlock()
    }

    public func fahrzeugSetzen(_ name: String?) {
        sperre.lock()
        let leer = (name ?? "").trimmingCharacters(in: .whitespaces).isEmpty
        fahrzeugName = leer ? nil : name
        let alle = Array(beobachter.values)
        let modell = letzte
        sperre.unlock()
        // Die Liste zeigt das Fahrzeug am Start-Knopf: neu aufbauen.
        DispatchQueue.main.async {
            for beiAenderung in alle { beiAenderung(modell) }
        }
    }

    /// Bittet die Oberfläche um eine Aktion ("starten" oder "beenden").
    /// Gibt falsch zurück, wenn es keine Oberfläche gibt, die sie ausführen kann.
    public func aktionAnfordern(_ aktion: String) -> Bool {
        sperre.lock()
        let moeglich = brueckeAktiv
        sperre.unlock()
        guard moeglich else { return false }
        NotificationCenter.default.post(
            name: JoltAnzeigeStore.aktionName, object: nil, userInfo: ["aktion": aktion])
        return true
    }

    public func ergebnisMelden(aktion: String, ok: Bool, text: String) {
        sperre.lock()
        let alle = Array(ergebnisBeobachter.values)
        sperre.unlock()
        DispatchQueue.main.async {
            for beiErgebnis in alle { beiErgebnis(aktion, ok, text) }
        }
    }

    public func ergebnisBeobachten(_ beiErgebnis: @escaping (String, Bool, String) -> Void) -> UUID {
        let kennung = UUID()
        sperre.lock()
        ergebnisBeobachter[kennung] = beiErgebnis
        sperre.unlock()
        return kennung
    }

    public func ergebnisNichtMehrBeobachten(_ kennung: UUID) {
        sperre.lock()
        ergebnisBeobachter.removeValue(forKey: kennung)
        sperre.unlock()
    }

    public func beobachten(_ beiAenderung: @escaping (JoltAnzeige?) -> Void) -> UUID {
        let kennung = UUID()
        sperre.lock()
        beobachter[kennung] = beiAenderung
        sperre.unlock()
        return kennung
    }

    public func nichtMehrBeobachten(_ kennung: UUID) {
        sperre.lock()
        beobachter.removeValue(forKey: kennung)
        sperre.unlock()
    }
}
