import Foundation
#if canImport(CarPlay)
import CarPlay
import UIKit

/// jolt's CarPlay scene (category "EV charging").
///
/// Everything consists of iOS templates; jolt draws nothing itself. What
/// is shown is what the UI's display model supplies - the same one that
/// also feeds the Live Activity:
///
///   Tabs "Übersicht" (CPListImageRowItem, opens first) and "Tabelle"
///   (CPInformationTemplate), both with the same eight slots at most:
///     charge level, arrival, reserve, consumption (average of the trip
///     above the bars), auxiliary loads, regeneration, charging stops, and
///     as the last one the control tile: start / end the recording
///     Every value tile opens a page with the details; a stop opens its
///     information page
///
/// If something is missing, the row is missing. If the display is older
/// than three minutes or empty, it says there that no trip is running -
/// and nothing that looks like a current value.
///
/// iOS loads this class by its name from the Info.plist
/// (`UISceneDelegateClassName`); `tools/ios_carplay.sh` enters it there.
@objc(JoltCarPlaySceneDelegate)
public class JoltCarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    private var iface: CPInterfaceController?
    private var imagesList: CPListTemplate?
    private var table: CPInformationTemplate?
    private var observerId: UUID?
    private var resultId: UUID?
    /// "wird gestartet …" until the result is in.
    private var running: String?
    private var runningSince: Date?

    /// From when a display counts as stale.
    private let freshnessSeconds: TimeInterval = 180
    /// The template in the car takes no more rows per section.
    private let rowsMax = 10

    public func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didConnect interfaceController: CPInterfaceController
    ) {
        iface = interfaceController
        record(JoltDisplayStore.shared.latest)
        // The same values in two arrangements: image rows and table (text only).
        let model = JoltDisplayStore.shared.latest
        let entries = self.entries(model)

        let rows = CPListTemplate(title: "jolt", sections: imageRows(entries))
        rows.tabTitle = "Übersicht"
        rows.tabImage = UIImage(systemName: "photo.on.rectangle")
        rows.trailingNavigationBarButtons = toolbar(model)
        imagesList = rows

        let board = CPInformationTemplate(
            title: "jolt", layout: .leading, items: tablesRows(entries), actions: [])
        board.tabTitle = "Tabelle"
        board.tabImage = UIImage(systemName: "list.bullet")
        board.trailingNavigationBarButtons = toolbar(model)
        table = board

        // The overview comes first: that is the view the app opens with.
        let tab = CPTabBarTemplate(templates: [rows, board])
        interfaceController.setRootTemplate(tab, animated: false, completion: nil)

        observerId = JoltDisplayStore.shared.observe { [weak self] model in
            guard let self = self else { return }
            // When a fresh display arrives, the recording is running: the "wird
            // gestartet" is done.
            if self.fresh(model) != nil { self.running = nil }
            self.record(model)
            self.show(model)
        }
        resultId = JoltDisplayStore.shared.observeResult { [weak self] action, ok, text in
            self?.result(action: action, ok: ok, text: text)
        }
    }

    public func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didDisconnectInterfaceController interfaceController: CPInterfaceController
    ) {
        if let ident = observerId {
            JoltDisplayStore.shared.stopObserving(ident)
        }
        observerId = nil
        if let ident = resultId {
            JoltDisplayStore.shared.stopObservingResult(ident)
        }
        resultId = nil
        running = nil
        imagesList = nil
        table = nil
        iface = nil
    }

    // MARK: - Content

    private func fresh(_ model: JoltDisplay?) -> JoltDisplay? {
        guard let model = model else { return nil }
        let age = Date().timeIntervalSince1970 - model.as_of / 1000
        return age <= freshnessSeconds ? model : nil
    }

    /// The tiles have **fixed slots**; if a value is missing, the slot
    /// stays with a dash. Otherwise the others would shift up as soon as a
    /// value arrives or goes, and you would have to search again while
    /// driving.
    ///
    ///   top:    charge level (with history) | arrival | reserve (start/stop: title bar)
    ///   bottom: consumption (bars) | auxiliary loads (history)
    ///           | regeneration (history) | charging stops
    ///
    /// CarPlay takes no more than eight tiles.
    private func entries(_ raw_model: JoltDisplay?) -> [Entry] {
        guard let model = fresh(raw_model) else {
            return [emptyTile("Keine laufende Fahrt"), controlTile(running: false)]
        }
        var result: [Entry] = []
        /// The image the UI drew for this slot (style "a" or "b"); nil means:
        /// Swift draws as before.
        func picture(_ place: String) -> UIImage? { finishedImage(place, model) }

        if let soc = model.soc {
            result.append(tile(
                title: "Ladestand", val: soc.text, minor: nil,
                line: series("soc"), picture: picture("soc"),
                handler: { [weak self] in self?.showSoc(model) }))
        } else {
            result.append(emptyTile("Ladestand", picture: picture("soc")))
        }
        if let arrival = model.arrival {
            result.append(tile(title: "Ankunft", val: arrival.text, minor: nil,
                                   picture: picture("arrival"),
                                   handler: { [weak self] in self?.showArrival(model) }))
        } else {
            result.append(emptyTile("Ankunft", picture: picture("arrival")))
        }
        if let reserve = model.reserve {
            result.append(tile(title: "Reserve", val: reserve.text, minor: "in",
                                   picture: picture("reserve"),
                                   handler: { [weak self] in self?.showReserve(model) }))
        } else {
            result.append(emptyTile("Reserve", picture: picture("reserve")))
        }

        let timeframe = (model.history?.timeframe ?? []).filter { $0.kwh100 != nil }
        if !timeframe.isEmpty || model.history?.bar != nil {
            // At the top the average of the whole trip (before the first
            // kilometre: the shortest window), below it the last thirty
            // minutes in bars of five minutes.
            let trip = model.history?.trip
            result.append(tile(
                title: trip == nil ? "kWh/100 km" : "Ø Verbrauch",
                val: trip?.text ?? timeframe.first?.text ?? "–",
                minor: nil,
                bar: model.history?.bar,
                picture: picture("consumption"),
                handler: { [weak self] in self?.showConsumption(model) }))
        } else {
            result.append(emptyTile("kWh/100 km", picture: picture("consumption")))
        }

        var parts: [String] = []
        if let aux = model.aux {
            if let text = aux.text { parts.append("Neben \(text)") }
            if let heating = aux.heatingText { parts.append("Heizung \(heating)") }
            if let climate = aux.climateText { parts.append("Klima \(climate)") }
            if let battery = aux.batterieText { parts.append("Akku \(battery)") }
        }
        if let aux = model.aux, !parts.isEmpty {
            result.append(tile(
                title: "Nebenverbraucher",
                val: aux.text ?? aux.heatingText ?? aux.climateText
                    ?? aux.batterieText ?? "–",
                minor: nil,
                line: series("aux"),
                picture: picture("aux"),
                handler: { [weak self] in self?.showAux(model) }))
        } else {
            result.append(emptyTile("Nebenverbraucher", picture: picture("aux")))
        }

        if let regen = model.history?.regen {
            result.append(tile(
                title: "Rekuperation",
                val: "\(regen.percent) %",
                minor: nil,
                line: series("regen"),
                picture: picture("regen"),
                handler: { [weak self] in self?.showRegen(model) }))
        } else {
            result.append(emptyTile("Rekuperation", picture: picture("regen")))
        }

        // A recording without a plan has no stops: that is not an error, the
        // slot then stays empty.
        if let stops = model.stopList, !stops.isEmpty {
            result.append(tile(
                title: "Ladestopps", val: "\(stops.count)", minor: nil,
                picture: picture("stops"),
                handler: { [weak self] in self?.showStops(stops) }))
        } else {
            result.append(emptyTile("Ladestopps", picture: picture("stops")))
        }
        // The seven value tiles are fixed; the control tile is the eighth.
        result.append(controlTile(running: true))
        return Array(result.prefix(8))
    }

    /// The control tile: starts the recording, or ends it while one is
    /// running. The same actions as the button in the title bar, but in the
    /// middle of the view and as large as the other tiles. While the UI has
    /// not answered yet it shows that and does nothing.
    private func controlTile(running active: Bool) -> Entry {
        if let text = running {
            return Entry(title: text, val: "…", picture: controlImage(.wait),
                         handler: nil, control: true)
        }
        if active {
            return Entry(title: "Beenden", val: "", picture: controlImage(.stop),
                         handler: { [weak self] in self?.endAsk() }, control: true)
        }
        return Entry(title: "Start", val: "", picture: controlImage(.start),
                     handler: { [weak self] in self?.launch() }, control: true)
    }

    private enum Control { case start, stop, wait }

    /// Dark tile with a large symbol in the colour of its meaning (the same
    /// colours as the tiles of the UI): green triangle, red square, three dots.
    private func controlImage(_ kind: Control) -> UIImage {
        let page: CGFloat = 120
        let format = UIGraphicsImageRendererFormat()
        format.scale = 2
        let ground = UIColor(red: 0.078, green: 0.122, blue: 0.169, alpha: 1)
        let edge = UIColor(red: 0.153, green: 0.220, blue: 0.290, alpha: 1)
        let good = UIColor(red: 0.212, green: 0.878, blue: 0.545, alpha: 1)
        let bad = UIColor(red: 1.0, green: 0.310, blue: 0.373, alpha: 1)
        let muted = UIColor(red: 0.525, green: 0.588, blue: 0.651, alpha: 1)
        return UIGraphicsImageRenderer(size: CGSize(width: page, height: page), format: format).image { _ in
            let frame = CGRect(x: 0.5, y: 0.5, width: page - 1, height: page - 1)
            ground.setFill()
            UIBezierPath(roundedRect: frame, cornerRadius: 17).fill()
            edge.setStroke()
            let outline = UIBezierPath(roundedRect: frame, cornerRadius: 17)
            outline.lineWidth = 1
            outline.stroke()
            let mid = page / 2
            switch kind {
            case .start:
                good.setFill()
                let t = UIBezierPath()
                t.move(to: CGPoint(x: mid - 17, y: mid - 28))
                t.addLine(to: CGPoint(x: mid + 31, y: mid))
                t.addLine(to: CGPoint(x: mid - 17, y: mid + 28))
                t.close()
                t.fill()
            case .stop:
                bad.setFill()
                UIBezierPath(roundedRect: CGRect(x: mid - 26, y: mid - 26, width: 52, height: 52),
                             cornerRadius: 8).fill()
            case .wait:
                muted.setFill()
                for i in -1...1 {
                    UIBezierPath(ovalIn: CGRect(x: mid + CGFloat(i) * 24 - 7, y: mid - 7,
                                                width: 14, height: 14)).fill()
                }
            }
        }
    }

    private func emptyTile(_ title: String, picture: UIImage? = nil) -> Entry {
        tile(title: title, val: "–", minor: nil, picture: picture)
    }

    /// The UI's finished image for a tile slot - or nil if none came along
    /// (style "klassisch", older UI, image not readable). In that case
    /// `tileImage` draws as before.
    private func finishedImage(_ place: String, _ model: JoltDisplay) -> UIImage? {
        guard model.look != nil, model.look != "klassisch",
              let text = model.tileImages?[place],
              let records = Data(base64Encoded: text),
              let picture = UIImage(data: records, scale: 2) else { return nil }
        return picture
    }

    /// The image scaled to the largest size the template allows, keeping
    /// the aspect ratio. A smaller image would be shown smaller than it could
    /// be; a larger one the system would scale down or crop.
    ///
    /// Nil bound (the system states none): the image stays as it is.
    private func fitted(_ picture: UIImage, to bound: CGSize?) -> UIImage {
        guard let bound = bound, bound.width > 0, bound.height > 0,
              picture.size.width > 0, picture.size.height > 0 else { return picture }
        let factor = min(bound.width / picture.size.width, bound.height / picture.size.height)
        guard abs(factor - 1) > 0.01 else { return picture }
        let destination = CGSize(width: (picture.size.width * factor).rounded(),
                                 height: (picture.size.height * factor).rounded())
        let format = UIGraphicsImageRendererFormat()
        format.scale = picture.scale
        return UIGraphicsImageRenderer(size: destination, format: format).image { _ in
            picture.draw(in: CGRect(origin: .zero, size: destination))
        }
    }

    /// Largest image in an image row.
    private var rowBound: CGSize? { CPListImageRowItem.maximumImageSize }

    // MARK: - History

    /// The last thirty minutes per value. They live as long as the app
    /// (not just the scene): anyone who disconnects CarPlay in between
    /// should not start from zero on reconnecting. A gap of more than three
    /// minutes starts the series anew - that is a different trip.
    private static var series_list: [String: [(timestamp: Date, val: Double)]] = [:]

    private func record(_ raw_model: JoltDisplay?) {
        guard let model = fresh(raw_model) else { return }
        remember("soc", model.soc?.percent)
        remember("aux", model.aux?.kw)
        remember("regen", model.history?.regen.map { Double($0.percent) })
    }

    private func remember(_ name: String, _ val: Double?) {
        guard let val = val else { return }
        let now_ts = Date()
        var lst = JoltCarPlaySceneDelegate.series_list[name] ?? []
        if let tail = lst.last, now_ts.timeIntervalSince(tail.timestamp) > freshnessSeconds {
            lst = []
        }
        // At most one point per 20 seconds: the display arrives more often.
        if let tail = lst.last, now_ts.timeIntervalSince(tail.timestamp) < 20 {
            lst[lst.count - 1] = (now_ts, val)
        } else {
            lst.append((now_ts, val))
        }
        lst.removeAll { now_ts.timeIntervalSince($0.timestamp) > 1800 }
        JoltCarPlaySceneDelegate.series_list[name] = lst
    }

    private func series(_ name: String) -> [Double]? {
        guard let lst = JoltCarPlaySceneDelegate.series_list[name], lst.count >= 2 else { return nil }
        return lst.map { $0.val }
    }


    private func showStops(_ stops: [JoltDisplay.ListStop]) {
        let rows: [CPListItem] = stops.prefix(rowsMax).map { stop in
            let item = CPListItem(text: stop.name, detailText: stopRow(stop))
            item.handler = { [weak self] _, finalize in
                self?.showStop(stop)
                finalize()
            }
            return item
        }
        let page = CPListTemplate(
            title: "Ladestopps", sections: [CPListSection(items: rows)])
        iface?.pushTemplate(page, animated: true, completion: nil)
    }

    /// Page with the details of a tile; the same information template for
    /// all of them.
    private func push(_ title: String, _ points: [CPInformationItem]) {
        let page = CPInformationTemplate(
            title: title, layout: .leading,
            items: points.isEmpty ? [CPInformationItem(title: "Keine Werte", detail: nil)] : points,
            actions: [])
        iface?.pushTemplate(page, animated: true, completion: nil)
    }

    private func number(_ val: Double, _ digits: Int = 1, signed: Bool = false) -> String {
        let f = NumberFormatter()
        f.locale = Locale(identifier: "de_DE")
        f.minimumFractionDigits = digits
        f.maximumFractionDigits = digits
        if signed { f.positivePrefix = "+" }
        return f.string(from: NSNumber(value: val)) ?? "\(val)"
    }

    /// The last thirty minutes of a remembered value: change, lowest, highest.
    private func trend(_ name: String, unit: String, digits: Int = 1) -> CPInformationItem? {
        guard let lst = JoltCarPlaySceneDelegate.series_list[name], lst.count >= 2,
              let first = lst.first, let last = lst.last else { return nil }
        let mins = max(1, Int((last.timestamp.timeIntervalSince(first.timestamp) / 60).rounded()))
        let vals = lst.map { $0.val }
        return CPInformationItem(
            title: "Verlauf \(mins) min",
            detail: "\(number(last.val - first.val, digits, signed: true)) \(unit)"
                + " · min \(number(vals.min() ?? 0, digits))"
                + " · max \(number(vals.max() ?? 0, digits))")
    }

    private func nextStop(_ model: JoltDisplay) -> CPInformationItem? {
        guard let stop = model.stop else { return nil }
        var detail = "in \(stop.kmText)"
        if let soc = stop.arrivalSocText {
            detail += " · \(soc) bei Ankunft" + (stop.planned == true ? " (geplant)" : "")
        }
        return CPInformationItem(title: "Nächster Stopp: \(stop.name)", detail: detail)
    }

    private func showSoc(_ model: JoltDisplay) {
        var points: [CPInformationItem] = []
        if let soc = model.soc {
            points.append(CPInformationItem(
                title: "Ladestand",
                detail: soc.text + (soc.source.map { " · " + sourceText($0) } ?? "")))
        }
        if let item = trend("soc", unit: "%-Punkte") { points.append(item) }
        if let item = nextStop(model) { points.append(item) }
        if let rest = model.rest {
            points.append(CPInformationItem(title: "Reststrecke", detail: rest.text))
        }
        push("Ladestand", points)
    }

    private func showArrival(_ model: JoltDisplay) {
        var points: [CPInformationItem] = []
        if let arrival = model.arrival {
            points.append(CPInformationItem(title: "Ankunft gegenüber Plan", detail: arrival.text))
        }
        if let rest = model.rest {
            points.append(CPInformationItem(title: "Reststrecke", detail: rest.text))
        }
        if let item = nextStop(model) { points.append(item) }
        push("Ankunft", points)
    }

    private func showReserve(_ model: JoltDisplay) {
        var points: [CPInformationItem] = []
        if let reserve = model.reserve {
            points.append(CPInformationItem(title: "Reserve in", detail: reserve.text))
        }
        if let rest = model.rest {
            points.append(CPInformationItem(title: "Reststrecke", detail: rest.text))
        }
        if let item = nextStop(model) { points.append(item) }
        push("Reserve", points)
    }

    private func showRegen(_ model: JoltDisplay) {
        var points: [CPInformationItem] = []
        if let regen = model.history?.regen {
            points.append(CPInformationItem(
                title: "Rekuperation",
                detail: "\(regen.percent) % der verbrauchten Energie · letzte \(timeframeName(regen.mins))"))
        }
        if let item = trend("regen", unit: "%-Punkte", digits: 0) { points.append(item) }
        push("Rekuperation", points)
    }

    private func showConsumption(_ model: JoltDisplay) {
        var points: [CPInformationItem] = []
        if let trip = model.history?.trip {
            points.append(CPInformationItem(
                title: "Ø Fahrt",
                detail: "\(trip.text) kWh/100 km · \(number(trip.km, 0)) km"))
        }
        for f in model.history?.timeframe ?? [] where f.kwh100 != nil {
            points.append(CPInformationItem(
                title: "letzte \(timeframeName(f.min))",
                detail: "\(f.text) kWh/100 km · \(f.kwText) kW"))
        }
        if let bar = model.history?.bar {
            // Oldest first, like in the tile; a gap is a dash.
            let text = bar.map { $0.map { number($0) } ?? "–" }.joined(separator: " · ")
            points.append(CPInformationItem(title: "Balken je 5 min (älteste zuerst)", detail: text))
        }
        push("Verbrauch", points)
    }

    private func showAux(_ model: JoltDisplay) {
        var points: [CPInformationItem] = []
        if let aux = model.aux {
            if let text = aux.text {
                points.append(CPInformationItem(
                    title: "Nebenverbraucher",
                    detail: text + (aux.source.map { " · " + sourceText($0) } ?? "")))
            }
            if let heating = aux.heatingText {
                points.append(CPInformationItem(title: "Heizung", detail: heating))
            }
            if let climate = aux.climateText {
                points.append(CPInformationItem(title: "Klima", detail: climate))
            }
            if let battery = aux.batterieText {
                points.append(CPInformationItem(title: "Akku", detail: battery))
            }
        }
        if let item = trend("aux", unit: "kW") { points.append(item) }
        push("Nebenverbraucher", points)
    }

    // MARK: - Drawing tiles

    private func tile(title: String, val: String, minor: String?,
                        bar: [Double?]? = nil,
                        line: [Double]? = nil,
                        picture: UIImage? = nil,
                        handler: (() -> Void)? = nil) -> Entry {
        Entry(
            title: title, val: val,
            picture: picture ?? tileImage(val: val, minor: minor, bar: bar, line: line),
            handler: handler)
    }

    /// A slot of the display, independent of the template that shows it.
    private struct Entry {
        let title: String
        let val: String
        let picture: UIImage
        let handler: (() -> Void)?
        /// The tile that starts / ends the recording: not a value, so it does
        /// not appear in the table.
        var control = false
    }

    /// Image rows of four images each; the caption under the image only
    /// exists from iOS 17.4, before that only the image is shown.
    private func imageRows(_ entries: [Entry]) -> [CPListSection] {
        let perRow = 4
        var rows: [CPListTemplateItem] = []
        var start = 0
        while start < entries.count {
            let part = Array(entries[start..<min(start + perRow, entries.count)])
            let pictures = part.map { fitted($0.picture, to: rowBound) }
            let row: CPListImageRowItem
            if #available(iOS 17.4, *) {
                row = CPListImageRowItem(
                    text: "", images: pictures, imageTitles: part.map { $0.title })
            } else {
                row = CPListImageRowItem(text: part.map { $0.title }.joined(separator: " · "),
                                           images: pictures)
            }
            row.listImageRowHandler = { _, index, finalize in
                if index >= 0 && index < part.count { part[index].handler?() }
                finalize()
            }
            rows.append(row)
            start += perRow
        }
        return [CPListSection(items: rows)]
    }

    private func tablesRows(_ entries: [Entry]) -> [CPInformationItem] {
        entries.filter { !$0.control }.map { CPInformationItem(title: $0.title, detail: $0.val) }
    }

    /// A tile as an image: a large value, below it a small line or a bar
    /// chart. For tiles CarPlay draws only an image and a title; everything
    /// else has to go into the image.
    private func tileImage(val: String, minor: String?, bar: [Double?]?,
                            line: [Double]?) -> UIImage {
        let page: CGFloat = 120
        let dimension = CGSize(width: page, height: page)
        let traits = iface?.carTraitCollection ?? UITraitCollection.current
        let format = UIGraphicsImageRendererFormat()
        format.scale = traits.displayScale > 0 ? traits.displayScale : 2
        let typeface = UIColor.label.resolvedColor(with: traits)
        let muted = UIColor.secondaryLabel.resolvedColor(with: traits)
        let barColor = UIColor.systemGreen.resolvedColor(with: traits)
        let area = UIColor.tertiarySystemFill.resolvedColor(with: traits)
        // "withBar" means: a chart below the value (bars or line).
        let withBar = (bar?.contains(where: { $0 != nil }) ?? false) || line != nil

        return UIGraphicsImageRenderer(size: dimension, format: format).image { _ in
            area.setFill()
            UIBezierPath(roundedRect: CGRect(origin: .zero, size: dimension),
                         cornerRadius: 14).fill()

            // Value: as large as possible without touching the edge.
            var point: CGFloat = withBar ? 32 : 40
            var attribute: [NSAttributedString.Key: Any] = [:]
            repeat {
                attribute = [.font: UIFont.systemFont(ofSize: point, weight: .semibold),
                             .foregroundColor: typeface]
                point -= 2
            } while (val as NSString).size(withAttributes: attribute).width > page - 12
                && point > 12
            let mass = (val as NSString).size(withAttributes: attribute)
            (val as NSString).draw(
                at: CGPoint(x: (page - mass.width) / 2, y: withBar ? 6 : 24),
                withAttributes: attribute)

            if let line = line, line.count >= 2 {
                let low = line.min() ?? 0
                let high = line.max() ?? 1
                let span = max(high - low, 0.0001)
                let bottom: CGFloat = page - 12
                let place: CGFloat = 46
                let step = (page - 20) / CGFloat(line.count - 1)
                let fs_path = UIBezierPath()
                for (i, entry) in line.enumerated() {
                    // A flat line sits in the middle, not on the floor.
                    let share = high - low < 0.0001 ? 0.5 : (entry - low) / span
                    let p = CGPoint(x: 10 + CGFloat(i) * step,
                                    y: bottom - place * CGFloat(share))
                    if i == 0 { fs_path.move(to: p) } else { fs_path.addLine(to: p) }
                }
                fs_path.lineWidth = 3
                fs_path.lineJoinStyle = .round
                barColor.setStroke()
                fs_path.stroke()
            } else if withBar, let bar = bar {
                let vals = bar.compactMap { $0 }
                let peak = max(vals.max() ?? 1, 1)
                let bottom: CGFloat = page - 10
                let place: CGFloat = 50
                let count = CGFloat(bar.count)
                let gap: CGFloat = 4
                let extent = (page - 20 - gap * (count - 1)) / count
                for (i, entry) in bar.enumerated() {
                    let x = 10 + CGFloat(i) * (extent + gap)
                    guard let entry = entry else {
                        // Gap: stale, not zero consumption.
                        muted.setFill()
                        UIBezierPath(rect: CGRect(x: x, y: bottom - 2, width: extent, height: 2)).fill()
                        continue
                    }
                    let elevation = max(3, place * CGFloat(max(entry, 0) / peak))
                    barColor.setFill()
                    UIBezierPath(roundedRect: CGRect(x: x, y: bottom - elevation, width: extent, height: elevation),
                                 cornerRadius: 2).fill()
                }
            } else if let minor = minor {
                let a: [NSAttributedString.Key: Any] = [
                    .font: UIFont.systemFont(ofSize: 15, weight: .regular),
                    .foregroundColor: muted]
                let w = (minor as NSString).size(withAttributes: a).width
                (minor as NSString).draw(
                    at: CGPoint(x: max(6, (page - w) / 2), y: page - 38), withAttributes: a)
            }
        }
    }

    // MARK: - Start and stop the recording

    /// Start and stop as a small button in the title bar: they need no
    /// tile, and the space belongs to the values. Stop asks for
    /// confirmation, an accidental tap does not end anything.
    private func toolbar(_ raw_model: JoltDisplay?) -> [CPBarButton] {
        if let text = running {
            let wait = CPBarButton(title: text, handler: nil)
            wait.isEnabled = false
            return [wait]
        }
        if fresh(raw_model) != nil {
            return [CPBarButton(title: "Beenden") { [weak self] _ in self?.endAsk() }]
        }
        return [CPBarButton(title: "Start") { [weak self] _ in self?.launch() }]
    }

    private func show(_ model: JoltDisplay?) {
        let entries = self.entries(model)
        imagesList?.updateSections(imageRows(entries))
        imagesList?.trailingNavigationBarButtons = toolbar(model)
        table?.items = tablesRows(entries)
        table?.trailingNavigationBarButtons = toolbar(model)
    }


    private func launch() {
        guard running == nil else { return }
        guard JoltDisplayStore.shared.requestAction("starten") else {
            hint("jolt läuft auf dem iPhone nicht. Öffne jolt einmal auf dem iPhone, dann geht es von hier.")
            return
        }
        running = "wird gestartet …"
        refresh()
        watch()
    }

    /// If the UI does not answer (the app is frozen, the iPhone locked),
    /// "wird gestartet …" would stay forever and block every further
    /// attempt. After 25 seconds this is stated.
    private func watch() {
        let brand = Date()
        runningSince = brand
        DispatchQueue.main.asyncAfter(deadline: .now() + 25) { [weak self] in
            guard let self = self, self.runningSince == brand, self.running != nil else { return }
            self.running = nil
            self.refresh()
            self.hint("Keine Antwort von jolt. Ist die App auf dem iPhone geöffnet?")
        }
    }

    /// Stop with confirmation: an accidental tap must not end a trip.
    private func endAsk() {
        guard running == nil else { return }
        let ja = CPAlertAction(title: "Beenden", style: .destructive) { [weak self] _ in
            self?.iface?.dismissTemplate(animated: true, completion: nil)
            guard let self = self else { return }
            if JoltDisplayStore.shared.requestAction("beenden") {
                self.running = "wird beendet …"
                self.refresh()
                self.watch()
            } else {
                self.hint("jolt läuft auf dem iPhone nicht. Die Aufzeichnung lässt sich dort beenden.")
            }
        }
        let no = CPAlertAction(title: "Abbrechen", style: .cancel) { [weak self] _ in
            self?.iface?.dismissTemplate(animated: true, completion: nil)
        }
        let question = CPAlertTemplate(
            titleVariants: ["Aufzeichnung beenden?"], actions: [ja, no])
        iface?.presentTemplate(question, animated: true, completion: nil)
    }

    /// The UI's answer. A success shows itself (the display appears or
    /// disappears); only an error needs a message.
    private func result(action: String, ok: Bool, text: String) {
        running = nil
        runningSince = nil
        refresh()
        if !ok {
            let reason = text.isEmpty ? "Das hat nicht geklappt." : text
            hint((action == "starten" ? "Start fehlgeschlagen: " : "Beenden fehlgeschlagen: ") + reason)
        }
    }

    private func refresh() {
        show(JoltDisplayStore.shared.latest)
    }

    private func hint(_ text: String) {
        let ok = CPAlertAction(title: "OK", style: .default) { [weak self] _ in
            self?.iface?.dismissTemplate(animated: true, completion: nil)
        }
        let report = CPAlertTemplate(titleVariants: [text], actions: [ok])
        iface?.presentTemplate(report, animated: true, completion: nil)
    }

    private func stopRow(_ stop: JoltDisplay.ListStop) -> String {
        var parts = ["in \(stop.kmText)"]
        if let soc = stop.arrivalSocText { parts.append("\(soc) bei Ankunft") }
        if let timestamp = stop.chargeTimeText { parts.append(timestamp) }
        return parts.joined(separator: " · ")
    }

    private func showStop(_ stop: JoltDisplay.ListStop) {
        var points: [CPInformationItem] = [
            CPInformationItem(title: "Entfernung", detail: stop.kmText)
        ]
        if let soc = stop.arrivalSocText {
            points.append(CPInformationItem(title: "Ladestand bei Ankunft", detail: soc))
        }
        if let timestamp = stop.chargeTimeText {
            points.append(CPInformationItem(title: "Ladezeit", detail: timestamp))
        }
        if let soc = stop.departureSocText {
            points.append(CPInformationItem(title: "Ladestand bei Abfahrt", detail: soc))
        }
        if let operatorName = stop.operator {
            points.append(CPInformationItem(title: "Betreiber", detail: operatorName))
        }
        if let kw = stop.powerKw {
            points.append(CPInformationItem(title: "Leistung", detail: "bis \(kw) kW"))
        }
        let page = CPInformationTemplate(
            title: stop.name, layout: .leading, items: points, actions: [])
        iface?.pushTemplate(page, animated: true, completion: nil)
    }

    private func sourceText(_ source: String?) -> String {
        switch source {
        case "gemessen": return "gemessen"
        case "zuletzt": return "zuletzt gemessen"
        case "gerechnet": return "gerechnet"
        default: return source ?? ""
        }
    }

    private func timeframeName(_ mins: Int) -> String {
        mins >= 60 ? "\(mins / 60) h" : "\(mins) min"
    }
}
#endif
