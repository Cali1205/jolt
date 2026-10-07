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
///   Kacheln "jolt" (CPGridTemplate, höchstens acht)
///     Fahrt beenden, Ladestand, Verbrauch (mit Balken), Rekuperation,
///     Ankunft, Reserve, Nebenverbraucher, Ladestopps
///     Ladestopps und Verbrauch öffnen eine Seite; ein Stopp ihre Informationsseite
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
    private var gitter: CPGridTemplate?
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
        let vorlage = CPGridTemplate(
            title: "jolt",
            gridButtons: kacheln(JoltAnzeigeStore.shared.aktuell))
        gitter = vorlage
        interfaceController.setRootTemplate(vorlage, animated: false, completion: nil)

        beobachterKennung = JoltAnzeigeStore.shared.beobachten { [weak self] modell in
            guard let self = self else { return }
            // Kommt eine frische Anzeige, läuft die Aufzeichnung: Das "wird
            // gestartet" ist erledigt.
            if self.frisch(modell) != nil { self.laeuft = nil }
            self.gitter?.updateGridButtons(self.kacheln(modell))
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
        gitter = nil
        schnittstelle = nil
    }

    // MARK: - Inhalt

    private func frisch(_ modell: JoltAnzeige?) -> JoltAnzeige? {
        guard let modell = modell else { return nil }
        let alter = Date().timeIntervalSince1970 - modell.stand / 1000
        return alter <= frischeSekunden ? modell : nil
    }

    /// Die Werte als Kacheln, in der Reihenfolge der früheren Liste: Fahrt
    /// (Beenden), Ladestand, Verbrauch, Rekuperation, Ankunft, Reserve,
    /// Nebenverbraucher, Ladestopps. Mehr als acht Kacheln nimmt CarPlay
    /// nicht; fehlt ein Wert, fehlt die Kachel.
    private func kacheln(_ rohmodell: JoltAnzeige?) -> [CPGridButton] {
        guard let modell = frisch(rohmodell) else {
            return [startKachel()]
        }
        var ergebnis: [CPGridButton] = [beendenKachel()]

        if let soc = modell.soc {
            ergebnis.append(kachel(
                titel: "Ladestand", wert: soc.text, klein: quelleText(soc.quelle)))
        }
        if let verlauf = modell.verlauf {
            let fenster = verlauf.fenster.filter { $0.kwh100 != nil }
            if !fenster.isEmpty || verlauf.balken != nil {
                // Oben der Wert des kürzesten Fensters, darunter die letzten
                // dreissig Minuten in Balken zu fünf Minuten.
                ergebnis.append(kachel(
                    titel: "kWh/100 km",
                    wert: fenster.first?.text ?? "–",
                    klein: fenster.first.map { fensterName($0.min) },
                    balken: verlauf.balken,
                    handler: { [weak self] in self?.verbrauchZeigen(fenster) }))
            }
            if let rekup = verlauf.rekup {
                ergebnis.append(kachel(
                    titel: "Rekuperation",
                    wert: "\(rekup.prozent) %",
                    klein: "über \(rekup.minuten) min"))
            }
        }
        if let ankunft = modell.ankunft {
            ergebnis.append(kachel(titel: "Ankunft", wert: ankunft.text, klein: nil))
        }
        if let reserve = modell.reserve {
            ergebnis.append(kachel(titel: "Reserve", wert: reserve.text, klein: "in"))
        }
        if let neben = modell.neben {
            var teile: [String] = []
            if let text = neben.text { teile.append("Neben \(text)") }
            if let heizung = neben.heizungText { teile.append("Heizung \(heizung)") }
            if let klima = neben.klimaText { teile.append("Klima \(klima)") }
            if let akku = neben.batterieText { teile.append("Akku \(akku)") }
            if !teile.isEmpty {
                ergebnis.append(kachel(
                    titel: "Nebenverbraucher",
                    wert: neben.text ?? neben.heizungText ?? neben.klimaText
                        ?? neben.batterieText ?? "–",
                    klein: nil,
                    handler: { [weak self] in self?.nebenZeigen(teile) }))
            }
        }
        // Eine Aufzeichnung ohne Plan hat keine Stopps: Das ist kein Fehler,
        // die Kachel fehlt dann einfach.
        if let stopps = modell.stoppListe, !stopps.isEmpty {
            ergebnis.append(kachel(
                titel: "Ladestopps", wert: "\(stopps.count)", klein: nil,
                handler: { [weak self] in self?.stoppsZeigen(stopps) }))
        }
        return Array(ergebnis.prefix(8))
    }

    private func stoppsZeigen(_ stopps: [JoltAnzeige.Listenstopp]) {
        let zeilen: [CPListItem] = stopps.prefix(zeilenMax).map { stopp in
            let item = CPListItem(text: stopp.name, detailText: stoppZeile(stopp))
            item.handler = { [weak self] _, abschluss in
                self?.stoppZeigen(stopp)
                abschluss()
            }
            return item
        }
        let seite = CPListTemplate(
            title: "Ladestopps", sections: [CPListSection(items: zeilen)])
        schnittstelle?.pushTemplate(seite, animated: true, completion: nil)
    }

    private func verbrauchZeigen(_ fenster: [JoltAnzeige.Fenster]) {
        let punkte = fenster.map {
            CPInformationItem(title: "letzte \(fensterName($0.min))",
                              detail: "\($0.text) kWh/100 km")
        }
        let seite = CPInformationTemplate(
            title: "Verbrauch", layout: .leading, items: punkte, actions: [])
        schnittstelle?.pushTemplate(seite, animated: true, completion: nil)
    }

    private func nebenZeigen(_ teile: [String]) {
        let punkte = teile.map { CPInformationItem(title: $0, detail: nil) }
        let seite = CPInformationTemplate(
            title: "Nebenverbraucher", layout: .leading, items: punkte, actions: [])
        schnittstelle?.pushTemplate(seite, animated: true, completion: nil)
    }

    // MARK: - Kacheln zeichnen

    private func kachel(titel: String, wert: String, klein: String?,
                        balken: [Double?]? = nil,
                        handler: (() -> Void)? = nil) -> CPGridButton {
        CPGridButton(
            titleVariants: [titel],
            image: kachelBild(wert: wert, klein: klein, balken: balken),
            handler: handler.map { aufruf -> ((CPGridButton) -> Void) in { _ in aufruf() } })
    }

    /// Eine Kachel als Bild: grosser Wert, darunter eine kleine Zeile oder
    /// ein Balkendiagramm. CarPlay zeichnet für Kacheln nur ein Bild und einen
    /// Titel; alles andere muss ins Bild.
    private func kachelBild(wert: String, klein: String?, balken: [Double?]?) -> UIImage {
        let seite: CGFloat = 120
        let groesse = CGSize(width: seite, height: seite)
        let traits = schnittstelle?.carTraitCollection ?? UITraitCollection.current
        let format = UIGraphicsImageRendererFormat()
        format.scale = traits.displayScale > 0 ? traits.displayScale : 2
        let schrift = UIColor.label.resolvedColor(with: traits)
        let gedaempft = UIColor.secondaryLabel.resolvedColor(with: traits)
        let balkenFarbe = UIColor.systemGreen.resolvedColor(with: traits)
        let flaeche = UIColor.tertiarySystemFill.resolvedColor(with: traits)
        let mitBalken = balken?.contains(where: { $0 != nil }) ?? false

        return UIGraphicsImageRenderer(size: groesse, format: format).image { _ in
            flaeche.setFill()
            UIBezierPath(roundedRect: CGRect(origin: .zero, size: groesse),
                         cornerRadius: 14).fill()

            // Wert: so gross wie möglich, ohne den Rand zu berühren.
            var punkt: CGFloat = mitBalken ? 32 : 40
            var attribute: [NSAttributedString.Key: Any] = [:]
            repeat {
                attribute = [.font: UIFont.systemFont(ofSize: punkt, weight: .semibold),
                             .foregroundColor: schrift]
                punkt -= 2
            } while (wert as NSString).size(withAttributes: attribute).width > seite - 12
                && punkt > 12
            let masse = (wert as NSString).size(withAttributes: attribute)
            (wert as NSString).draw(
                at: CGPoint(x: (seite - masse.width) / 2, y: mitBalken ? 6 : 24),
                withAttributes: attribute)

            if mitBalken, let balken = balken {
                let werte = balken.compactMap { $0 }
                let spitze = max(werte.max() ?? 1, 1)
                let unten: CGFloat = seite - 10
                let platz: CGFloat = 50
                let anzahl = CGFloat(balken.count)
                let luecke: CGFloat = 4
                let breite = (seite - 20 - luecke * (anzahl - 1)) / anzahl
                for (i, eintrag) in balken.enumerated() {
                    let x = 10 + CGFloat(i) * (breite + luecke)
                    guard let eintrag = eintrag else {
                        // Lücke: Stand, kein Verbrauch null.
                        gedaempft.setFill()
                        UIBezierPath(rect: CGRect(x: x, y: unten - 2, width: breite, height: 2)).fill()
                        continue
                    }
                    let hoehe = max(3, platz * CGFloat(max(eintrag, 0) / spitze))
                    balkenFarbe.setFill()
                    UIBezierPath(roundedRect: CGRect(x: x, y: unten - hoehe, width: breite, height: hoehe),
                                 cornerRadius: 2).fill()
                }
            } else if let klein = klein {
                let a: [NSAttributedString.Key: Any] = [
                    .font: UIFont.systemFont(ofSize: 15, weight: .regular),
                    .foregroundColor: gedaempft]
                let w = (klein as NSString).size(withAttributes: a).width
                (klein as NSString).draw(
                    at: CGPoint(x: max(6, (seite - w) / 2), y: seite - 38), withAttributes: a)
            }
        }
    }

    // MARK: - Aufzeichnung starten und beenden

    private func startKachel() -> CPGridButton {
        let fahrzeug = JoltAnzeigeStore.shared.fahrzeugName
        return kachel(
            titel: "Aufzeichnung starten",
            wert: laeuft == nil ? "Start" : "…",
            klein: laeuft ?? fahrzeug,
            handler: { [weak self] in self?.starten() })
    }

    private func beendenKachel() -> CPGridButton {
        kachel(
            titel: "Aufzeichnung beenden",
            wert: laeuft == nil ? "Ende" : "…",
            klein: laeuft,
            handler: { [weak self] in self?.beendenFragen() })
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
        gitter?.updateGridButtons(kacheln(JoltAnzeigeStore.shared.aktuell))
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
