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
    private var ergebnisKennung: UUID?
    /// "wird gestartet …" bis das Ergebnis da ist.
    private var laeuft: String?
    private var laeuftSeit: Date?

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
            // Kommt eine frische Anzeige, läuft die Aufzeichnung: Das "wird
            // gestartet" ist erledigt.
            if self.frisch(modell) != nil { self.laeuft = nil }
            self.liste?.updateSections(self.abschnitte(modell))
        }
        ergebnisKennung = JoltAnzeigeStore.shared.ergebnisBeobachten { [weak self] aktion, ok, text in
            self?.ergebnis(aktion: aktion, ok: ok, text: text)
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
        if let kennung = ergebnisKennung {
            JoltAnzeigeStore.shared.ergebnisNichtMehrBeobachten(kennung)
        }
        ergebnisKennung = nil
        laeuft = nil
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
            return [CPListSection(
                items: [startZeile()], header: "Keine laufende Fahrt", sectionIndexTitle: nil)]
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
        // Beenden steht zuoberst im eigenen Abschnitt: Wer fertig ist, sucht es
        // nicht unter den Werten.
        ergebnis.append(CPListSection(
            items: [beendenZeile()], header: "Fahrt", sectionIndexTitle: nil))
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

    // MARK: - Aufzeichnung starten und beenden

    private func startZeile() -> CPListItem {
        let fahrzeug = JoltAnzeigeStore.shared.fahrzeugName
        let zeile = CPListItem(
            text: "Aufzeichnung starten",
            detailText: laeuft ?? (fahrzeug.map { "Fahrzeug: \($0)" }
                                   ?? "mit dem zuletzt benutzten Fahrzeug"))
        zeile.handler = { [weak self] _, abschluss in
            self?.starten()
            abschluss()
        }
        return zeile
    }

    private func beendenZeile() -> CPListItem {
        let zeile = CPListItem(
            text: "Aufzeichnung beenden",
            detailText: laeuft ?? "Fahrt abschliessen und speichern")
        zeile.handler = { [weak self] _, abschluss in
            self?.beendenFragen()
            abschluss()
        }
        return zeile
    }

    private func starten() {
        guard laeuft == nil else { return }
        guard JoltAnzeigeStore.shared.aktionAnfordern("starten") else {
            hinweis("jolt läuft auf dem iPhone nicht. Öffne jolt einmal auf dem iPhone, dann geht es von hier.")
            return
        }
        laeuft = "wird gestartet …"
        aktualisieren()
        ueberwachen()
    }

    /// Antwortet die Oberfläche nicht (die App ist eingefroren, das iPhone
    /// gesperrt), bliebe "wird gestartet …" für immer stehen und sperrte jeden
    /// weiteren Versuch. Nach 25 Sekunden wird das gesagt.
    private func ueberwachen() {
        let marke = Date()
        laeuftSeit = marke
        DispatchQueue.main.asyncAfter(deadline: .now() + 25) { [weak self] in
            guard let self = self, self.laeuftSeit == marke, self.laeuft != nil else { return }
            self.laeuft = nil
            self.aktualisieren()
            self.hinweis("Keine Antwort von jolt. Ist die App auf dem iPhone geöffnet?")
        }
    }

    /// Beenden mit Rückfrage: Ein Tippen aus Versehen darf keine Fahrt abschliessen.
    private func beendenFragen() {
        guard laeuft == nil else { return }
        let ja = CPAlertAction(title: "Beenden", style: .destructive) { [weak self] _ in
            self?.schnittstelle?.dismissTemplate(animated: true, completion: nil)
            guard let self = self else { return }
            if JoltAnzeigeStore.shared.aktionAnfordern("beenden") {
                self.laeuft = "wird beendet …"
                self.aktualisieren()
                self.ueberwachen()
            } else {
                self.hinweis("jolt läuft auf dem iPhone nicht. Die Aufzeichnung lässt sich dort beenden.")
            }
        }
        let nein = CPAlertAction(title: "Abbrechen", style: .cancel) { [weak self] _ in
            self?.schnittstelle?.dismissTemplate(animated: true, completion: nil)
        }
        let frage = CPAlertTemplate(
            titleVariants: ["Aufzeichnung beenden?"], actions: [ja, nein])
        schnittstelle?.presentTemplate(frage, animated: true, completion: nil)
    }

    /// Die Antwort der Oberfläche. Ein Erfolg zeigt sich von selbst (die
    /// Anzeige kommt oder verschwindet); nur ein Fehler braucht eine Meldung.
    private func ergebnis(aktion: String, ok: Bool, text: String) {
        laeuft = nil
        laeuftSeit = nil
        aktualisieren()
        if !ok {
            let grund = text.isEmpty ? "Das hat nicht geklappt." : text
            hinweis((aktion == "starten" ? "Start fehlgeschlagen: " : "Beenden fehlgeschlagen: ") + grund)
        }
    }

    private func aktualisieren() {
        liste?.updateSections(abschnitte(JoltAnzeigeStore.shared.aktuell))
    }

    private func hinweis(_ text: String) {
        let ok = CPAlertAction(title: "OK", style: .default) { [weak self] _ in
            self?.schnittstelle?.dismissTemplate(animated: true, completion: nil)
        }
        let meldung = CPAlertTemplate(titleVariants: [text], actions: [ok])
        schnittstelle?.presentTemplate(meldung, animated: true, completion: nil)
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
