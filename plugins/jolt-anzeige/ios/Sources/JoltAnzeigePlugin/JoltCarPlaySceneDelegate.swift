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
///   Reiter "Kacheln" (CPGridTemplate), "Bilder" (CPListImageRowItem) und
///   "Tabelle" (CPInformationTemplate), alle mit denselben höchstens acht Plätzen:
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
    private var bilderListe: CPListTemplate?
    private var tabelle: CPInformationTemplate?
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
        aufnehmen(JoltAnzeigeStore.shared.aktuell)
        // Dieselben Werte in drei Anordnungen, zum Vergleichen im Auto: Kacheln
        // (Raster von iOS), Bildzeilen (dichter) und Tabelle (nur Text).
        let modell = JoltAnzeigeStore.shared.aktuell
        let eintraege = self.eintraege(modell)

        let raster = CPGridTemplate(title: "jolt", gridButtons: gitterKnoepfe(eintraege))
        raster.tabTitle = "Kacheln"
        raster.tabImage = UIImage(systemName: "square.grid.2x2")
        raster.trailingNavigationBarButtons = leiste(modell)
        gitter = raster

        let zeilen = CPListTemplate(title: "jolt", sections: bildZeilen(eintraege))
        zeilen.tabTitle = "Bilder"
        zeilen.tabImage = UIImage(systemName: "photo.on.rectangle")
        zeilen.trailingNavigationBarButtons = leiste(modell)
        bilderListe = zeilen

        let tafel = CPInformationTemplate(
            title: "jolt", layout: .leading, items: tabellenZeilen(eintraege), actions: [])
        tafel.tabTitle = "Tabelle"
        tafel.tabImage = UIImage(systemName: "list.bullet")
        tafel.trailingNavigationBarButtons = leiste(modell)
        tabelle = tafel

        let reiter = CPTabBarTemplate(templates: [raster, zeilen, tafel])
        interfaceController.setRootTemplate(reiter, animated: false, completion: nil)

        beobachterKennung = JoltAnzeigeStore.shared.beobachten { [weak self] modell in
            guard let self = self else { return }
            // Kommt eine frische Anzeige, läuft die Aufzeichnung: Das "wird
            // gestartet" ist erledigt.
            if self.frisch(modell) != nil { self.laeuft = nil }
            self.aufnehmen(modell)
            self.anzeigen(modell)
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
        bilderListe = nil
        tabelle = nil
        schnittstelle = nil
    }

    // MARK: - Inhalt

    private func frisch(_ modell: JoltAnzeige?) -> JoltAnzeige? {
        guard let modell = modell else { return nil }
        let alter = Date().timeIntervalSince1970 - modell.stand / 1000
        return alter <= frischeSekunden ? modell : nil
    }

    /// Die Kacheln haben **feste Plätze**; fehlt ein Wert, bleibt der Platz
    /// mit einem Strich stehen. Sonst rückten die übrigen nach, sobald ein
    /// Wert kommt oder geht, und man müsste im Fahren neu suchen.
    ///
    ///   oben:   Ladestand (mit Verlauf) | Ankunft | Reserve (Start/Beenden: Titelleiste)
    ///   unten:  Verbrauch (Balken) | Nebenverbraucher (Verlauf)
    ///           | Rekuperation (Verlauf) | Ladestopps
    ///
    /// Mehr als acht Kacheln nimmt CarPlay nicht.
    private func eintraege(_ rohmodell: JoltAnzeige?) -> [Eintrag] {
        guard let modell = frisch(rohmodell) else {
            return [leereKachel("Keine laufende Fahrt")]
        }
        var ergebnis: [Eintrag] = []
        /// Das Bild, das die Oberfläche für diesen Platz gezeichnet hat (Stil
        /// "a" oder "b"); nil heisst: Swift zeichnet wie bisher.
        func bild(_ platz: String) -> UIImage? { fertigesBild(platz, modell) }

        if let soc = modell.soc {
            ergebnis.append(kachel(
                titel: "Ladestand", wert: soc.text, klein: nil,
                linie: reihe("soc"), bild: bild("soc")))
        } else {
            ergebnis.append(leereKachel("Ladestand", bild: bild("soc")))
        }
        if let ankunft = modell.ankunft {
            ergebnis.append(kachel(titel: "Ankunft", wert: ankunft.text, klein: nil,
                                   bild: bild("ankunft")))
        } else {
            ergebnis.append(leereKachel("Ankunft", bild: bild("ankunft")))
        }
        if let reserve = modell.reserve {
            ergebnis.append(kachel(titel: "Reserve", wert: reserve.text, klein: "in",
                                   bild: bild("reserve")))
        } else {
            ergebnis.append(leereKachel("Reserve", bild: bild("reserve")))
        }

        let fenster = (modell.verlauf?.fenster ?? []).filter { $0.kwh100 != nil }
        if !fenster.isEmpty || modell.verlauf?.balken != nil {
            // Oben der Wert des kürzesten Fensters, darunter die letzten
            // dreissig Minuten in Balken zu fünf Minuten.
            ergebnis.append(kachel(
                titel: "kWh/100 km",
                wert: fenster.first?.text ?? "–",
                klein: fenster.first.map { fensterName($0.min) },
                balken: modell.verlauf?.balken,
                bild: bild("verbrauch"),
                handler: { [weak self] in self?.verbrauchZeigen(fenster) }))
        } else {
            ergebnis.append(leereKachel("kWh/100 km", bild: bild("verbrauch")))
        }

        var teile: [String] = []
        if let neben = modell.neben {
            if let text = neben.text { teile.append("Neben \(text)") }
            if let heizung = neben.heizungText { teile.append("Heizung \(heizung)") }
            if let klima = neben.klimaText { teile.append("Klima \(klima)") }
            if let akku = neben.batterieText { teile.append("Akku \(akku)") }
        }
        if let neben = modell.neben, !teile.isEmpty {
            ergebnis.append(kachel(
                titel: "Nebenverbraucher",
                wert: neben.text ?? neben.heizungText ?? neben.klimaText
                    ?? neben.batterieText ?? "–",
                klein: nil,
                linie: reihe("neben"),
                bild: bild("neben"),
                handler: { [weak self] in self?.nebenZeigen(teile) }))
        } else {
            ergebnis.append(leereKachel("Nebenverbraucher", bild: bild("neben")))
        }

        if let rekup = modell.verlauf?.rekup {
            ergebnis.append(kachel(
                titel: "Rekuperation",
                wert: "\(rekup.prozent) %",
                klein: nil,
                linie: reihe("rekup"),
                bild: bild("rekup")))
        } else {
            ergebnis.append(leereKachel("Rekuperation", bild: bild("rekup")))
        }

        // Eine Aufzeichnung ohne Plan hat keine Stopps: Das ist kein Fehler,
        // der Platz bleibt dann leer.
        if let stopps = modell.stoppListe, !stopps.isEmpty {
            ergebnis.append(kachel(
                titel: "Ladestopps", wert: "\(stopps.count)", klein: nil,
                bild: bild("stopps"),
                handler: { [weak self] in self?.stoppsZeigen(stopps) }))
        } else {
            ergebnis.append(leereKachel("Ladestopps", bild: bild("stopps")))
        }
        return Array(ergebnis.prefix(8))
    }

    private func leereKachel(_ titel: String, bild: UIImage? = nil) -> Eintrag {
        kachel(titel: titel, wert: "–", klein: nil, bild: bild)
    }

    /// Das fertige Bild der Oberfläche für einen Kachelplatz - oder nil, wenn
    /// keines mitkam (Stil "klassisch", ältere Oberfläche, Bild nicht lesbar).
    /// In dem Fall zeichnet `kachelBild` wie bisher.
    private func fertigesBild(_ platz: String, _ modell: JoltAnzeige) -> UIImage? {
        guard modell.stil != nil, modell.stil != "klassisch",
              let text = modell.kachelBilder?[platz],
              let daten = Data(base64Encoded: text),
              let bild = UIImage(data: daten, scale: 2) else { return nil }
        return begrenzt(bild)
    }

    /// CarPlay zeigt Kachelbilder höchstens in einer bestimmten Grösse; ein
    /// grösseres würde das System verkleinern oder abschneiden. Hier wird es
    /// vorher passend verkleinert, mit dem Seitenverhältnis.
    ///
    /// Die Grenze nennt iOS erst ab 26 (`maximumGridButtonImageSize`); davor
    /// bleibt das Bild, wie es ist - 120 Punkte, so gross wie das, was
    /// `kachelBild` immer schon gezeichnet hat.
    private func begrenzt(_ bild: UIImage) -> UIImage {
        guard #available(iOS 26.0, *) else { return bild }
        let grenze = CPGridTemplate.maximumGridButtonImageSize
        guard grenze.width > 0, grenze.height > 0,
              bild.size.width > grenze.width || bild.size.height > grenze.height
        else { return bild }
        let faktor = min(grenze.width / bild.size.width, grenze.height / bild.size.height)
        let ziel = CGSize(width: bild.size.width * faktor, height: bild.size.height * faktor)
        let format = UIGraphicsImageRendererFormat()
        format.scale = bild.scale
        return UIGraphicsImageRenderer(size: ziel, format: format).image { _ in
            bild.draw(in: CGRect(origin: .zero, size: ziel))
        }
    }

    // MARK: - Verläufe

    /// Die letzten dreissig Minuten je Wert. Sie leben so lange wie die App
    /// (nicht nur die Szene): Wer CarPlay zwischendurch trennt, soll beim
    /// Verbinden nicht bei null anfangen. Eine Lücke von mehr als drei Minuten
    /// beginnt die Reihe neu - das ist eine andere Fahrt.
    private static var reihen: [String: [(zeit: Date, wert: Double)]] = [:]

    private func aufnehmen(_ rohmodell: JoltAnzeige?) {
        guard let modell = frisch(rohmodell) else { return }
        merken("soc", modell.soc?.prozent)
        merken("neben", modell.neben?.kw)
        merken("rekup", modell.verlauf?.rekup.map { Double($0.prozent) })
    }

    private func merken(_ name: String, _ wert: Double?) {
        guard let wert = wert else { return }
        let jetzt = Date()
        var liste = JoltCarPlaySceneDelegate.reihen[name] ?? []
        if let letzte = liste.last, jetzt.timeIntervalSince(letzte.zeit) > frischeSekunden {
            liste = []
        }
        // Höchstens ein Punkt je 20 Sekunden: Die Anzeige kommt öfter.
        if let letzte = liste.last, jetzt.timeIntervalSince(letzte.zeit) < 20 {
            liste[liste.count - 1] = (jetzt, wert)
        } else {
            liste.append((jetzt, wert))
        }
        liste.removeAll { jetzt.timeIntervalSince($0.zeit) > 1800 }
        JoltCarPlaySceneDelegate.reihen[name] = liste
    }

    private func reihe(_ name: String) -> [Double]? {
        guard let liste = JoltCarPlaySceneDelegate.reihen[name], liste.count >= 2 else { return nil }
        return liste.map { $0.wert }
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
                        linie: [Double]? = nil,
                        bild: UIImage? = nil,
                        handler: (() -> Void)? = nil) -> Eintrag {
        Eintrag(
            titel: titel, wert: wert,
            bild: bild ?? kachelBild(wert: wert, klein: klein, balken: balken, linie: linie),
            handler: handler)
    }

    /// Ein Platz der Anzeige, unabhängig von der Vorlage, die ihn zeigt.
    private struct Eintrag {
        let titel: String
        let wert: String
        let bild: UIImage
        let handler: (() -> Void)?
    }

    private func gitterKnoepfe(_ eintraege: [Eintrag]) -> [CPGridButton] {
        eintraege.prefix(8).map { e in
            CPGridButton(
                titleVariants: [e.titel], image: e.bild,
                handler: e.handler.map { aufruf -> ((CPGridButton) -> Void) in { _ in aufruf() } })
        }
    }

    /// Bildzeilen zu je vier Bildern; die Beschriftung unter dem Bild gibt es
    /// erst ab iOS 17.4, davor steht nur das Bild.
    private func bildZeilen(_ eintraege: [Eintrag]) -> [CPListSection] {
        let proZeile = 4
        var zeilen: [CPListTemplateItem] = []
        var start = 0
        while start < eintraege.count {
            let teil = Array(eintraege[start..<min(start + proZeile, eintraege.count)])
            let bilder = teil.map { $0.bild }
            let zeile: CPListImageRowItem
            if #available(iOS 17.4, *) {
                zeile = CPListImageRowItem(
                    text: "", images: bilder, imageTitles: teil.map { $0.titel })
            } else {
                zeile = CPListImageRowItem(text: teil.map { $0.titel }.joined(separator: " · "),
                                           images: bilder)
            }
            zeile.listImageRowHandler = { _, index, abschluss in
                if index >= 0 && index < teil.count { teil[index].handler?() }
                abschluss()
            }
            zeilen.append(zeile)
            start += proZeile
        }
        return [CPListSection(items: zeilen)]
    }

    private func tabellenZeilen(_ eintraege: [Eintrag]) -> [CPInformationItem] {
        eintraege.map { CPInformationItem(title: $0.titel, detail: $0.wert) }
    }

    /// Eine Kachel als Bild: grosser Wert, darunter eine kleine Zeile oder
    /// ein Balkendiagramm. CarPlay zeichnet für Kacheln nur ein Bild und einen
    /// Titel; alles andere muss ins Bild.
    private func kachelBild(wert: String, klein: String?, balken: [Double?]?,
                            linie: [Double]?) -> UIImage {
        let seite: CGFloat = 120
        let groesse = CGSize(width: seite, height: seite)
        let traits = schnittstelle?.carTraitCollection ?? UITraitCollection.current
        let format = UIGraphicsImageRendererFormat()
        format.scale = traits.displayScale > 0 ? traits.displayScale : 2
        let schrift = UIColor.label.resolvedColor(with: traits)
        let gedaempft = UIColor.secondaryLabel.resolvedColor(with: traits)
        let balkenFarbe = UIColor.systemGreen.resolvedColor(with: traits)
        let flaeche = UIColor.tertiarySystemFill.resolvedColor(with: traits)
        // "mitBalken" heisst: ein Diagramm unter dem Wert (Balken oder Linie).
        let mitBalken = (balken?.contains(where: { $0 != nil }) ?? false) || linie != nil

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

            if let linie = linie, linie.count >= 2 {
                let tief = linie.min() ?? 0
                let hoch = linie.max() ?? 1
                let spanne = max(hoch - tief, 0.0001)
                let unten: CGFloat = seite - 12
                let platz: CGFloat = 46
                let schritt = (seite - 20) / CGFloat(linie.count - 1)
                let pfad = UIBezierPath()
                for (i, eintrag) in linie.enumerated() {
                    // Eine flache Linie liegt in der Mitte, nicht am Boden.
                    let anteil = hoch - tief < 0.0001 ? 0.5 : (eintrag - tief) / spanne
                    let p = CGPoint(x: 10 + CGFloat(i) * schritt,
                                    y: unten - platz * CGFloat(anteil))
                    if i == 0 { pfad.move(to: p) } else { pfad.addLine(to: p) }
                }
                pfad.lineWidth = 3
                pfad.lineJoinStyle = .round
                balkenFarbe.setStroke()
                pfad.stroke()
            } else if mitBalken, let balken = balken {
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

    /// Start und Beenden als kleine Schaltfläche in der Titelleiste: Sie
    /// brauchen keine Kachel, und der Platz gehört den Werten. Beenden fragt
    /// nach, ein Tippen aus Versehen schliesst nichts ab.
    private func leiste(_ rohmodell: JoltAnzeige?) -> [CPBarButton] {
        if let text = laeuft {
            let warten = CPBarButton(title: text, handler: nil)
            warten.isEnabled = false
            return [warten]
        }
        if frisch(rohmodell) != nil {
            return [CPBarButton(title: "Beenden") { [weak self] _ in self?.beendenFragen() }]
        }
        return [CPBarButton(title: "Start") { [weak self] _ in self?.starten() }]
    }

    private func anzeigen(_ modell: JoltAnzeige?) {
        let eintraege = self.eintraege(modell)
        gitter?.updateGridButtons(gitterKnoepfe(eintraege))
        gitter?.trailingNavigationBarButtons = leiste(modell)
        bilderListe?.updateSections(bildZeilen(eintraege))
        bilderListe?.trailingNavigationBarButtons = leiste(modell)
        tabelle?.items = tabellenZeilen(eintraege)
        tabelle?.trailingNavigationBarButtons = leiste(modell)
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
        anzeigen(JoltAnzeigeStore.shared.aktuell)
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
