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
