/*
 * Ascandir SessionRecorder – Vencord-Plugin
 *
 * Nimmt in Vesktop jeden Teilnehmer des Sprachkanals als eigene Spur auf, solange der
 * Ascandir SessionRecorder aufnimmt. Die Spuren landen direkt im Aufnahmeordner des
 * SessionRecorders – nichts verlässt den PC.
 *
 * Funktioniert nur in Vesktop: Dort läuft Sprache über WebRTC und jede Person hat einen
 * eigenen Audiostrom. Die normale Discord-App mischt alles in einem nativen Modul.
 */

import { definePluginSettings } from "@api/Settings";
import { Logger } from "@utils/Logger";
import definePlugin, { OptionType, PluginNative, StartAt } from "@utils/types";
import { ChannelStore, GuildMemberStore, MediaEngineStore, SelectedChannelStore, showToast, UserStore, VoiceStateStore } from "@webpack/common";

const Native = VencordNative.pluginHelpers.AscandirSessionRecorder as PluginNative<typeof import("./native")>;
const logger = new Logger("AscandirSessionRecorder");
const PLUGIN_VERSION = 2;

const settings = definePluginSettings({
    port: {
        type: OptionType.NUMBER,
        description: "Port des SessionRecorders (Standard 7863)",
        default: 7863,
    },
    notify: {
        type: OptionType.BOOLEAN,
        description: "Kurze Meldung beim Start/Ende der Einzelspur-Aufnahme",
        default: true,
    },
});

// ---------- WebRTC beobachten ----------
const pcs = new Set<RTCPeerConnection>();
const midSsrc = new Map<string, number>();        // "pcIndex:mid" → ssrc (aus der Remote-SDP)
const ssrcUser = new Map<number, string>();       // ssrc → Discord-User-ID
let origSRD: typeof RTCPeerConnection.prototype.setRemoteDescription | null = null;
const pcIds = new WeakMap<RTCPeerConnection, number>();
let pcCounter = 0;

function pcId(pc: RTCPeerConnection) {
    if (!pcIds.has(pc)) pcIds.set(pc, ++pcCounter);
    return pcIds.get(pc)!;
}

function parseSdp(pc: RTCPeerConnection, sdp: string) {
    const id = pcId(pc);
    for (const sec of sdp.split(/\r?\nm=/).slice(1)) {
        if (!sec.startsWith("audio")) continue;
        const mid = /a=mid:(\S+)/.exec(sec)?.[1];
        const ssrc = /a=ssrc:(\d+)/.exec(sec)?.[1];
        if (mid && ssrc) midSsrc.set(`${id}:${mid}`, Number(ssrc));
        // Manche Versionen tragen die User-ID direkt in msid/cname ein
        const snow = /a=(?:msid|ssrc:\d+ (?:cname|msid)):?\s*(\d{17,20})/.exec(sec)?.[1];
        if (ssrc && snow) ssrcUser.set(Number(ssrc), snow);
    }
}

function hookWebRTC() {
    if (origSRD) return;
    origSRD = RTCPeerConnection.prototype.setRemoteDescription;
    RTCPeerConnection.prototype.setRemoteDescription = function (this: RTCPeerConnection, desc?: any, ...rest: any[]) {
        try {
            pcs.add(this);
            if (desc?.sdp) parseSdp(this, desc.sdp);
            this.addEventListener("connectionstatechange", () => {
                if (this.connectionState === "closed") pcs.delete(this);
            });
        } catch (e) { logger.error(e); }
        return (origSRD as any).call(this, desc, ...rest);
    } as any;
}

function unhookWebRTC() {
    if (origSRD) RTCPeerConnection.prototype.setRemoteDescription = origSRD;
    origSRD = null;
}

// Discords Sprachverbindung kennt die Zuordnung ssrc → User; createUser mitschneiden
const hookedConns = new WeakSet<object>();
function voiceConnections(): any[] {
    try {
        return [...(MediaEngineStore.getMediaEngine() as any).connections ?? []]
            .filter(c => !c.destroyed && (c.context ?? "default") === "default");
    } catch { return []; }
}

function hookConnections() {
    for (const c of voiceConnections()) {
        if (hookedConns.has(c)) continue;
        hookedConns.add(c);
        const orig = c.createUser;
        if (typeof orig === "function") {
            c.createUser = function (userId: string, audioSSRC: number, ...rest: any[]) {
                if (audioSSRC) ssrcUser.set(Number(audioSSRC), String(userId));
                return orig.call(this, userId, audioSSRC, ...rest);
            };
        }
    }
}

function userForSsrc(ssrc: number | undefined): string | null {
    if (!ssrc) return null;
    const known = ssrcUser.get(ssrc);
    if (known) return known;
    for (const c of voiceConnections()) {
        try {
            const u = c.getUserIdBySsrc?.(ssrc);
            if (u) {
                ssrcUser.set(ssrc, String(u));
                return String(u);
            }
        } catch { }
    }
    return null;
}

