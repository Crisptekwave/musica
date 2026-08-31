/**
 * CrispPanel — CRISP engine embedded in Musica VJ.
 *
 * Drop a track -> DNA (BPM + kick grid) -> render an original remake loop ->
 * download WAV + E2S pattern. All on-device, self-contained audio DSP that
 * runs inside the Tauri WebView2 (no Rust changes, no provider calls).
 */
import { useRef, useState } from "react";
import { X, Upload, Loader2, Download, Music2 } from "lucide-react";
import { ensureAudioContext } from "./audio/context";
import { channelsFrom, downloadBlob, encodeWav } from "./audio/wav";
import { extractDna, type TrackDna } from "./audio/dna";
import { renderKickback } from "./audio/kickback";
import { buildPatternSyx, patternFilename } from "./audio/pattern";

export function CrispPanel({ onClose }: { onClose: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dna, setDna] = useState<TrackDna | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [busy, setBusy] = useState<"analyze" | "render" | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function onFile(file: File) {
    setBusy("analyze");
    setDna(null);
    setFileName(file.name);
    setNote(null);
    try {
      await ensureAudioContext();
      const raw = await file.arrayBuffer();
      const ctx = new OfflineAudioContext(1, 1, 44100);
      const buffer = await ctx.decodeAudioData(raw.slice(0));
      const d = extractDna(buffer);
      if (!d) {
        setNote("Could not read the track — need a few seconds of rhythm.");
        return;
      }
      setDna(d);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Decode failed");
    } finally {
      setBusy(null);
    }
  }

  async function render() {
    if (!dna) return;
    setBusy("render");
    setNote(null);
    try {
      const buffer = await renderKickback({
        genre: dna.genre,
        bpm: dna.bpm,
        kick: dna.kick,
        acid: dna.acid,
        perc: dna.perc,
        grit: dna.grit,
        bars: dna.bars,
        seed: (Math.random() * 1e9) | 0,
        kickGrid: dna.kickGrid,
      });
      const mono = channelsFrom(buffer)[0]!;
      let peak = 1e-9;
      for (let i = 0; i < mono.length; i++) {
        const a = Math.abs(mono[i]!);
        if (a > peak) peak = a;
      }
      const scale = peak > 0 ? 0.891 / peak : 1;
      for (let i = 0; i < mono.length; i++) mono[i] = Math.tanh(mono[i]! * scale * 1.05);

      const wav = encodeWav([mono], 44100, 16);
      const stem = (fileName ?? "remake").replace(/\.[^.]+$/, "").replace(/[^\w-]+/g, "_");
      const wavName = `remake_${stem}_${Math.round(dna.bpm)}.wav`;
      downloadBlob(wav, wavName);

      const acid = dna.kickGrid.map((k) => !k);
      const syx = await buildPatternSyx({
        name: stem.slice(0, 8) || "KICK",
        bpm: dna.bpm,
        bars: dna.bars,
        kick: dna.kickGrid,
        acid,
      });
      downloadBlob(syx, patternFilename(stem));
      setNote(`${wavName} + ${patternFilename(stem)} · ${dna.bpm} BPM · ${dna.genre}`);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Render error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 9000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(4,6,10,0.78)",
        backdropFilter: "blur(6px)",
      }}
    >
      <div
        style={{
          width: "min(620px, 92vw)",
          maxHeight: "86vh",
          overflowY: "auto",
          background: "linear-gradient(180deg,#0d1117,#0a0d13)",
          border: "1px solid rgba(255,255,255,0.12)",
          borderRadius: 16,
          padding: 22,
          color: "#e6edf3",
          fontFamily: "Space Grotesk, system-ui, sans-serif",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div>
            <div style={{ fontSize: 11, letterSpacing: 2, textTransform: "uppercase", color: "#7ee787" }}>
              CRISP · on-device
            </div>
            <h2 style={{ margin: "2px 0 0", fontSize: 22 }}>Drop a track — get a new loop</h2>
          </div>
          <button
            onClick={onClose}
            style={{ background: "transparent", border: 0, color: "#8b949e", cursor: "pointer", padding: 6 }}
          >
            <X size={20} />
          </button>
        </div>

        <p style={{ fontSize: 13, color: "#8b949e", lineHeight: 1.5 }}>
          CRISP reads the BPM + kick grid, then the synth writes a NEW original loop on
          that rhythm — WAV + E2S pattern, no ROM dumps, no network.
        </p>

        <input
          ref={inputRef}
          type="file"
          accept="audio/*,.wav,.aif,.aiff,.flac,.mp3,.m4a"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void onFile(f);
          }}
        />

        <button
          onClick={() => inputRef.current?.click()}
          disabled={busy === "analyze"}
          style={{
            marginTop: 16,
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 16px",
            borderRadius: 10,
            border: "1px solid rgba(255,255,255,0.14)",
            background: "rgba(255,255,255,0.04)",
            color: "#e6edf3",
            cursor: "pointer",
            fontSize: 14,
          }}
        >
          {busy === "analyze" ? <Loader2 size={16} className="spin" /> : <Upload size={16} />}
          {fileName ? `Re-read ${fileName}` : "Choose a track"}
        </button>

        {dna && (
          <div
            style={{
              marginTop: 14,
              padding: 12,
              borderRadius: 10,
              background: "rgba(127,231,135,0.08)",
              border: "1px solid rgba(127,231,135,0.25)",
            }}
          >
            <div style={{ fontSize: 12, fontFamily: "monospace", color: "#7ee787" }}>{dna.summary}</div>
            <div style={{ display: "flex", gap: 3, marginTop: 8, flexWrap: "wrap" }}>
              {dna.kickGrid.map((hit, i) => (
                <span
                  key={i}
                  style={{
                    width: 22,
                    height: 22,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    borderRadius: 5,
                    fontSize: 10,
                    fontFamily: "monospace",
                    background: hit ? "rgba(127,231,135,0.9)" : "rgba(255,255,255,0.06)",
                    color: hit ? "#0d1117" : "#4b5563",
                  }}
                >
                  {i + 1}
                </span>
              ))}
            </div>
            <button
              onClick={() => void render()}
              disabled={busy === "render"}
              style={{
                marginTop: 12,
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "9px 14px",
                borderRadius: 10,
                border: 0,
                background: "#7ee787",
                color: "#0d1117",
                fontWeight: 600,
                cursor: "pointer",
                fontSize: 13,
              }}
            >
              {busy === "render" ? <Loader2 size={15} className="spin" /> : <Music2 size={15} />}
              {busy === "render" ? "Rendering…" : "Render remake + pattern"}
            </button>
          </div>
        )}

        {note && (
          <div style={{ marginTop: 12, fontSize: 12, color: "#7ee787", fontFamily: "monospace" }}>
            <Download size={12} style={{ verticalAlign: -1, marginRight: 4 }} />
            {note}
          </div>
        )}
      </div>
      <style>{`.spin{animation:crspin 1.2s linear infinite}@keyframes crspin{to{transform:rotate(360deg)}}`}</style>
    </div>
  );
}