import Foundation

/// The last display, for everything that is not the Live Activity - the
/// CarPlay list.
///
/// The display model is created in the UI's JavaScript and arrives here
/// via the plugin. The CarPlay scene runs in the same process but without
/// knowing the UI: it reads from here and gets notified when something
/// new arrives.
///
/// The state is persisted so that a CarPlay scene that starts before the
/// UI (iPhone locked, the car starts the app) does not begin empty. How
/// old it is is stated in the model (`as_of`); the display decides from
/// when it no longer takes it for full.
public final class JoltDisplayStore {
    public static let shared = JoltDisplayStore()

    private let keyname = "jolt.display.last"
    private let mutex = NSLock()
    private var observer: [UUID: (JoltDisplay?) -> Void] = [:]
    private var tail: JoltDisplay?

    // MARK: Actions from CarPlay (start and stop the recording)
    //
    // The UI (JavaScript) can start the recording, CarPlay cannot.
    // CarPlay therefore sends a request to the plugin, which passes it on to
    // the UI, and gets the result back from there.
    //
    // **It only works while the UI is running.** If the car starts the app
    // on its own (iPhone locked, nothing open), there is no UI: the plugin
    // is not loaded, `bridgeActive` stays false, and CarPlay honestly says
    // that jolt has to be opened once on the iPhone.
    public static let actionName = Notification.Name("JoltCarPlayAction")

    public private(set) var bridgeActive = false
    /// The vehicle the recording starts with (last used).
    public private(set) var vehicleName: String?
    private var resultObserver: [UUID: (String, Bool, String) -> Void] = [:]

    private init() {
        if let records = UserDefaults.std_default.data(forKey: keyname),
           let model = try? JSONDecoder().decode(JoltDisplay.self, from: records) {
            tail = model
        }
    }

    public var latest: JoltDisplay? {
        mutex.lock()
        defer { mutex.unlock() }
        return tail
    }

    /// `nil`: the trip is over, there is nothing left to show.
    public func assign(_ model: JoltDisplay?) {
        mutex.lock()
        tail = model
        let every = Array(observer.values)
        mutex.unlock()

        // The tile images are left out: they are almost 200 KB and arrive anew
        // every few seconds. The state is persisted only for the cold start,
        // and by three minutes it no longer counts anyway - until the UI next
        // reports, Swift draws on its own.
        var saveTo = model
        saveTo?.tileImages = nil
        if let safe = saveTo, let records = try? JSONEncoder().encode(safe) {
            UserDefaults.std_default.set(records, forKey: keyname)
        } else {
            UserDefaults.std_default.removeObject(forKey: keyname)
        }
        // CarPlay's templates may only be touched on the main thread.
        DispatchQueue.main.async {
            for atChange in every { atChange(model) }
        }
    }

    public func reportBridge(_ active: Bool) {
        mutex.lock()
        bridgeActive = active
        mutex.unlock()
    }

    public func setVehicle(_ name: String?) {
        mutex.lock()
        let empty = (name ?? "").trimmingCharacters(in: .whitespaces).isEmpty
        vehicleName = empty ? nil : name
        let every = Array(observer.values)
        let model = tail
        mutex.unlock()
        // The list shows the vehicle on the start button: rebuild it.
        DispatchQueue.main.async {
            for atChange in every { atChange(model) }
        }
    }

    /// Asks the UI for an action ("starten" or "beenden"). Returns false if
    /// there is no UI that can carry it out.
    public func requestAction(_ action: String) -> Bool {
        mutex.lock()
        let possible = bridgeActive
        mutex.unlock()
        guard possible else { return false }
        NotificationCenter.default.post(
            name: JoltDisplayStore.actionName, object: nil, userInfo: ["action": action])
        return true
    }

    public func reportResult(action: String, ok: Bool, text: String) {
        mutex.lock()
        let every = Array(resultObserver.values)
        mutex.unlock()
        DispatchQueue.main.async {
            for atResult in every { atResult(action, ok, text) }
        }
    }

    public func observeResult(_ atResult: @escaping (String, Bool, String) -> Void) -> UUID {
        let ident = UUID()
        mutex.lock()
        resultObserver[ident] = atResult
        mutex.unlock()
        return ident
    }

    public func stopObservingResult(_ ident: UUID) {
        mutex.lock()
        resultObserver.removeValue(forKey: ident)
        mutex.unlock()
    }

    public func observe(_ atChange: @escaping (JoltDisplay?) -> Void) -> UUID {
        let ident = UUID()
        mutex.lock()
        observer[ident] = atChange
        mutex.unlock()
        return ident
    }

    public func stopObserving(_ ident: UUID) {
        mutex.lock()
        observer.removeValue(forKey: ident)
        mutex.unlock()
    }
}
