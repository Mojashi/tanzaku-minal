#!/bin/sh
# Render icon/strip.svg into the launcher app bundle's icon (macOS).
#
# ./dev.sh build recreates the bundle and restores the stock icon, so run this
# again afterwards. Needs rsvg-convert (brew install librsvg).
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
APP="$REPO/kitty/launcher/kitty.app"

if [ ! -d "$APP" ]; then
    echo "no app bundle at $APP -- run ./dev.sh build first" >&2
    exit 1
fi

rm -rf "$HERE/strip.iconset"
mkdir -p "$HERE/strip.iconset"
for sz in 16 32 64 128 256 512 1024; do
    rsvg-convert -w $sz -h $sz "$HERE/strip.svg" -o "$HERE/r$sz.png"
done

cd "$HERE"
cp r16.png  strip.iconset/icon_16x16.png;    cp r32.png   strip.iconset/icon_16x16@2x.png
cp r32.png  strip.iconset/icon_32x32.png;    cp r64.png   strip.iconset/icon_32x32@2x.png
cp r128.png strip.iconset/icon_128x128.png;  cp r256.png  strip.iconset/icon_128x128@2x.png
cp r256.png strip.iconset/icon_256x256.png;  cp r512.png  strip.iconset/icon_256x256@2x.png
cp r512.png strip.iconset/icon_512x512.png;  cp r1024.png strip.iconset/icon_512x512@2x.png
iconutil -c icns strip.iconset -o strip.icns

# Keep a copy of the stock icon the first time around.
[ -f "$HERE/kitty-original.icns" ] || cp "$APP/Contents/Resources/kitty.icns" "$HERE/kitty-original.icns"
cp strip.icns "$APP/Contents/Resources/kitty.icns"

# macOS prioritizes Assets.car (and CFBundleIconName in Info.plist) over kitty.icns.
# Remove Assets.car and CFBundleIconName so macOS uses kitty.icns.
[ -f "$APP/Contents/Resources/Assets.car" ] && rm -f "$APP/Contents/Resources/Assets.car"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$APP/Contents/Info.plist" 2>/dev/null || true

# macOS caches icons hard; touch the bundle, re-register with LaunchServices, and clear cache.
touch "$APP" "$APP/Contents/Info.plist"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true
rm -rf /private/var/folders/*/*/*/com.apple.dock.iconcache 2>/dev/null || true
rm -rf /private/var/folders/*/*/*/com.apple.iconservices* 2>/dev/null || true
killall Dock 2>/dev/null || true

rm -f r16.png r32.png r64.png r128.png r256.png r512.png r1024.png
rm -rf "$HERE/strip.iconset"
echo "applied to $APP"
echo "to restore: cp '$HERE/kitty-original.icns' '$APP/Contents/Resources/kitty.icns'"
