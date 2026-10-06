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

        if let modell = modell, let daten = try? JSONEncoder().encode(modell) {
            UserDefaults.standard.set(daten, forKey: schluessel)
        } else {
            UserDefaults.standard.removeObject(forKey: schluessel)
        }
        // Die Vorlagen von CarPlay dürfen nur im Hauptthread angefasst werden.
        DispatchQueue.main.async {
            for beiAenderung in alle { beiAenderung(modell) }
        }
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
