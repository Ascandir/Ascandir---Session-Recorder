// SessionRecorder.exe – startet das Tool ohne Konsolenfenster und öffnet es im Browser.
package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"unsafe"
)

func msg(text string) {
	user32 := syscall.NewLazyDLL("user32.dll")
	box := user32.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString("Ascandir - SessionRecorder")
	box.Call(0, uintptr(unsafe.Pointer(t)), uintptr(unsafe.Pointer(c)), 0x40)
}

func main() {
	exe, _ := os.Executable()
	dir := filepath.Dir(exe)
	py := filepath.Join(dir, "venv", "Scripts", "python.exe")
	if _, err := os.Stat(py); err != nil {
		msg("Der SessionRecorder ist noch nicht fertig installiert.\nBitte das Setup nochmal ausführen.")
		return
	}
	os.MkdirAll(filepath.Join(dir, "daten"), 0o755)
	logf, _ := os.OpenFile(filepath.Join(dir, "daten", "log.txt"),
		os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	cmd := exec.Command(py, filepath.Join(dir, "app", "main.py"))
	cmd.Dir = dir
	cmd.Stdout = logf
	cmd.Stderr = logf
	cmd.Env = append(os.Environ(), "PYTHONUTF8=1", "PYTHONIOENCODING=utf-8")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	if err := cmd.Start(); err != nil {
		msg("Start fehlgeschlagen: " + err.Error())
	}
}
