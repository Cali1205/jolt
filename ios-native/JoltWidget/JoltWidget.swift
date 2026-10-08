// jolt's Live Activity: charge level, next charging stop, arrival.
//
// It appears on the lock screen, in the Dynamic Island and - from iOS 26 -
// in the CarPlay dashboard. CarPlay shows the `.small` family (the same as
// the Apple Watch); if it is missing, it falls back to the Dynamic
// Island's compact views. Only what is in the model is shown - if a field
// is missing, the row is missing, and nothing is substituted.
import ActivityKit
import SwiftUI
import WidgetKit

@main
struct JoltWidgetBundle: WidgetBundle {
    var body: some Widget {
        JoltTripDisplay()
    }
}

// The extension applies from iOS 18: only there is the small family that
// CarPlay shows. The app itself keeps its minimum version; on older
// iPhones only the Live Activity is missing.
struct JoltTripDisplay: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: JoltTripAttributes.self) { context in
            TripView(state: context.state, stale: context.isStale)
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
                    Text(context.state.arrival?.text ?? "")
                        .font(.headline)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Text(context.state.short)
                        .font(.subheadline)
                }
            } compactLeading: {
                Text(context.state.soc?.text ?? "–")
            } compactTrailing: {
                Text(context.state.stop?.kmText ?? context.state.reserve?.text ?? "")
            } minimal: {
                Image(systemName: "bolt.fill")
            }
        }
        .supplementalActivityFamilies([.small])
    }
}

struct TripView: View {
    @Environment(\.activityFamily) private var familie

    let state: JoltDisplay
    let stale: Bool

    var body: some View {
        Group {
            switch familie {
            case .small:
                minor
            default:
                large
            }
        }
        .foregroundStyle(.white)
        .opacity(stale ? 0.55 : 1)
    }

    /// CarPlay and Watch: one large number and one line.
    private var minor: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(state.soc?.text ?? "–")
                .font(.system(size: 34, weight: .bold, design: .rounded))
            Text(row)
                .font(.caption)
                .lineLimit(2)
            if let five = state.history?.timeframe.first(where: { $0.min == 5 }),
               five.kwh100 != nil {
                Text("Ø 5 min \(five.text) kWh/100")
                    .font(.caption2)
                    .opacity(0.85)
            }
        }
    }

    private var large: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(state.soc?.text ?? "–")
                    .font(.system(size: 40, weight: .bold, design: .rounded))
                if let source = state.soc?.source, source != "gemessen" {
                    Text(source)
                        .font(.caption)
                        .opacity(0.8)
                }
                Spacer()
                if let arrival = state.arrival {
                    Text("Ankunft \(arrival.text)")
                        .font(.headline)
                }
            }
            if let stop = state.stop {
                Text("Stopp in \(stop.kmText): \(stop.name)"
                     + (stop.arrivalSocText.map { " (\($0))" } ?? ""))
                    .font(.subheadline)
            } else if let reserve = state.reserve {
                Text("Reserve in \(reserve.text)")
                    .font(.subheadline)
            }
            if let history = state.history {
                consumption(history)
            }
            if let row = auxRow {
                Text(row)
                    .font(.caption)
                    .opacity(0.9)
            }
            if let rest = state.rest {
                Text("Noch \(rest.text)")
                    .font(.caption)
                    .opacity(0.8)
            }
            if stale {
                Text("Keine aktuellen Werte")
                    .font(.caption.bold())
            }
        }
    }

    /// Consumption in kWh/100 km averaged over the last 1, 5, 30 and 60
    /// minutes, next to it the last thirty minutes as bars. What the car
    /// itself shows (since start) is deliberately not here.
    private func consumption(_ history: JoltDisplay.History) -> some View {
        HStack(alignment: .bottom, spacing: 10) {
            ForEach(history.timeframe, id: \.min) { timeframe in
                VStack(spacing: 0) {
                    Text(timeframe.text)
                        .font(.system(size: 17, weight: .semibold, design: .rounded))
                    Text(timeframe.min >= 60 ? "\(timeframe.min / 60) h" : "\(timeframe.min) min")
                        .font(.system(size: 10))
                        .opacity(0.75)
                }
            }
            Text("kWh/100")
                .font(.system(size: 10))
                .opacity(0.75)
            Spacer(minLength: 4)
            if let bar = history.bar {
                barChart(bar)
            }
        }
    }

    /// Six bars, the right one is the newest. A gap stays a gap.
    private func barChart(_ vals: [Double?]) -> some View {
        let highest = max(vals.compactMap { $0 }.max() ?? 1, 1)
        return HStack(alignment: .bottom, spacing: 3) {
            ForEach(vals.indices, id: \.self) { i in
                RoundedRectangle(cornerRadius: 2)
                    .frame(width: 6, height: vals[i].map { max(4, CGFloat($0 / highest) * 30) } ?? 2)
                    .opacity(vals[i] == nil ? 0.3 : 1)
            }
        }
        .frame(height: 30, alignment: .bottom)
    }

    /// Auxiliary loads, heating, climate, regeneration, battery - what the
    /// car does not show itself.
    private var auxRow: String? {
        var parts: [String] = []
        if let aux = state.aux {
            if let text = aux.text {
                parts.append("Neben \(text)" + (aux.source == "geschaetzt" ? "*" : ""))
            }
            if let heating = aux.heatingText { parts.append("Heizung \(heating)") }
            if let climate = aux.climateText { parts.append("Klima \(climate)") }
            if let batterie = aux.batterieText { parts.append("Akku \(batterie)") }
        }
        if let regen = state.history?.regen {
            parts.append("Rekup \(regen.percent) %")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// The single line for the small display.
    private var row: String {
        if stale { return "Keine aktuellen Werte" }
        if let stop = state.stop {
            return "Stopp in \(stop.kmText)"
                + (stop.arrivalSocText.map { " (\($0))" } ?? "")
        }
        if let reserve = state.reserve { return "Reserve in \(reserve.text)" }
        if let arrival = state.arrival { return arrival.text }
        return "Fahrt läuft"
    }
}
