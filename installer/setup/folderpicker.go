package main

// Moderner Windows-Ordnerdialog (wie im Explorer) – freie Navigation auf alle Laufwerke.

import (
	"syscall"
	"unsafe"
)

type guid struct {
	d1     uint32
	d2, d3 uint16
	d4     [8]byte
}

var (
	clsidFileOpenDialog = guid{0xDC1C5A9C, 0xE88A, 0x4DDE, [8]byte{0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7}}
	iidIFileOpenDialog  = guid{0xD57C7288, 0xD4AD, 0x4768, [8]byte{0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60}}
	iidIShellItem       = guid{0x43826D1E, 0xE718, 0x42EE, [8]byte{0xBC, 0x55, 0xA1, 0xE2, 0x61, 0xC3, 0x7B, 0xFE}}

	ole32   = syscall.NewLazyDLL("ole32.dll")
	shell32 = syscall.NewLazyDLL("shell32.dll")
)

const (
	fosPickFolders     = 0x20
	fosForceFileSystem = 0x40
	fosPathMustExist   = 0x800
	sigdnFileSysPath   = 0x80058000
)

// vtable-Aufruf: obj ist ein COM-Interface-Zeiger
func vcall(obj uintptr, index int, args ...uintptr) uintptr {
	vtbl := *(*uintptr)(unsafe.Pointer(obj))
	fn := *(*uintptr)(unsafe.Pointer(vtbl + uintptr(index)*unsafe.Sizeof(uintptr(0))))
	all := append([]uintptr{obj}, args...)
	r, _, _ := syscall.SyscallN(fn, all...)
	return r
}

func release(obj uintptr) {
	if obj != 0 {
		vcall(obj, 2)
	}
}

// pickFolder zeigt den Explorer-Ordnerdialog. ok=false bei Abbruch; err!=nil wenn der Dialog nicht verfügbar ist.
func pickFolder(owner uintptr, title, start string) (path string, ok bool, failed bool) {
	ole32.NewProc("CoInitializeEx").Call(0, 2) // STA; S_FALSE wenn schon initialisiert
	var dlg uintptr
	hr, _, _ := ole32.NewProc("CoCreateInstance").Call(
		uintptr(unsafe.Pointer(&clsidFileOpenDialog)), 0, 1|4, // CLSCTX_INPROC_SERVER|LOCAL_SERVER
		uintptr(unsafe.Pointer(&iidIFileOpenDialog)), uintptr(unsafe.Pointer(&dlg)))
	if int32(hr) < 0 || dlg == 0 {
		return "", false, true
	}
	defer release(dlg)

	// GetOptions(10) / SetOptions(9)
	var opts uint32
	vcall(dlg, 10, uintptr(unsafe.Pointer(&opts)))
	vcall(dlg, 9, uintptr(opts|fosPickFolders|fosForceFileSystem|fosPathMustExist))

	if t, err := syscall.UTF16PtrFromString(title); err == nil {
		vcall(dlg, 17, uintptr(unsafe.Pointer(t))) // SetTitle
	}
	if l, err := syscall.UTF16PtrFromString("Ordner auswählen"); err == nil {
		vcall(dlg, 18, uintptr(unsafe.Pointer(l))) // SetOkButtonLabel
	}
	// Startordner vorwählen (nur Vorauswahl, man kann überall hin navigieren)
	for dir := start; dir != ""; {
		p, _ := syscall.UTF16PtrFromString(dir)
		var item uintptr
		r, _, _ := shell32.NewProc("SHCreateItemFromParsingName").Call(
			uintptr(unsafe.Pointer(p)), 0, uintptr(unsafe.Pointer(&iidIShellItem)), uintptr(unsafe.Pointer(&item)))
		if int32(r) >= 0 && item != 0 {
			vcall(dlg, 12, item) // SetFolder
			release(item)
			break
		}
		parent := parentDir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}

	if hr := vcall(dlg, 3, owner); int32(hr) < 0 { // Show; Abbruch = HRESULT_FROM_WIN32(ERROR_CANCELLED)
		return "", false, false
	}
	var res uintptr
	if hr := vcall(dlg, 20, uintptr(unsafe.Pointer(&res))); int32(hr) < 0 || res == 0 { // GetResult
		return "", false, false
	}
	defer release(res)
	var psz *uint16
	if hr := vcall(res, 5, sigdnFileSysPath, uintptr(unsafe.Pointer(&psz))); int32(hr) < 0 || psz == nil { // GetDisplayName
		return "", false, false
	}
	path = syscall.UTF16ToString((*[32768]uint16)(unsafe.Pointer(psz))[:])
	ole32.NewProc("CoTaskMemFree").Call(uintptr(unsafe.Pointer(psz)))
	return path, true, false
}

func parentDir(p string) string {
	for i := len(p) - 1; i > 0; i-- {
		if p[i] == '\\' {
			if i == 2 && p[1] == ':' {
				return p[:3]
			}
			return p[:i]
		}
	}
	return p
}
