# Part of tools/ios_carplay.sh: adjust the AppDelegate and, if requested,
# enter the CarPlay entitlement in the project.
#
#     ruby tools/ios_carplay.rb ios/App true
#
# Both abort instead of carrying on silently when the template no longer looks
# the way it is expected to: an AppDelegate that does not know the CarPlay
# scene looks green in the simulator and shows nothing in the car.
app = ARGV[0] || "ios/App"
entitlement = (ARGV[1] || "false") == "true"

# ---- 1. AppDelegate: CarPlay gets its configuration from the Info.plist
path = File.join(app, "App", "AppDelegate.swift")
abort "AppDelegate.swift not found: #{path}" unless File.exist?(path)
source = File.read(path)

marker = "jolt: CarPlay scene"
if source.include?(marker)
  puts "AppDelegate: already adjusted."
else
  pattern = /(configurationForConnecting\s+connectingSceneSession:\s*UISceneSession,\s*options:\s*UIScene\.ConnectionOptions\)\s*->\s*UISceneConfiguration\s*\{)/m
  abort "AppDelegate: configurationForConnecting not found - the Capacitor template has changed (tools/ios_carplay.rb)." unless source =~ pattern
  insertion = <<~SWIFT.gsub(/^/, "        ")

    // #{marker}: The CarPlay scene has its own delegate (Info.plist,
    // CPTemplateApplicationSceneSessionRoleApplication). Without these lines it
    // would get the UI's window delegate.
    if connectingSceneSession.role.rawValue == "CPTemplateApplicationSceneSessionRoleApplication" {
        return UISceneConfiguration(name: "CarPlay Configuration", sessionRole: connectingSceneSession.role)
    }
  SWIFT
  updated = source.sub(pattern) { |m| m + "\n" + insertion.rstrip + "\n" }
  abort "AppDelegate: the adjustment changed nothing." if updated == source
  File.write(path, updated)
  puts "AppDelegate: the CarPlay scene is no longer served by the window delegate."
end

# ---- 2. Entitlement (only on request)
unless entitlement
  puts "CarPlay entitlement: not entered (switch off)."
  exit 0
end

require "xcodeproj"
file = File.join(app, "App", "App.entitlements")
File.write(file, <<~PLIST)
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
  	<key>com.apple.developer.carplay-charging</key>
  	<true/>
  </dict>
  </plist>
PLIST

project = Xcodeproj::Project.open(File.join(app, "App.xcodeproj"))
target = project.targets.find { |t| t.name == "App" }
abort "Target 'App' not found." unless target
target.build_configurations.each do |k|
  k.build_settings["CODE_SIGN_ENTITLEMENTS"] = "App/App.entitlements"
end
group = project.main_group.find_subpath("App", false)
group.new_file("App.entitlements") if group && group.files.none? { |f| f.path == "App.entitlements" }
project.save
puts "CarPlay entitlement entered: App/App.entitlements (com.apple.developer.carplay-charging)."
