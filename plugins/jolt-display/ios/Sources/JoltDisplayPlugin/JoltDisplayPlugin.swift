import Foundation
import Capacitor
#if canImport(ActivityKit)
import ActivityKit
#endif

/// Brings the display model of the UI into a Live Activity.
///
/// Why a Live Activity: it has a beginning and an end - the trip -, it is
/// updated by the app, which is running anyway (background location,
/// dongle), and it appears on the lock screen and in the CarPlay dashboard
/// without Apple having to approve the app as a CarPlay app.
///
/// **The app may only start it in the foreground.** The driver starts the
/// recording with a tap, so that holds. If it ever fails (for example after
/// a reload in the background), `refresh` reports an error, and the UI's
/// next heartbeat tries again.
@objc(JoltDisplayPlugin)
public class JoltDisplayPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "JoltDisplayPlugin"
    public let jsName = "JoltDisplay"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "available", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "refresh", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "finish", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "ready", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "actionResult", returnType: CAPPluginReturnPromise)
    ]

    /// The plugin is loaded, so the UI is running: CarPlay may give it
    /// jobs (start and stop the recording). The requests from the list arrive
    /// as a notification and go to the JavaScript (trips.js) as the event
    /// "carplayAction".
    public override func load() {
        JoltDisplayStore.shared.reportBridge(true)
        NotificationCenter.default.addObserver(
            forName: JoltDisplayStore.actionName, object: nil, queue: .main
        ) { [weak self] report in
            guard let action = report.userInfo?["action"] as? String else { return }
            self?.notifyListeners("carplayAction", data: ["action": action])
        }
    }

    /// How long a display counts as fresh without a new report. The UI
    /// reports at least every minute; after that the activity shows
    /// "stale" instead of presenting an old charge level as current.
    private let freshnessSeconds: TimeInterval = 180

    @objc func available(_ call: CAPPluginCall) {
        #if canImport(ActivityKit)
        if #available(iOS 16.2, *) {
            call.resolve(["available": ActivityAuthorizationInfo().areActivitiesEnabled])
            return
        }
        #endif
        call.resolve(["available": false])
    }

    /// `json`: the display model as a string. A string rather than an
    /// object, so the bridge does not reshape anything - it is read here
    /// exactly once, with the same keys as in display.js.
    @objc func refresh(_ call: CAPPluginCall) {
        guard let text = call.getString("json"), let records = text.data(using: .utf8) else {
            call.reject("json fehlt")
            return
        }
        let state: JoltDisplay
        do {
            state = try JSONDecoder().decode(JoltDisplay.self, from: records)
        } catch {
            call.reject("Anzeigemodell nicht lesbar: \(error.localizedDescription)")
            return
        }
        // First for CarPlay, with the list of stops - regardless of whether
        // the Live Activity succeeds (it may only begin in the foreground).
        JoltDisplayStore.shared.assign(state)

        #if canImport(ActivityKit)
        guard #available(iOS 16.2, *) else {
            call.reject("Live Activities brauchen iOS 16.2")
            return
        }
        // The Live Activity carries 4 KB at most; the stop list belongs only
        // in the CarPlay template.
        var forActivity = state
        forActivity.stopList = nil
        forActivity.tileImages = nil
        Task {
            do {
                try await self.assign(forActivity)
                call.resolve()
            } catch {
                call.reject("Live Activity: \(error.localizedDescription)")
            }
        }
        #else
        call.reject("ActivityKit fehlt")
        #endif
    }

    /// The UI reports which vehicle a recording would start with.
    @objc func ready(_ call: CAPPluginCall) {
        JoltDisplayStore.shared.setVehicle(call.getString("vehicle"))
        call.resolve()
    }

    /// The result of an action from CarPlay: ok, and on an error the reason.
    @objc func actionResult(_ call: CAPPluginCall) {
        JoltDisplayStore.shared.reportResult(
            action: call.getString("action") ?? "",
            ok: call.getBool("ok") ?? false,
            text: call.getString("text") ?? "")
        call.resolve()
    }

    @objc func finish(_ call: CAPPluginCall) {
        JoltDisplayStore.shared.assign(nil)
        #if canImport(ActivityKit)
        guard #available(iOS 16.2, *) else {
            call.resolve()
            return
        }
        Task {
            for active in Activity<JoltTripAttributes>.activities {
                await active.end(nil, dismissalPolicy: .immediate)
            }
            call.resolve()
        }
        #else
        call.resolve()
        #endif
    }

    #if canImport(ActivityKit)
    @available(iOS 16.2, *)
    private func assign(_ state: JoltDisplay) async throws {
        let contents = ActivityContent(
            state: state,
            staleDate: Date().addingTimeInterval(freshnessSeconds))

        // A running display is updated; there is never more than one, not
        // even after the UI reloads.
        let present = Activity<JoltTripAttributes>.activities
        if let first_item = present.first {
            await first_item.update(contents)
            for further in present.dropFirst() {
                await further.end(nil, dismissalPolicy: .immediate)
            }
            return
        }

        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            throw NSError(domain: "jolt", code: 1, userInfo: [
                NSLocalizedDescriptionKey:
                    "Live-Aktivitäten sind in den iPhone-Einstellungen ausgeschaltet."])
        }
        _ = try Activity.request(
            attributes: JoltTripAttributes(name: "jolt"),
            content: contents,
            pushType: nil)
    }
    #endif
}
