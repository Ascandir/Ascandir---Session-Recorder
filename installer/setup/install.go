package main

// Die eigentliche Installation – unabhängig vom Fenster.

import (
	"archive/zip"
	"bufio"
	"bytes"
	"embed"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

//go:embed all:payload
var payload embed.FS

const appVersion = "1.3.0"

const (
	uvURL   = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
	vcURL   = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
	regKey  = `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\DnD-Chronist`
	appName = "Ascandir - SessionRecorder"
)

type Options struct {
	Dir       string
	Desktop   bool
	StartMenu bool
}

type UI interface {
	Step(n, total int, text string)
	Log(line string)
}

var (
	curMu  sync.Mutex
	curCmd *exec.Cmd
	cancel bool
)

func cancelInstall() {
	curMu.Lock()
	defer curMu.Unlock()
	cancel = true
	if curCmd != nil && curCmd.Process != nil {
		curCmd.Process.Kill()
	}
}

func hidden(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
}

// splitLines trennt an \n und \r (Fortschrittsanzeigen)
func splitLines(data []byte, atEOF bool) (int, []byte, error) {
	if i := bytes.IndexAny(data, "\r\n"); i >= 0 {
		return i + 1, data[:i], nil
	}
	if atEOF && len(data) > 0 {
		return len(data), data, nil
	}
	return 0, nil, nil
}

func runLogged(ui UI, dir string, env []string, name string, args ...string) error {
	if cancel {
		return fmt.Errorf("abgebrochen")
	}
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), env...)
	hidden(cmd)
	pr, pw := io.Pipe()
	cmd.Stdout = pw
	cmd.Stderr = pw
	curMu.Lock()
	curCmd = cmd
	curMu.Unlock()
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan struct{})
	go func() {
		sc := bufio.NewScanner(pr)
		sc.Buffer(make([]byte, 64*1024), 1024*1024)
		sc.Split(splitLines)
		for sc.Scan() {
			if t := strings.TrimSpace(sc.Text()); t != "" {
				ui.Log(t)
			}
		}
		close(done)
	}()
	err := cmd.Wait()
	pw.Close()
	<-done
	curMu.Lock()
	curCmd = nil
	curMu.Unlock()
	if cancel {
		return fmt.Errorf("abgebrochen")
	}
	return err
}

func download(ui UI, url, dest string) error {
	ui.Log("Lade " + url)
	c := &http.Client{Timeout: 30 * time.Minute}
	r, err := c.Get(url)
	if err != nil {
		return err
	}
	defer r.Body.Close()
	if r.StatusCode != 200 {
		return fmt.Errorf("Download fehlgeschlagen (HTTP %d)", r.StatusCode)
	}
	f, err := os.Create(dest + ".part")
	if err != nil {
		return err
	}
	_, err = io.Copy(f, r.Body)
	f.Close()
	if err != nil {
		return err
	}
	return os.Rename(dest+".part", dest)
}

func unzipOne(zipPath, name, dest string) error {
	z, err := zip.OpenReader(zipPath)
	if err != nil {
		return err
	}
	defer z.Close()
	for _, f := range z.File {
		if strings.EqualFold(filepath.Base(f.Name), name) {
			rc, err := f.Open()
			if err != nil {
				return err
			}
			defer rc.Close()
			out, err := os.Create(dest)
			if err != nil {
				return err
			}
			defer out.Close()
			_, err = io.Copy(out, rc)
			return err
		}
	}
	return fmt.Errorf("%s nicht im Archiv gefunden", name)
}

func writePayload(root string) error {
	os.RemoveAll(filepath.Join(root, "app")) // alter Programmcode; Daten bleiben
	os.Remove(filepath.Join(root, "DnD-Chronist.exe"))
	os.Remove(filepath.Join(root, "DnD-Chronist-Setup.exe"))
	return fs.WalkDir(payload, "payload", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel := strings.TrimPrefix(strings.TrimPrefix(p, "payload"), "/")
		if rel == "" {
			return nil
		}
		target := filepath.Join(root, filepath.FromSlash(rel))
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		data, err := payload.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, 0o644)
	})
}

func powershell(script string) error {
	cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script)
	hidden(cmd)
	return cmd.Run()
}

func psQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

