#!/bin/bash
# 用法：
#   IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#   PROFILE="clashlink-notary" ./tools/sign_and_notarize.sh
# 依赖：已安装 Apple Developer ID 证书，且执行过
#   xcrun notarytool store-credentials clashlink-notary --apple-id ... --team-id ... --password ...
set -euo pipefail
APP="dist/Clash Link.app"
IDENTITY="${IDENTITY:?请设置 IDENTITY，例如 'Developer ID Application: Name (TEAMID)'}"
PROFILE="${PROFILE:-clashlink-notary}"
ZIP="dist/ClashLink-notarize.zip"

codesign --deep --force --options runtime --timestamp --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
xcrun stapler staple "$APP"
spctl -a -vvv -t exec "$APP"
echo "完成：已签名并公证 $APP"
