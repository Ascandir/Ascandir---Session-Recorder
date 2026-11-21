#!/usr/bin/env bash
# Baut den Windows-Setup-Assistenten Ascandir-SessionRecorder-Setup.exe (Cross-Compile mit Go).
# Das eigentliche Programm (PyInstaller) lädt das Setup bei der Installation aus dem Release.
# Benötigt: Go >= 1.24, go-winres (go install github.com/tc-hib/go-winres@latest)
set -euo pipefail
cd "$(dirname "$0")"
VERSION=$(cat app/VERSION)
WINRES=${WINRES:-go-winres}
export GOFLAGS=-mod=mod

P=installer/setup/payload
rm -rf "$P"; mkdir -p "$P"
cp ANLEITUNG.html assets/icon.ico "$P/"

( cd installer/setup
  sed -i.bak "s/^const appVersion = .*/const appVersion = \"$VERSION\"/" install.go && rm -f install.go.bak
  $WINRES simply --icon ../../assets/icon.png --manifest gui --product-name "Ascandir - SessionRecorder Setup" \
     --file-description "Ascandir - SessionRecorder Setup" --product-version "$VERSION" --file-version "$VERSION" >/dev/null
  GOOS=windows GOARCH=amd64 go build -ldflags "-H windowsgui -s -w" -o ../../Ascandir-SessionRecorder-Setup.exe . )
echo "Fertig: Ascandir-SessionRecorder-Setup.exe (Version $VERSION)"