func makeShortcuts(root string, desktop, start bool) error {
	var places []string
	if desktop {
		places = append(places, "[Environment]::GetFolderPath('Desktop')")
	}
	if start {
		places = append(places, "[Environment]::GetFolderPath('Programs')")
	}
	if len(places) == 0 {
		return nil
	}
	script := fmt.Sprintf(`$w = New-Object -ComObject WScript.Shell
foreach ($p in @(%s)) {
  $s = $w.CreateShortcut((Join-Path $p 'Ascandir - SessionRecorder.lnk'))
  $s.TargetPath = %s; $s.WorkingDirectory = %s; $s.IconLocation = %s
  $s.Description = 'D&D-Sessions aufnehmen und zusammenfassen'; $s.Save()
}`, strings.Join(places, ","), psQuote(filepath.Join(root, "SessionRecorder.exe")),
		psQuote(root), psQuote(filepath.Join(root, "icon.ico")))
	return powershell(script)
}

func removeShortcuts() {
	powershell(`foreach ($n in 'Ascandir - SessionRecorder.lnk','DnD-Chronist.lnk') { foreach ($p in [Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs')) { Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $p $n) } }`)
}

// shortcutExists: gibt es (alte oder neue) Verknüpfungen? Für Updates ohne Nachfragen.
func shortcutExists(folder string) bool {
	cmd := exec.Command("powershell", "-NoProfile", "-Command",
		"$p=[Environment]::GetFolderPath('"+folder+"'); (Test-Path (Join-Path $p 'Ascandir - SessionRecorder.lnk')) -or (Test-Path (Join-Path $p 'DnD-Chronist.lnk'))")
	hidden(cmd)
	out, _ := cmd.Output()
	return strings.Contains(strings.ToLower(string(out)), "true")
}

func reg(args ...string) {
	cmd := exec.Command("reg", args...)
	hidden(cmd)
	cmd.Run()
}

func previousInstall() string {
	cmd := exec.Command("reg", "query", regKey, "/v", "InstallLocation")
	hidden(cmd)
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(out), "\n") {
		if i := strings.Index(line, "REG_SZ"); i >= 0 {
			return strings.TrimSpace(line[i+6:])
		}
	}
	return ""
}

func vcRedistMissing() bool {
	win := os.Getenv("WINDIR")
	if win == "" {
		win = `C:\Windows`
	}
	_, err := os.Stat(filepath.Join(win, "System32", "msvcp140.dll"))
	return err != nil
}

func appRunning() bool {
	c := &http.Client{Timeout: 800 * time.Millisecond}
	r, err := c.Get("http://127.0.0.1:7860/")
	if err != nil {
		return false
	}
	r.Body.Close()
	return true
}

// writable prüft, ob wir in den Ordner schreiben dürfen (ohne Admin).
func writable(dir string) bool {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return false
	}
	f := filepath.Join(dir, ".schreibtest")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		return false
	}
	os.Remove(f)
	return true
}

