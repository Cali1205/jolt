# Das Widget-Ziel (Live Activity) in das erzeugte Xcode-Projekt einhängen.
#
# `cap add ios` kennt nur das App-Ziel. Eine Live Activity braucht aber eine
# Widget-Erweiterung, also ein eigenes Ziel mit eigener Bundle-Kennung, das in
# die App eingebettet wird. Das lässt sich nicht als Swift-Paket abbilden, und
# das Projekt ist nicht eingecheckt - deshalb wird es nach `cap add` mit dem
# Ruby-Werkzeug `xcodeproj` (dasselbe, das CocoaPods benutzt) ergänzt.
#
# Aufruf (über tools/ios_widget.sh, das vorher die Quellen hinkopiert):
#
#     ruby tools/ios_widget.rb ios/App/App.xcodeproj de.thesmarthome.jolt TEAMID
#
# Die Quellen liegen dann schon in ios/App/JoltWidget/.
require "xcodeproj"

pfad, app_id, team = ARGV
abort "Aufruf: ios_widget.rb PROJEKT APP_ID [TEAM]" if pfad.nil? || app_id.nil?

NAME = "JoltWidget"
MIN_IOS = "18.0"   # supplementalActivityFamilies (CarPlay) gibt es ab iOS 18

projekt = Xcodeproj::Project.open(pfad)
app = projekt.targets.find { |t| t.name == "App" }
abort "Ziel 'App' nicht gefunden - die Capacitor-Vorlage hat sich geaendert." unless app

if projekt.targets.any? { |t| t.name == NAME }
  puts "Ziel #{NAME} gibt es schon - nichts zu tun."
  exit 0
end

ordner = File.join(File.dirname(pfad), NAME)
%w[Info.plist JoltWidget.swift JoltFahrtAttributes.swift].each do |datei|
  abort "Fehlt: #{File.join(ordner, datei)}" unless File.exist?(File.join(ordner, datei))
end

# Die Fassung der App uebernehmen: Eine Erweiterung mit anderer Versions-
# nummer lehnt App Store Connect ab.
app_einstellungen = app.build_configurations.first.build_settings
version = app_einstellungen["MARKETING_VERSION"] || "1.0"
bau = app_einstellungen["CURRENT_PROJECT_VERSION"] || "1"

ziel = projekt.new_target(:app_extension, NAME, :ios, MIN_IOS, nil, :swift)

gruppe = projekt.main_group.new_group(NAME, NAME)
quellen = %w[JoltWidget.swift JoltFahrtAttributes.swift].map { |d| gruppe.new_file(d) }
gruppe.new_file("Info.plist")
ziel.add_file_references(quellen)

%w[WidgetKit SwiftUI ActivityKit].each { |f| ziel.add_system_framework(f) }

ziel.build_configurations.each do |k|
  s = k.build_settings
  s["PRODUCT_BUNDLE_IDENTIFIER"] = "#{app_id}.widget"
  s["PRODUCT_NAME"] = "$(TARGET_NAME)"
  s["INFOPLIST_FILE"] = "#{NAME}/Info.plist"
  s["GENERATE_INFOPLIST_FILE"] = "NO"
  s["SWIFT_VERSION"] = "5.0"
  s["TARGETED_DEVICE_FAMILY"] = "1"
  s["IPHONEOS_DEPLOYMENT_TARGET"] = MIN_IOS
  s["SKIP_INSTALL"] = "YES"
  s["MARKETING_VERSION"] = version
  s["CURRENT_PROJECT_VERSION"] = bau
  s["CODE_SIGN_STYLE"] = "Automatic"
  s["DEVELOPMENT_TEAM"] = team if team && !team.empty?
  s["LD_RUNPATHS_SEARCH_PATHS"] =
    "$(inherited) @executable_path/Frameworks @executable_path/../../Frameworks"
end

# Die App selbst muss mindestens so neu sein wie die Erweiterung, die sie
# einbettet - und soll mit der Live-Activity-Faehigkeit gekennzeichnet sein.
([projekt] + [app]).each do |t|
  t.build_configurations.each do |k|
    k.build_settings["IPHONEOS_DEPLOYMENT_TARGET"] = MIN_IOS
  end
end

einbetten = app.new_copy_files_build_phase("Embed Foundation Extensions")
einbetten.dst_subfolder_spec = "13"   # PlugIns
datei = einbetten.add_file_reference(ziel.product_reference, true)
datei.settings = { "ATTRIBUTES" => ["RemoveHeadersOnCopy"] }
app.add_dependency(ziel)

projekt.save
puts "Widget-Ziel #{NAME} eingehaengt (#{app_id}.widget, iOS #{MIN_IOS})."