// ---------- Aufnahme ----------
interface Tap {
    key: string;               // pcIndex:mid
    receiver: RTCRtpReceiver;
    nodes: AudioNode[];
    audio: HTMLAudioElement;
}

interface UserInfo { id: string; name: string; anzeige: string; file: string; erste: number; letzte: number; }

let ctx: AudioContext | null = null;
let rec: { dir: string; origin: number; originEpoch: number; sr: number; } | null = null;
const taps = new Map<string, Tap>();
const pending = new Map<string, { pos: number; data: Int16Array; }[]>();
const users = new Map<string, UserInfo>();
let pollTimer: any = null, flushTimer: any = null, scanTimer: any = null;
let warnedNoWebRTC = false;
let skip = new Set<string>();          // im SessionRecorder abgehakte Personen (werden nicht aufgenommen)

function myId() {
    try { return UserStore.getCurrentUser()?.id; } catch { return undefined; }
}

function displayName(id: string) {
    const u: any = UserStore.getUser(id);
    let nick: string | undefined;
    try {
        const ch = ChannelStore.getChannel(SelectedChannelStore.getVoiceChannelId()!);
        if (ch?.guild_id) nick = GuildMemberStore.getNick(ch.guild_id, id) ?? undefined;
    } catch { }
    return { name: u?.username ?? id, anzeige: nick ?? u?.globalName ?? u?.username ?? id };
}

function currentOwner(tap: Tap): string | null {
    // 1. aktive Quelle (nur während gesprochen wird), 2. SSRC aus der SDP
    let ssrc: number | undefined;
    try { ssrc = tap.receiver.getSynchronizationSources?.()[0]?.source; } catch { }
    const u = userForSsrc(ssrc) ?? userForSsrc(midSsrc.get(tap.key));
    return u && u !== myId() ? u : null;
}

function ensureUser(id: string) {
    if (users.has(id) || !rec) return;
    const n = displayName(id);
    users.set(id, { id, ...n, file: `${id}.pcm`, erste: Date.now(), letzte: Date.now() });
    writeMeta();
    Native.log(rec.dir, `Neue Spur: ${n.anzeige} (${n.name}, ${id})`);
}

function onBlock(tap: Tap, e: AudioProcessingEvent) {
    if (!rec || !ctx) return;
    const input = e.inputBuffer.getChannelData(0);
    let peak = 0;
    for (let i = 0; i < input.length; i += 8) peak = Math.max(peak, Math.abs(input[i]));
    if (peak < 1e-4) return;                       // Stille: Datei bleibt dort ohnehin leer
    const owner = currentOwner(tap);
    if (!owner || skip.has(owner)) return;
    ensureUser(owner);
    const pos = Math.round((e.playbackTime - rec.origin) * rec.sr);
    if (pos < 0) return;
    const pcm = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
        const v = Math.max(-1, Math.min(1, input[i]));
        pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    if (!pending.has(owner)) pending.set(owner, []);
    pending.get(owner)!.push({ pos, data: pcm });
    users.get(owner)!.letzte = Date.now();
}

async function flush() {
    if (!rec) return;
    const dir = rec.dir;
    for (const [uid, blocks] of pending) {
        if (!blocks.length) continue;
        pending.set(uid, []);
        blocks.sort((a, b) => a.pos - b.pos);
        // zusammenhängende Blöcke zu einem Schreibvorgang zusammenfassen
        let i = 0;
        while (i < blocks.length) {
            let j = i, len = blocks[i].data.length;
            while (j + 1 < blocks.length && blocks[j + 1].pos === blocks[j].pos + blocks[j].data.length) {
                j++;
                len += blocks[j].data.length;
            }
            const merged = new Int16Array(len);
            let off = 0;
            for (let k = i; k <= j; k++) { merged.set(blocks[k].data, off); off += blocks[k].data.length; }
            try {
                await Native.write(dir, users.get(uid)!.file, blocks[i].pos * 2, new Uint8Array(merged.buffer));
            } catch (err) { logger.error("Schreiben fehlgeschlagen", err); }
            i = j + 1;
        }
    }
}

function writeMeta() {
    if (!rec) return;
    let channel: string | undefined, guild: string | undefined;
    try {
        channel = SelectedChannelStore.getVoiceChannelId() ?? undefined;
        guild = channel ? ChannelStore.getChannel(channel)?.guild_id : undefined;
    } catch { }
    const meta = {
        version: 1, quelle: "Vencord-Plugin AscandirSessionRecorder", sample_rate: rec.sr, kanaele: 1,
        format: "s16le", start_epoch_ms: rec.originEpoch, kanal: channel, server: guild, ich: myId(),
        spuren: [...users.values()],
    };
    Native.writeMeta(rec.dir, JSON.stringify(meta, null, 2));
}

