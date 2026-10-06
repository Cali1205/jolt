// Die Live Activity von jolt: Ladestand, nächster Ladestopp, Ankunft.
//
// Sie erscheint auf dem Sperrbildschirm, in der Dynamic Island und - ab iOS 26 -
// im CarPlay-Dashboard. CarPlay zeigt die Familie `.small` (dieselbe wie die
// Apple Watch); fehlt sie, fällt es auf die kompakten Ansichten der Dynamic
// Island zurück. Gezeigt wird nur, was im Modell steht - fehlt ein Feld,
// fehlt die Zeile, und es wird nichts ersetzt.
import ActivityKit
import SwiftUI
import WidgetKit

@main
struct JoltWidgetBundle: WidgetBundle {
    var body: some Widget {
        JoltFahrtAnzeige()
    }
}

// Die Erweiterung gilt ab iOS 18: Erst dort gibt es die kleine Familie, die
// CarPlay zeigt. Die App selbst behaelt ihre Mindestfassung; auf aelteren
// iPhones fehlt nur die Live Activity.
struct JoltFahrtAnzeige: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: JoltFahrtAttributes.self) { context in
            FahrtAnsicht(zustand: context.state, veraltet: context.isStale)
                .padding(12)
                .activityBackgroundTint(Color.black.opacity(0.75))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Text(context.state.soc?.text ?? "–")
                        .font(.title2.bold())
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(context.state.ankunft?.text ?? "")
                        .font(.headline)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Text(context.state.kurz)
                        .font(.subheadline)
                }
            } compactLeading: {
                Text(context.state.soc?.text ?? "–")
            } compactTrailing: {
                Text(context.state.stopp?.kmText ?? context.state.reserve?.text ?? "")
            } minimal: {
                Image(systemName: "bolt.fill")
            }
        }
        .supplementalActivityFamilies([.small])
    }
}

struct FahrtAnsicht: View {
    @Environment(\.activityFamily) private var familie

    let zustand: JoltAnzeige
    let veraltet: Bool

    var body: some View {
        Group {
            switch familie {
            case .small:
                klein
            default:
                gross
            }
        }
        .foregroundStyle(.white)
        .opacity(veraltet ? 0.55 : 1)
    }

    /// CarPlay und Watch: eine große Zahl und eine Zeile.
    private var klein: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(zustand.soc?.text ?? "–")
                .font(.system(size: 34, weight: .bold, design: .rounded))
            Text(zeile)
                .font(.caption)
                .lineLimit(2)
            if let fuenf = zustand.verlauf?.fenster.first(where: { $0.min == 5 }),
               fuenf.kwh100 != nil {
                Text("Ø 5 min \(fuenf.text) kWh/100")
                    .font(.caption2)
                    .opacity(0.85)
            }
        }
    }

    private var gross: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(zustand.soc?.text ?? "–")
                    .font(.system(size: 40, weight: .bold, design: .rounded))
                if let quelle = zustand.soc?.quelle, quelle != "gemessen" {
                    Text(quelle)
                        .font(.caption)
                        .opacity(0.8)
                }
                Spacer()
                if let ankunft = zustand.ankunft {
                    Text("Ankunft \(ankunft.text)")
                        .font(.headline)
                }
            }
            if let stopp = zustand.stopp {
                Text("Stopp in \(stopp.kmText): \(stopp.name)"
                     + (stopp.ankunftSocText.map { " (\($0))" } ?? ""))
                    .font(.subheadline)
            } else if let reserve = zustand.reserve {
                Text("Reserve in \(reserve.text)")
                    .font(.subheadline)
            }
            if let verlauf = zustand.verlauf {
                verbrauch(verlauf)
            }
            if let zeile = nebenZeile {
                Text(zeile)
                    .font(.caption)
                    .opacity(0.9)
            }
            if let rest = zustand.rest {
                Text("Noch \(rest.text)")
                    .font(.caption)
                    .opacity(0.8)
            }
            if veraltet {
                Text("Keine aktuellen Werte")
                    .font(.caption.bold())
            }
        }
    }

    /// Verbrauch in kWh/100 km im Schnitt der letzten 1, 5, 30 und 60 Minuten,
    /// daneben die letzten dreissig Minuten als Balken. Was das Auto selbst
    /// anzeigt (seit Start), steht hier absichtlich nicht.
    private func verbrauch(_ verlauf: JoltAnzeige.Verlauf) -> some View {
        HStack(alignment: .bottom, spacing: 10) {
            ForEach(verlauf.fenster, id: \.min) { fenster in
                VStack(spacing: 0) {
                    Text(fenster.text)
                        .font(.system(size: 17, weight: .semibold, design: .rounded))
                    Text(fenster.min >= 60 ? "\(fenster.min / 60) h" : "\(fenster.min) min")
                        .font(.system(size: 10))
                        .opacity(0.75)
                }
            }
            Text("kWh/100")
                .font(.system(size: 10))
                .opacity(0.75)
            Spacer(minLength: 4)
            if let balken = verlauf.balken {
                balkenDiagramm(balken)
            }
        }
    }

    /// Sechs Balken, der rechte ist der neueste. Eine Lücke bleibt eine Lücke.
    private func balkenDiagramm(_ werte: [Double?]) -> some View {
        let hoechster = max(werte.compactMap { $0 }.max() ?? 1, 1)
        return HStack(alignment: .bottom, spacing: 3) {
            ForEach(werte.indices, id: \.self) { i in
                RoundedRectangle(cornerRadius: 2)
                    .frame(width: 6, height: werte[i].map { max(4, CGFloat($0 / hoechster) * 30) } ?? 2)
                    .opacity(werte[i] == nil ? 0.3 : 1)
            }
        }
        .frame(height: 30, alignment: .bottom)
    }

    /// Nebenverbraucher, Heizung, Klima, Rekuperation, Batterie - was das Auto
    /// nicht selbst zeigt.
    private var nebenZeile: String? {
        var teile: [String] = []
        if let neben = zustand.neben {
            if let text = neben.text {
                teile.append("Neben \(text)" + (neben.quelle == "geschaetzt" ? "*" : ""))
            }
            if let heizung = neben.heizungText { teile.append("Heizung \(heizung)") }
            if let klima = neben.klimaText { teile.append("Klima \(klima)") }
            if let batterie = neben.batterieText { teile.append("Akku \(batterie)") }
        }
        if let rekup = zustand.verlauf?.rekup {
            teile.append("Rekup \(rekup.prozent) %")
        }
        return teile.isEmpty ? nil : teile.joined(separator: " · ")
    }

    /// Die eine Zeile für die kleine Anzeige.
    private var zeile: String {
        if veraltet { return "Keine aktuellen Werte" }
        if let stopp = zustand.stopp {
            return "Stopp in \(stopp.kmText)"
                + (stopp.ankunftSocText.map { " (\($0))" } ?? "")
        }
        if let reserve = zustand.reserve { return "Reserve in \(reserve.text)" }
        if let ankunft = zustand.ankunft { return ankunft.text }
        return "Fahrt läuft"
    }
}
