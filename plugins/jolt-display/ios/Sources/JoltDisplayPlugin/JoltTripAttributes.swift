// This file exists twice in the built project: here in the plugin and as
// a copy in the widget target (tools/ios_widget.sh copies it). The Live
// Activity connects the two via the names of the types and the Codable
// representation - the file is therefore the single source, and changing
// it changes both sides at once.
//
// The fields are the display model from frontend/display.js, key for
// key. What is missing there (no plan, no charging stop) is nil here.
import Foundation

public struct JoltDisplay: Codable, Hashable {
    public struct Soc: Codable, Hashable {
        public var percent: Double
        public var text: String
        public var source: String?
    }

    public struct Stop: Codable, Hashable {
        public var name: String
        public var km: Double
        public var kmText: String
        public var arrivalSoc: Int?
        public var arrivalSocText: String?
        public var planned: Bool?
    }

    /// Reserve, arrival and rest carry a number besides the text; the
    /// display only needs the text, the number is skipped when reading.
    public struct Row: Codable, Hashable {
        public var text: String
    }

    /// Consumption averaged over one, five, thirty and sixty minutes.
    /// `text` is "–" if the trace does not cover the window.
    public struct Timeframe: Codable, Hashable {
        public var min: Int
        public var kwh100: Double?
        public var kw: Double?
        public var text: String
        public var kwText: String
    }

    /// Share of the drawn energy that was fed back.
    public struct Regen: Codable, Hashable {
        public var percent: Int
        public var mins: Int
    }

    public struct History: Codable, Hashable {
        public var timeframe: [Timeframe]
        /// The last thirty minutes in bars of five minutes, oldest first;
        /// nil is a gap (stale), not zero consumption.
        public var bar: [Double?]?
        public var regen: Regen?
    }

    /// What the car draws without driving, and what else it gives.
    public struct Aux: Codable, Hashable {
        public var kw: Double?
        public var text: String?
        public var source: String?
        public var heatingKw: Double?
        public var heatingText: String?
        public var climateKw: Double?
        public var climateText: String?
        public var batterieC: Int?
        public var batterieText: String?
    }

    /// A charging stop of the trip, as the CarPlay list shows it. The Live
    /// Activity does not get it: it may carry 4 KB at most.
    public struct ListStop: Codable, Hashable {
        public var name: String
        public var km: Double
        public var kmText: String
        public var arrivalSoc: Int?
        public var arrivalSocText: String?
        public var departureSocText: String?
        public var chargeTimeMin: Int?
        public var chargeTimeText: String?
        public var operator: String?
        public var powerKw: Int?
    }

    /// How the CarPlay tiles are presented: "klassisch" (Swift draws), "a"
    /// or "b" (the UI supplies the images). If the field is missing,
    /// "klassisch" applies.
    public var look: String?
    /// One PNG in Base64 per tile slot, drawn by the UI
    /// (frontend/tiles.js): soc, ankunft, reserve, verbrauch, neben, rekup,
    /// stopps. For CarPlay only - the Live Activity may carry 4 KB at most
    /// and does not get them.
    public var tileImages: [String: String]?

    public var short: String
    public var history: History?
    public var aux: Aux?
    public var stopList: [ListStop]?
    public var soc: Soc?
    public var stop: Stop?
    public var reserve: Row?
    public var arrival: Row?
    public var rest: Row?
    /// Milliseconds since 1970, as JavaScript delivers them.
    public var as_of: Double
}

#if canImport(ActivityKit)
import ActivityKit

@available(iOS 16.2, *)
public struct JoltTripAttributes: ActivityAttributes {
    public typealias ContentState = JoltDisplay

    public var name: String

    public init(name: String) {
        self.name = name
    }
}
#endif
