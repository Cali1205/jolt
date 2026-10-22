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

    /// CarPlay and Watch: the course of the trip when there is one, otherwise
    /// one large number and one line.
    @ViewBuilder
    private var minor: some View {
        if let course = state.course {
            CourseTile(state: state, course: course, line: row)
        } else {
            minorText
        }
    }

    private var minorText: some View {
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

/// The CarPlay dashboard tile: the charge level, the state of charge over
/// the distance (measured against the plan) and the auxiliary consumers.
struct CourseTile: View {
    let state: JoltDisplay
    let course: JoltDisplay.Course
    let line: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(state.soc?.text ?? "–")
                    .font(.system(size: 26, weight: .bold, design: .rounded))
                    .layoutPriority(1)
                VStack(alignment: .leading, spacing: 0) {
                    if let delta = course.delta {
                        Text(delta == 0 ? "nach Plan" : "\(delta > 0 ? "+" : "−")\(abs(delta)) % zum Plan")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(delta >= 0 ? Tone.ok : Tone.warn)
                    }
                    Text(line)
                        .font(.caption2)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                if let arrival = state.arrival {
                    Text(arrival.text)
                        .font(.caption2)
                        .opacity(0.7)
                }
            }
            CourseChart(course: course, soc: state.soc?.percent, stop: state.stop)
                .frame(maxWidth: .infinity, minHeight: 22, maxHeight: .infinity)
            if let aux = state.aux {
                HStack(spacing: 0) {
                    consumer("flame.fill", aux.heatingKw, Tone.heat)
                    consumer("snowflake", aux.climateKw, Tone.climate)
                    consumer("sum", aux.kw, .white)
                }
            }
        }
    }

    /// Zero is "aus"; a value that was not read stays a dash, not "aus".
    private func consumer(_ symbol: String, _ kw: Double?, _ tone: Color) -> some View {
        let off = (kw ?? 0) <= 0
        return HStack(spacing: 3) {
            Image(systemName: symbol)
                .foregroundStyle(tone)
            Text(kw.map { $0 > 0 ? "\($0.formatted(.number.precision(.fractionLength(1)).locale(Locale(identifier: "de_DE")))) kW" : "aus" } ?? "–")
        }
        .font(.caption2.weight(.semibold))
        .opacity(off ? 0.4 : 1)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

enum Tone {
    static let ok = Color(red: 0.24, green: 0.86, blue: 0.59)
    static let warn = Color(red: 1.0, green: 0.79, blue: 0.24)
    static let low = Color(red: 1.0, green: 0.42, blue: 0.37)
    static let plan = Color(red: 0.42, green: 0.47, blue: 0.52)
    static let volt = Color(red: 0.36, green: 0.78, blue: 1.0)
    static let heat = Color(red: 1.0, green: 0.62, blue: 0.35)
    static let climate = Color(red: 0.5, green: 0.83, blue: 1.0)

    static func soc(_ percent: Double) -> Color {
        percent >= 40 ? ok : (percent >= 20 ? warn : low)
    }
}

/// State of charge over the distance: the plan dashed, the measured line
/// strong with a fill below it, the next charging stop as a bolt.
struct CourseChart: View {
    let course: JoltDisplay.Course
    let soc: Double?
    let stop: JoltDisplay.Stop?

    var body: some View {
        Canvas { context, size in
            // Without a plan the axis is the distance driven: leave room ahead
            // so the dot does not sit on the edge.
            let total = course.plan == nil ? max(course.km * 1.25, 10) : max(course.total, 1)
            let inset: CGFloat = 4
            let x = { (km: Double) in inset + (size.width - 2 * inset) * CGFloat(min(max(km / total, 0), 1)) }
            let y = { (soc: Double) in inset + (size.height - 2 * inset) * CGFloat(1 - min(max(soc, 0), 100) / 100) }
            let tone = Tone.soc(soc ?? course.actual.last?[1] ?? 50)
            let polyline = { (points: [[Double]]) -> Path in
                var path = Path()
                for (i, p) in points.enumerated() where p.count == 2 {
                    let at = CGPoint(x: x(p[0]), y: y(p[1]))
                    if i == 0 { path.move(to: at) } else { path.addLine(to: at) }
                }
                return path
            }

            if let reserve = course.reserve {
                var line = Path()
                line.move(to: CGPoint(x: inset, y: y(reserve)))
                line.addLine(to: CGPoint(x: size.width - inset, y: y(reserve)))
                context.stroke(line, with: .color(Tone.low.opacity(0.4)),
                               style: StrokeStyle(lineWidth: 1, dash: [2, 4]))
            }
            for km in course.stops ?? [] {
                var line = Path()
                line.move(to: CGPoint(x: x(km), y: inset))
                line.addLine(to: CGPoint(x: x(km), y: size.height - inset))
                context.stroke(line, with: .color(Tone.volt.opacity(0.35)), lineWidth: 1)
            }
            if let plan = course.plan {
                context.stroke(polyline(plan), with: .color(Tone.plan),
                               style: StrokeStyle(lineWidth: 1.5, lineJoin: .round, dash: [4, 3]))
            }
            if let first = course.actual.first, let last = course.actual.last, course.actual.count > 1 {
                var area = polyline(course.actual)
                area.addLine(to: CGPoint(x: x(last[0]), y: size.height - inset))
                area.addLine(to: CGPoint(x: x(first[0]), y: size.height - inset))
                area.closeSubpath()
                context.fill(area, with: .linearGradient(
                    Gradient(colors: [tone.opacity(0.42), tone.opacity(0)]),
                    startPoint: CGPoint(x: 0, y: inset), endPoint: CGPoint(x: 0, y: size.height)))
                context.stroke(polyline(course.actual), with: .color(tone),
                               style: StrokeStyle(lineWidth: 2.5, lineCap: .round, lineJoin: .round))
            }
            if let stop, let arrival = stop.arrivalSoc {
                let at = CGPoint(x: x(course.km + stop.km), y: y(Double(arrival)))
                context.fill(Path(ellipseIn: CGRect(x: at.x - 2.5, y: at.y - 2.5, width: 5, height: 5)),
                             with: .color(Tone.volt))
            }
            let now = CGPoint(x: x(course.km), y: y(soc ?? course.actual.last?[1] ?? 0))
            context.fill(Path(ellipseIn: CGRect(x: now.x - 6, y: now.y - 6, width: 12, height: 12)),
                         with: .color(tone.opacity(0.3)))
            context.fill(Path(ellipseIn: CGRect(x: now.x - 3.5, y: now.y - 3.5, width: 7, height: 7)),
                         with: .color(.white))
        }
    }
}
