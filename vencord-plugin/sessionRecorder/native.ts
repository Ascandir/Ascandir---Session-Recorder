/*
 * Ascandir SessionRecorder – Vencord-Plugin (Hauptprozess-Teil)
 * Schickt die Einzelspuren der Discord-Teilnehmer (rohes PCM, s16le, mono) an den SessionRecorder,
 * der sie in seinen Aufnahmeordner schreibt. Kommuniziert nur mit 127.0.0.1 – so funktioniert es auch,
 * wenn Vesktop in einer Sandbox (Flatpak) läuft und selbst nicht in den Aufnahmeordner schreiben darf.
 */

import { IpcMainInvokeEvent } from "electron";

let port = 7863;
const base = () => `http://127.0.0.1:${port}`;
const safeName = (n: string) => /^[\w.-]{1,80}$/.test(n);

/** Fragt den SessionRecorder (lokal) nach dem Aufnahmestatus und meldet, wer im Kanal ist. */
export async function status(_: IpcMainInvokeEvent, p: number, info?: string) {
    port = p || 7863;
    try {
        const r = await fetch(`${base()}/status`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: info ?? "{}",
            signal: AbortSignal.timeout(900),
        });
        if (!r.ok) return null;
        return await r.json();
    } catch {
        return null;
    }
}

/** PCM-Daten an die richtige Stelle der Spurdatei schreiben lassen (Lücken bleiben Stille). */
export async function write(_: IpcMainInvokeEvent, _dir: string, file: string, byteOffset: number, data: Uint8Array) {
    if (!safeName(file) || byteOffset < 0) return false;
    try {
        const r = await fetch(`${base()}/pcm?file=${encodeURIComponent(file)}&offset=${byteOffset}`, {
            method: "POST",
            headers: { "Content-Type": "application/octet-stream" },
            body: new Uint8Array(data),
            signal: AbortSignal.timeout(5000),
        });
        return r.ok;
    } catch {
        return false;
    }
}

export async function writeMeta(_: IpcMainInvokeEvent, _dir: string, json: string) {
    try {
        const r = await fetch(`${base()}/meta`, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: json,
            signal: AbortSignal.timeout(3000),
        });
        return r.ok;
    } catch {
        return false;
    }
}

export async function log(_: IpcMainInvokeEvent, _dir: string, line: string) {
    try {
        await fetch(`${base()}/log`, {
            method: "POST", headers: { "Content-Type": "text/plain; charset=utf-8" }, body: line,
            signal: AbortSignal.timeout(2000),
        });
    } catch { }
}

export function closeAll(_?: IpcMainInvokeEvent) { }
