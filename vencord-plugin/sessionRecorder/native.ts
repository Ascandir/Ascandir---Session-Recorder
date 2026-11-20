/*
 * Ascandir SessionRecorder – Vencord-Plugin (Hauptprozess-Teil)
 * Schreibt die Einzelspuren der Discord-Teilnehmer als rohes PCM (s16le, mono) in den
 * Aufnahmeordner, den der SessionRecorder vorgibt. Kommuniziert nur mit 127.0.0.1.
 */

import { IpcMainInvokeEvent } from "electron";
import { appendFileSync, closeSync, existsSync, mkdirSync, openSync, writeFileSync, writeSync } from "fs";
import { isAbsolute, join, normalize } from "path";

const fds = new Map<string, number>();
let allowedDir: string | null = null;

const safeName = (n: string) => /^[\w.-]{1,80}$/.test(n);

function inDir(dir: string, name: string) {
    if (!allowedDir || normalize(dir) !== allowedDir || !safeName(name)) return null;
    return join(allowedDir, name);
}

/** Fragt den SessionRecorder (lokal) nach dem Aufnahmestatus. */
export async function status(_: IpcMainInvokeEvent, port: number) {
    try {
        const r = await fetch(`http://127.0.0.1:${port}/status`, { signal: AbortSignal.timeout(900) });
        if (!r.ok) return null;
        const s = await r.json();
        if (s?.recording && typeof s.dir === "string" && isAbsolute(s.dir)) {
            const d = normalize(s.dir);
            if (allowedDir !== d) closeAll(_);
            allowedDir = d;
            if (!existsSync(d)) mkdirSync(d, { recursive: true });
        }
        return s;
    } catch {
        return null;
    }
}

/** Schreibt PCM-Daten an die richtige Stelle der Spurdatei (Lücken bleiben Stille). */
export function write(_: IpcMainInvokeEvent, dir: string, file: string, byteOffset: number, data: Uint8Array) {
    const p = inDir(dir, file);
    if (!p || byteOffset < 0) return false;
    let fd = fds.get(p);
    if (fd === undefined) {
        fd = openSync(p, existsSync(p) ? "r+" : "w+");
        fds.set(p, fd);
    }
    const buf = Buffer.from(data.buffer, data.byteOffset, data.byteLength);
    writeSync(fd, buf, 0, buf.length, byteOffset);
    return true;
}

export function writeMeta(_: IpcMainInvokeEvent, dir: string, json: string) {
    const p = inDir(dir, "spuren.json");
    if (!p) return false;
    writeFileSync(p, json, "utf-8");
    return true;
}

export function log(_: IpcMainInvokeEvent, dir: string, line: string) {
    const p = inDir(dir, "plugin.log");
    if (p) appendFileSync(p, `[${new Date().toISOString()}] ${line}\n`, "utf-8");
}

export function closeAll(_?: IpcMainInvokeEvent) {
    for (const fd of fds.values()) {
        try { closeSync(fd); } catch { }
    }
    fds.clear();
}
