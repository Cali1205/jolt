# Hook the widget target (Live Activity) into the generated Xcode project.
#
# `cap add ios` only knows the app target. A Live Activity needs a widget
# extension, though - a target of its own with its own bundle identifier,
# embedded into the app. That cannot be expressed as a Swift package, and the
# project is not checked in - so after `cap add` it is added with the Ruby
# tool `xcodeproj` (the same one CocoaPods uses).
#
# Invocation (via tools/ios_widget.sh, which copies the sources in first):
#
#     ruby tools/ios_widget.rb ios/App/App.xcodeproj de.thesmarthome.jolt TEAMID
#
# The sources are then already in ios/App/JoltWidget/.
require "xcodeproj"

path, app_id, team = ARGV
abort "Usage: ios_widget.rb PROJECT APP_ID [TEAM]" if path.nil? || app_id.nil?

NAME = "JoltWidget"
MIN_IOS = "18.0"   # supplementalActivityFamilies (CarPlay) exists from iOS 18

project = Xcodeproj::Project.open(path)
app = project.targets.find { |t| t.name == "App" }
abort "Target 'App' not found - the Capacitor template has changed." unless app

if project.targets.any? { |t| t.name == NAME }
  puts "Target #{NAME} already exists - nothing to do."
  exit 0
end

folder = File.join(File.dirname(path), NAME)
%w[Info.plist JoltWidget.swift JoltTripAttributes.swift].each do |file|
  abort "Missing: #{File.join(folder, file)}" unless File.exist?(File.join(folder, file))
end

# Take over the app's version: App Store Connect rejects an extension with a
# different version number.
app_settings = app.build_configurations.first.build_settings
version = app_settings["MARKETING_VERSION"] || "1.0"
build = app_settings["CURRENT_PROJECT_VERSION"] || "1"

target = project.new_target(:app_extension, NAME, :ios, MIN_IOS, nil, :swift)

group = project.main_group.new_group(NAME, NAME)
sources = %w[JoltWidget.swift JoltTripAttributes.swift].map { |d| group.new_file(d) }
group.new_file("Info.plist")
target.add_file_references(sources)

%w[WidgetKit SwiftUI ActivityKit].each { |f| target.add_system_framework(f) }

target.build_configurations.each do |k|
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
  s["CURRENT_PROJECT_VERSION"] = build
  s["CODE_SIGN_STYLE"] = "Automatic"
  s["DEVELOPMENT_TEAM"] = team if team && !team.empty?
  s["LD_RUNPATHS_SEARCH_PATHS"] =
    "$(inherited) @executable_path/Frameworks @executable_path/../../Frameworks"
end

# Only the extension is set to iOS 18; the app stays at its minimum version.
# Raising the app made `cap sync` write a Package.swift with `.iOS(.v18)`,
# which the package manager (tools-version 5.9) rejects - the first attempt
# failed because of that.

embed = app.new_copy_files_build_phase("Embed Foundation Extensions")
embed.dst_subfolder_spec = "13"   # PlugIns
file = embed.add_file_reference(target.product_reference, true)
file.settings = { "ATTRIBUTES" => ["RemoveHeadersOnCopy"] }
app.add_dependency(target)

project.save
puts "Widget target #{NAME} hooked in (#{app_id}.widget, from iOS #{MIN_IOS})."
