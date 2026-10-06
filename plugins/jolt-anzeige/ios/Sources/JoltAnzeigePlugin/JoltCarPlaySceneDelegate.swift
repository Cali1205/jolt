import Foundation
#if canImport(CarPlay)
import CarPlay
import UIKit

/// Die CarPlay-Szene von jolt (Kategorie "EV charging").
///
/// Alles besteht aus Vorlagen von iOS; jolt zeichnet nichts selbst. Gezeigt
/// wird, was das Anzeigemodell der Oberfläche liefert - dasselbe, das auch die
/// Live Activity speist:
///
///   Liste "jolt"
///     Abschnitt "Jetzt":     Ladestand, Verbrauch, Ankunft, Reserve,
///                            Nebenverbraucher, Rekuperation
///     Abschnitt "Ladestopps": die Stopps der Fahrt - Antippen öffnet eine
///                            Informationsseite zum Stopp
///
/// Fehlt etwas, fehlt die Zeile. Ist die Anzeige älter als drei Minuten oder
/// leer, steht dort, dass keine Fahrt läuft - und nichts, was nach einem
/// aktuellen Wert aussieht.
///
/// Diese Klasse wird von iOS über ihren Namen aus der Info.plist geladen
/// (`UISceneDelegateClassName`); `tools/ios_carplay.sh` trägt ihn dort ein.
@objc(JoltCarPlaySceneDelegate)
public class JoltCarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    private var schnittstelle: CPInterfaceController?
    private var liste: CPListTemplate?
    private var beobachterKennung: UUID?

    /// Ab wann eine Anzeige als veraltet gilt.
    private let frischeSekunden: TimeInterval = 180
    /// Mehr Zeilen je Abschnitt nimmt die Vorlage im Auto nicht.
    private let zeilenMax = 10

    public func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didConnect interfaceController: CPInterfaceController
    ) {
        schnittstelle = interfaceController
        let vorlage = CPListTemplate(
            title: "jolt",
            sections: abschnitte(JoltAnzeigeStore.shared.aktuell))
        liste = vorlage
        interfaceController.setRootTemplate(vorlage, animated: false, completion: nil)

        beobachterKennung = JoltAnzeigeStore.shared.beobachten { [weak self] modell in
            guard let self = self else { return }
            self.liste?.updateSections(self.abschnitte(modell))
        }
    }

    public func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didDisconnectInterfaceController interfaceController: CPInterfaceController
    ) {
        if let kennung = beobachterKennung {
            JoltAnzeigeStore.shared.nichtMehrBeobachten(kennung)
        }
        beobachterKennung = nil
        liste = nil
        schnittstelle = nil
    }

    // MARK: - Inhalt

    private func frisch(_ modell: JoltAnzeige?) -> JoltAnzeige? {
        guard let modell = modell else { return nil }
        let alter = Date().timeIntervalSince1970 - modell.stand / 1000
        return alter <= frischeSekunden ? modell : nil
    }

    private func abschnitte(_ rohmodell: JoltAnzeige?) -> [CPListSection] {
        guard let modell = frisch(rohmodell) else {
            let leer = CPListItem(
                text: "Keine laufende Fahrt",
                detailText: "Starte die Aufzeichnung in jolt")
            return [CPListSection(items: [leer])]
        }

        var jetzt: [CPListItem] = []
        if let soc = modell.soc {
            jetzt.append(CPListItem(
                text: "Ladestand \(soc.text)",
                detailText: quelleText(soc.quelle)))
        }
        if let verlauf = modell.verlauf {
            let teile = verlauf.fenster
                .filter { $0.kwh100 != nil }
                .map { "\(fensterName($0.min)) \($0.text)" }
            if !teile.isEmpty {
                jetzt.append(CPListItem(
                    text: "Verbrauch kWh/100 km",
                    detailText: teile.joined(separator: " · ")))
            }
            if let rekup = verlauf.rekup {
                jetzt.append(CPListItem(
                    text: "Rekuperation \(rekup.prozent) %",
                    detailText: "über \(rekup.minuten) min"))
            }
        }
        if let ankunft = modell.ankunft {
            jetzt.append(CPListItem(text: "Ankunft", detailText: ankunft.text))
        }
        if let reserve = modell.reserve {
            jetzt.append(CPListItem(text: "Reserve", detailText: "in \(reserve.text)"))
        }
        if let neben = modell.neben {
            var teile: [String] = []
            if let text = neben.text { teile.append("Neben \(text)") }
            if let heizung = neben.heizungText { teile.append("Heizung \(heizung)") }
            if let klima = neben.klimaText { teile.append("Klima \(klima)") }
            if let akku = neben.batterieText { teile.append("Akku \(akku)") }
            if !teile.isEmpty {
                jetzt.append(CPListItem(
                    text: "Nebenverbraucher",
                    detailText: teile.joined(separator: " · ")))
            }
        }

        var ergebnis: [CPListSection] = []
        if !jetzt.isEmpty {
            ergebnis.append(CPListSection(
                items: Array(jetzt.prefix(zeilenMax)),
                header: "Jetzt",
                sectionIndexTitle: nil))
        }

        if let stopps = modell.stoppListe, !stopps.isEmpty {
            let zeilen: [CPListItem] = stopps.prefix(zeilenMax).map { stopp in
                let item = CPListItem(text: stopp.name, detailText: stoppZeile(stopp))
                item.handler = { [weak self] _, abschluss in
                    self?.stoppZeigen(stopp)
                    abschluss()
                }
                return item
            }
            ergebnis.append(CPListSection(
                items: zeilen, header: "Ladestopps", sectionIndexTitle: nil))
        }
        // Eine Aufzeichnung ohne Plan hat keine Stopps: Das ist kein Fehler,
        // der Abschnitt fehlt dann einfach.

        return ergebnis.isEmpty
            ? [CPListSection(items: [CPListItem(text: "Keine Werte", detailText: nil)])]
            : ergebnis
    }

    private func stoppZeile(_ stopp: JoltAnzeige.Listenstopp) -> String {
        var teile = ["in \(stopp.kmText)"]
        if let soc = stopp.ankunftSocText { teile.append("\(soc) bei Ankunft") }
        if let zeit = stopp.ladezeitText { teile.append(zeit) }
        return teile.joined(separator: " · ")
    }

    private func stoppZeigen(_ stopp: JoltAnzeige.Listenstopp) {
        var punkte: [CPInformationItem] = [
            CPInformationItem(title: "Entfernung", detail: stopp.kmText)
        ]
        if let soc = stopp.ankunftSocText {
            punkte.append(CPInformationItem(title: "Ladestand bei Ankunft", detail: soc))
        }
        if let zeit = stopp.ladezeitText {
            punkte.append(CPInformationItem(title: "Ladezeit", detail: zeit))
        }
        if let soc = stopp.abfahrtSocText {
            punkte.append(CPInformationItem(title: "Ladestand bei Abfahrt", detail: soc))
        }
        if let betreiber = stopp.betreiber {
            punkte.append(CPInformationItem(title: "Betreiber", detail: betreiber))
        }
        if let kw = stopp.leistungKw {
            punkte.append(CPInformationItem(title: "Leistung", detail: "bis \(kw) kW"))
        }
        let seite = CPInformationTemplate(
            title: stopp.name, layout: .leading, items: punkte, actions: [])
        schnittstelle?.pushTemplate(seite, animated: true, completion: nil)
    }

    private func quelleText(_ quelle: String?) -> String {
        switch quelle {
        case "gemessen": return "gemessen"
        case "zuletzt": return "zuletzt gemessen"
        case "gerechnet": return "gerechnet"
        default: return quelle ?? ""
        }
    }

    private func fensterName(_ minuten: Int) -> String {
        minuten >= 60 ? "\(minuten / 60) h" : "\(minuten) min"
    }
}
#endif