function scanReceivers() {
    if (!rec || !ctx) return;
    hookConnections();
    for (const pc of pcs) {
        if (pc.connectionState === "closed") continue;
        for (const tr of pc.getTransceivers()) {
            const r = tr.receiver;
            if (r.track?.kind !== "audio" || !tr.mid) continue;
            const key = `${pcId(pc)}:${tr.mid}`;
            if (taps.has(key) && taps.get(key)!.receiver === r) continue;
            const stream = new MediaStream([r.track]);
            // Chrome liefert entfernte WebRTC-Töne nur an WebAudio, wenn ein Audio-Element sie abspielt
            const audio = new Audio();
            audio.muted = true;
            audio.srcObject = stream;
            audio.play().catch(() => { });
            const src = ctx.createMediaStreamSource(stream);
            const proc = ctx.createScriptProcessor(4096, 1, 1);
            const tap: Tap = { key, receiver: r, nodes: [src, proc], audio };
            proc.onaudioprocess = e => onBlock(tap, e);
            src.connect(proc);
            proc.connect(ctx.destination);     // Ausgang bleibt stumm (wir kopieren nichts hinein)
            taps.set(key, tap);
        }
    }
    if (!pcs.size && !warnedNoWebRTC) {
        warnedNoWebRTC = true;
        Native.log(rec.dir, "Keine WebRTC-Verbindung gefunden – läuft Discord in Vesktop und bist du im Sprachkanal?");
    }
}

async function startRecording(dir: string) {
    ctx = new AudioContext({ sampleRate: 48000 });
    await ctx.resume().catch(() => { });
    if (ctx.state !== "running") {
        // Falls der Browser-Teil Audio erst nach einer Benutzeraktion erlaubt
        const wake = () => ctx?.resume().catch(() => { });
        document.addEventListener("pointerdown", wake, { once: true });
        document.addEventListener("keydown", wake, { once: true });
        Native.log(dir, `AudioContext ist "${ctx.state}" – wird beim nächsten Klick in Vesktop gestartet`);
    }
    rec = { dir, origin: ctx.currentTime, originEpoch: Date.now(), sr: ctx.sampleRate };
    users.clear();
    warnedNoWebRTC = false;
    Native.log(dir, `Start – ${pcs.size} WebRTC-Verbindung(en), ${ssrcUser.size} bekannte SSRCs, ${ctx.sampleRate} Hz`);
    writeMeta();
    scanReceivers();
    scanTimer = setInterval(scanReceivers, 2000);
    flushTimer = setInterval(flush, 1500);
    if (settings.store.notify) showToast({ text: "🔴 SessionRecorder: Einzelspuren werden aufgenommen", variant: "success" });
}

async function stopRecording() {
    clearInterval(scanTimer);
    clearInterval(flushTimer);
    for (const t of taps.values()) {
        for (const n of t.nodes) { try { n.disconnect(); } catch { } }
        t.audio.srcObject = null;
    }
    taps.clear();
    await flush();
    if (rec) {
        writeMeta();
        Native.log(rec.dir, `Ende – ${users.size} Spur(en): ${[...users.values()].map(u => u.anzeige).join(", ")}`);
    }
    await Native.closeAll();
    await ctx?.close().catch(() => { });
    ctx = null;
    const n = users.size;
    rec = null;
    if (settings.store.notify) showToast({ text: `⏹️ SessionRecorder: ${n} Einzelspur(en) gespeichert`, variant: "default" });
}

/** Wer ist gerade mit mir im Sprachkanal? (für die Auswahl im SessionRecorder) */
function channelInfo() {
    let channel: string | undefined, people: { id: string; name: string; anzeige: string; }[] = [];
    try {
        channel = SelectedChannelStore.getVoiceChannelId() ?? undefined;
        if (channel) {
            const me = myId();
            people = Object.keys(VoiceStateStore.getVoiceStatesForChannel(channel) ?? {})
                .filter(id => id !== me)
                .map(id => ({ id, ...displayName(id) }));
        }
    } catch { }
    const name = channel ? ChannelStore.getChannel(channel)?.name : undefined;
    return { version: PLUGIN_VERSION, kanal: channel, kanal_name: name, personen: people, webrtc: pcs.size > 0 };
}

let busy = false;
async function poll() {
    if (busy) return;
    busy = true;
    try {
        const s = await Native.status(settings.store.port, JSON.stringify(channelInfo()));
        skip = new Set<string>(Array.isArray(s?.nicht_aufnehmen) ? s.nicht_aufnehmen.map(String) : []);
        const want = !!s?.recording && typeof s?.dir === "string";
        if (want && !rec) await startRecording(s.dir);
        else if (rec && (!want || s.dir !== rec.dir)) await stopRecording();
    } catch (e) {
        logger.error(e);
    } finally {
        busy = false;
    }
}

export default definePlugin({
    name: "AscandirSessionRecorder",
    description: "Nimmt im Sprachkanal jede Person als eigene Spur für den Ascandir SessionRecorder auf (nur in Vesktop).",
    authors: [{ name: "Ascandir", id: 0n }],
    settings,
    startAt: StartAt.Init,
    enabledByDefault: true,

    start() {
        hookWebRTC();
        pollTimer = setInterval(poll, 1000);
    },

    async stop() {
        clearInterval(pollTimer);
        if (rec) await stopRecording();
        unhookWebRTC();
    },
});
