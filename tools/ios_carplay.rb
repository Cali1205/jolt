# Teil von tools/ios_carplay.sh: AppDelegate anpassen und, wenn verlangt, den
# CarPlay-Entitlement ins Projekt eintragen.
#
#     ruby tools/ios_carplay.rb ios/App true
#
# Beides bricht ab, statt still weiterzumachen, wenn die Vorlage nicht mehr so
# aussieht wie erwartet: Ein AppDelegate, der die CarPlay-Szene nicht
# kennt, sieht im Simulator gruen aus und zeigt im Auto nichts.
app = ARGV[0] || "ios/App"
entitlement = (ARGV[1] || "false") == "true"

# ---- 1. AppDelegate: CarPlay bekommt seine Konfiguration aus der Info.plist
pfad = File.join(app, "App", "AppDelegate.swift")
abort "AppDelegate.swift nicht gefunden: #{pfad}" unless File.exist?(pfad)
quelle = File.read(pfad)

marke = "jolt: CarPlay-Szene"
if quelle.include?(marke)
  puts "AppDelegate: schon angepasst."
else
  muster = /(configurationForConnecting\s+connectingSceneSession:\s*UISceneSession,\s*options:\s*UIScene\.ConnectionOptions\)\s*->\s*UISceneConfiguration\s*\{)/m
  abort "AppDelegate: configurationForConnecting nicht gefunden - die Capacitor-Vorlage hat sich geaendert (tools/ios_carplay.rb)." unless quelle =~ muster
  einschub = <<~SWIFT.gsub(/^/, "        ")

    // #{marke}: Die Szene fuer CarPlay hat ihren eigenen Delegate (Info.plist,
    // CPTemplateApplicationSceneSessionRoleApplication). Ohne diese Zeilen bekaeme
    // sie den Fenster-Delegate der Oberflaeche.
    if connectingSceneSession.role.rawValue == "CPTemplateApplicationSceneSessionRoleApplication" {
        return UISceneConfiguration(name: "CarPlay Configuration", sessionRole: connectingSceneSession.role)
    }
  SWIFT
  neu = quelle.sub(muster) { |m| m + "\n" + einschub.rstrip + "\n" }
  abort "AppDelegate: Anpassung hat nichts geaendert." if neu == quelle
  File.write(pfad, neu)
  puts "AppDelegate: CarPlay-Szene wird nicht mehr mit dem Fenster-Delegate bedient."
end

# ---- 2. Entitlement (nur auf Verlangen)
unless entitlement
  puts "CarPlay-Entitlement: nicht eingetragen (Schalter aus)."
  exit 0
end

require "xcodeproj"
datei = File.join(app, "App", "App.entitlements")
File.write(datei, <<~PLIST)
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
  	<key>com.apple.developer.carplay-charging</key>
  	<true/>
  </dict>
  </plist>
PLIST

projekt = Xcodeproj::Project.open(File.join(app, "App.xcodeproj"))
ziel = projekt.targets.find { |t| t.name == "App" }
abort "Ziel 'App' nicht gefunden." unless ziel
ziel.build_configurations.each do |k|
  k.build_settings["CODE_SIGN_ENTITLEMENTS"] = "App/App.entitlements"
end
gruppe = projekt.main_group.find_subpath("App", false)
gruppe.new_file("App.entitlements") if gruppe && gruppe.files.none? { |f| f.path == "App.entitlements" }
projekt.save
puts "CarPlay-Entitlement eingetragen: App/App.entitlements (com.apple.developer.carplay-charging)."
