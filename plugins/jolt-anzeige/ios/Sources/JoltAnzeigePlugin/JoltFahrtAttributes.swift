// Diese Datei gibt es zweimal im gebauten Projekt: hier im Plugin und als
// Kopie im Widget-Ziel (tools/ios_widget.sh kopiert sie). Die Live Activity
// verbindet beide über den Namen der Typen und die Codable-Darstellung - die
// Datei ist deshalb die einzige Quelle, und ändert man sie, ändern sich beide
// Seiten gleichzeitig.
//
// Die Felder sind das Anzeigemodell aus frontend/anzeige.js, Schlüssel für
// Schlüssel. Was dort fehlt (kein Plan, kein Ladestopp), ist hier nil.
import Foundation

public struct JoltAnzeige: Codable, Hashable {
    public struct Soc: Codable, Hashable {
        public var prozent: Double
        public var text: String
        public var quelle: String?
    }

    public struct Stopp: Codable, Hashable {
        public var name: String
        public var km: Double
        public var kmText: String
        public var ankunftSoc: Int?
        public var ankunftSocText: String?
        public var geplant: Bool?
    }

    /// Reserve, Ankunft und Rest haben außer dem Text noch eine Zahl; die
    /// Anzeige braucht nur den Text, die Zahl wird beim Lesen übergangen.
    public struct Zeile: Codable, Hashable {
        public var text: String
    }

    /// Verbrauch im Schnitt über eine, fünf, dreissig und sechzig Minuten.
    /// `text` ist "–", wenn die Spur das Fenster nicht abdeckt.
    public struct Fenster: Codable, Hashable {
        public var min: Int
        public var kwh100: Double?
        public var kw: Double?
        public var text: String
        public var kwText: String
    }

    /// Anteil der entnommenen Energie, der zurückgespeist wurde.
    public struct Rekup: Codable, Hashable {
        public var prozent: Int
        public var minuten: Int
    }

    public struct Verlauf: Codable, Hashable {
        public var fenster: [Fenster]
        /// Die letzten dreissig Minuten in Balken zu fünf Minuten, ältester
        /// zuerst; nil ist eine Lücke (Stand), nicht null Verbrauch.
        public var balken: [Double?]?
        public var rekup: Rekup?
    }

    /// Was das Auto zieht, ohne zu fahren, und was es sonst noch hergibt.
    public struct Neben: Codable, Hashable {
        public var kw: Double?
        public var text: String?
        public var quelle: String?
        public var heizungKw: Double?
        public var heizungText: String?
        public var klimaKw: Double?
        public var klimaText: String?
        public var batterieC: Int?
        public var batterieText: String?
    }

    /// Ein Ladestopp der Fahrt, wie die CarPlay-Liste ihn zeigt. Die Live
    /// Activity bekommt ihn nicht: Sie darf höchstens 4 KB tragen.
    public struct Listenstopp: Codable, Hashable {
        public var name: String
        public var km: Double
        public var kmText: String
        public var ankunftSoc: Int?
        public var ankunftSocText: String?
        public var abfahrtSocText: String?
        public var ladezeitMin: Int?
        public var ladezeitText: String?
        public var betreiber: String?
        public var leistungKw: Int?
    }

    /// Die Darstellung der CarPlay-Kacheln: "klassisch" (Swift zeichnet), "a"
    /// oder "b" (die Oberfläche liefert die Bilder). Fehlt das Feld, gilt
    /// "klassisch".
    public var stil: String?
    /// Je Kachelplatz ein PNG in Base64, von der Oberfläche gezeichnet
    /// (frontend/kacheln.js): soc, ankunft, reserve, verbrauch, neben, rekup,
    /// stopps. Nur für CarPlay - die Live Activity darf höchstens 4 KB tragen
    /// und bekommt sie nicht.
    public var kachelBilder: [String: String]?

    public var kurz: String
    public var verlauf: Verlauf?
    public var neben: Neben?
    public var stoppListe: [Listenstopp]?
    public var soc: Soc?
    public var stopp: Stopp?
    public var reserve: Zeile?
    public var ankunft: Zeile?
    public var rest: Zeile?
    /// Millisekunden seit 1970, wie JavaScript sie liefert.
    public var stand: Double
}

#if canImport(ActivityKit)
import ActivityKit

@available(iOS 16.2, *)
public struct JoltFahrtAttributes: ActivityAttributes {
    public typealias ContentState = JoltAnzeige

    public var name: String

    public init(name: String) {
        self.name = name
    }
}
#endif