func freeGB(dir string) float64 {
	vol := filepath.VolumeName(dir) + `\`
	p, err := syscall.UTF16PtrFromString(vol)
	if err != nil {
		return -1
	}
	var free uint64
	k := syscall.NewLazyDLL("kernel32.dll").NewProc("GetDiskFreeSpaceExW")
	r, _, _ := k.Call(uintptr(unsafe.Pointer(p)), uintptr(unsafe.Pointer(&free)), 0, 0)
	if r == 0 {
		return -1
	}
	return float64(free) / 1e9
}

func Install(o Options, ui UI) error {
	root := o.Dir
	const total = 6
	env := []string{
		"UV_PYTHON_INSTALL_DIR=" + filepath.Join(root, "python"),
		"UV_CACHE_DIR=" + filepath.Join(root, "cache"),
		"PIP_CACHE_DIR=" + filepath.Join(root, "cache", "pip"),
		"TEMP=" + filepath.Join(root, "daten", "temp"),
		"TMP=" + filepath.Join(root, "daten", "temp"),
		"UV_NO_PROGRESS=1",
		"PYTHONUTF8=1",
		"PYTHONIOENCODING=utf-8",
	}

	ui.Step(1, total, "Programmdateien werden kopiert …")
	if err := os.MkdirAll(filepath.Join(root, "daten", "temp"), 0o755); err != nil {
		return err
	}
	if err := writePayload(root); err != nil {
		return err
	}
	if self, err := os.Executable(); err == nil {
		dst := filepath.Join(root, "SessionRecorder-Setup.exe")
		if !strings.EqualFold(self, dst) {
			if data, err := os.ReadFile(self); err == nil {
				os.WriteFile(dst, data, 0o755)
			}
		}
	}

	ui.Step(2, total, "Microsoft Visual C++ Laufzeit wird geprüft …")
	if vcRedistMissing() {
		ui.Log("Visual C++ Laufzeit fehlt – wird installiert (Windows fragt evtl. nach Erlaubnis).")
		vc := filepath.Join(os.TempDir(), "vc_redist.x64.exe")
		if err := download(ui, vcURL, vc); err != nil {
			return err
		}
		runLogged(ui, "", nil, vc, "/install", "/quiet", "/norestart")
		os.Remove(vc)
	} else {
		ui.Log("Visual C++ Laufzeit ist vorhanden.")
	}

	ui.Step(3, total, "Python wird eingerichtet …")
	tools := filepath.Join(root, "tools")
	os.MkdirAll(tools, 0o755)
	uv := filepath.Join(tools, "uv.exe")
	zp := filepath.Join(tools, "uv.zip")
	if err := download(ui, uvURL, zp); err != nil {
		return err
	}
	if err := unzipOne(zp, "uv.exe", uv); err != nil {
		return err
	}
	os.Remove(zp)
	py := filepath.Join(root, "venv", "Scripts", "python.exe")
	if _, err := os.Stat(py); err != nil {
		if err := runLogged(ui, root, env, uv, "venv", "--python", "3.12", "venv"); err != nil {
			return fmt.Errorf("Python einrichten: %w", err)
		}
	}

	ui.Step(4, total, "Programmteile werden installiert …")
	if err := runLogged(ui, root, env, uv, "pip", "install", "--python", py, "-r",
		filepath.Join(root, "app", "requirements.txt")); err != nil {
		return fmt.Errorf("Programmteile installieren: %w", err)
	}

	ui.Step(5, total, "KI-Modelle werden geladen (das dauert am längsten) …")
	if err := runLogged(ui, root, env, py, filepath.Join(root, "app", "main.py"), "--modelle"); err != nil {
		return fmt.Errorf("KI-Modelle laden: %w", err)
	}
	runLogged(ui, root, env, uv, "cache", "clean")

	ui.Step(6, total, "Verknüpfungen werden angelegt …")
	removeShortcuts()
	if err := makeShortcuts(root, o.Desktop, o.StartMenu); err != nil {
		ui.Log("Verknüpfung konnte nicht angelegt werden: " + err.Error())
	}
	setup := filepath.Join(root, "SessionRecorder-Setup.exe")
	reg("add", regKey, "/v", "DisplayName", "/d", appName, "/f")
	reg("add", regKey, "/v", "DisplayVersion", "/d", appVersion, "/f")
	reg("add", regKey, "/v", "InstallLocation", "/d", root, "/f")
	reg("add", regKey, "/v", "UninstallString", "/d", `"`+setup+`" /uninstall`, "/f")
	reg("add", regKey, "/v", "DisplayIcon", "/d", filepath.Join(root, "icon.ico"), "/f")
	reg("add", regKey, "/v", "Publisher", "/d", "Ascandir", "/f")
	reg("add", regKey, "/v", "NoModify", "/t", "REG_DWORD", "/d", "1", "/f")
	ui.Log("Fertig.")
	return nil
}

func Uninstall(root string, deleteData bool) {
	removeShortcuts()
	reg("delete", regKey, "/f")
	for _, d := range []string{"app", "venv", "python", "tools", "cache"} {
		os.RemoveAll(filepath.Join(root, d))
	}
	for _, f := range []string{"SessionRecorder.exe", "DnD-Chronist.exe", "ANLEITUNG.html", "icon.ico"} {
		os.Remove(filepath.Join(root, f))
	}
	os.RemoveAll(filepath.Join(root, "daten", "modelle"))
	var c *exec.Cmd
	if deleteData {
		os.RemoveAll(filepath.Join(root, "daten"))
		os.RemoveAll(filepath.Join(root, "Sessions"))
		os.RemoveAll(filepath.Join(root, "Aufnahmen"))
		c = exec.Command("cmd", "/c", `timeout /t 2 >nul & rmdir /s /q "`+root+`"`)
	} else {
		c = exec.Command("cmd", "/c", `timeout /t 2 >nul & del /q "`+filepath.Join(root, "SessionRecorder-Setup.exe")+`"`)
	}
	hidden(c)
	c.Start()
}
