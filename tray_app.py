"""Conferência de Documentos (Document Comparator)
Windows 11 system-tray app. Drag 2+ PDFs of the SAME shipment onto the window;
Gemini reads them all and the app shows ONLY what matters: the fields that
DISAGREE, with the source quote inline, so a human checks before shipping.

Run:  pythonw tray_app.py   (or run_tray.vbs / the .bat)
Batch/test:  python tray_app.py file1.pdf file2.pdf ...  -> prints JSON
"""

from __future__ import annotations

import os
import threading

from PIL import Image, ImageDraw
import pystray
from tkinterdnd2 import DND_FILES, TkinterDnD
import tkinter as tk
from tkinter import ttk, font as tkfont, messagebox

from compare import autostart, engine
from compare.engine import minority_docs, norm as _norm
from compare.gemini import GeminiError


def _flt(v) -> float:
    try:
        return float(_norm(v))
    except ValueError:
        return float("-inf")

APP_NAME = "Conferência de Documentos"
DARK = "#1F4E79"
MID = "#2E75B6"
BAND = "#DDEBF7"
OK = "#1E7E34"
WARN = "#B8860B"
ERR = "#C0392B"
DIM = "#8A8F98"
BG = "#F4F6F9"
BORDER = "#D6DCE3"
DROPBG = "#EDF4FC"


def _make_icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=12, fill=DARK)
    d.rectangle([12, 14, 30, 50], fill="white")
    d.rectangle([34, 14, 52, 50], fill=BAND)
    d.line([15, 22, 27, 22], fill=MID, width=2)
    d.line([15, 28, 27, 28], fill=MID, width=2)
    d.line([37, 22, 49, 22], fill=MID, width=2)
    d.line([37, 40, 41, 46], fill=OK, width=3)
    d.line([41, 46, 50, 32], fill=OK, width=3)
    return img


class App:
    def __init__(self) -> None:
        self.root = TkinterDnD.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("720x640")
        self.root.minsize(540, 480)
        self.root.configure(bg="white")
        self._comparison: dict | None = None
        self._chat_history: list[dict] = []
        self._staged: list[str] = []        # files queued for comparison
        self._busy = False
        self._tick = 0

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.icon = pystray.Icon(
            "doc_comparator", _make_icon_image(), APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Mostrar janela", self._show_from_tray,
                                 default=True),
                pystray.MenuItem("Iniciar com o Windows", self._toggle_autostart,
                                 checked=lambda i: autostart.is_enabled()),
                pystray.MenuItem("Sair", self._quit_from_tray),
            ))
        threading.Thread(target=self.icon.run, daemon=True).start()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.body_f = tkfont.Font(family="Segoe UI", size=10)
        self.small_f = tkfont.Font(family="Segoe UI", size=9)
        self.cap_f = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.mono = tkfont.Font(family="Consolas", size=10)
        self.mono_b = tkfont.Font(family="Consolas", size=10, weight="bold")

        self.root.configure(bg=BG)
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TButton", font=self.small_f, padding=(10, 5))
        style.configure("Primary.TButton", font=self.body_f, padding=(18, 6))
        style.configure("TCheckbutton", background=BG, foreground="#555",
                        font=self.small_f)

        def card(parent):                       # thin-bordered white card
            outer = tk.Frame(parent, bg=BORDER)
            inner = tk.Frame(outer, bg="white")
            inner.pack(fill="both", expand=True, padx=1, pady=1)
            return outer, inner

        # ── queue card: the drop target IS the list (one box, not three) ──
        qcard, qin = card(self.root)
        qin.configure(bg=DROPBG)
        qcard.pack(fill="x", padx=14, pady=(14, 8))
        tk.Label(qin, text="⬇   ARRASTE OS PDFs AQUI   ·   de qualquer pasta, "
                 "um por vez", bg=DROPBG, fg=DARK, font=self.cap_f,
                 anchor="w").pack(fill="x", padx=10, pady=(8, 4))
        lbw = tk.Frame(qin, bg=DROPBG)
        lbw.pack(fill="x", padx=8, pady=(0, 8))
        self.stage = tk.Listbox(lbw, height=4, font=self.small_f, bd=0,
                                activestyle="none", relief="flat",
                                highlightthickness=0, bg="white",
                                selectmode="extended", selectbackground=BAND,
                                selectforeground=DARK)
        ssb = ttk.Scrollbar(lbw, command=self.stage.yview)
        self.stage.configure(yscrollcommand=ssb.set)
        ssb.pack(side="right", fill="y")
        self.stage.pack(side="left", fill="both", expand=True)
        for w in (qin, lbw, self.stage):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", self._on_drop)
        self.stage.bind("<Delete>", lambda e: self._remove_selected())

        # ── action row ──
        ctl = tk.Frame(self.root, bg=BG)
        ctl.pack(fill="x", padx=14)
        self.compare_btn = ttk.Button(ctl, text="Comparar", style="Primary.TButton",
                                      command=self._compare, state="disabled")
        self.compare_btn.pack(side="left")
        self.rm_btn = ttk.Button(ctl, text="Remover", command=self._remove_selected,
                                 state="disabled")
        self.rm_btn.pack(side="left", padx=(8, 0))
        self.clr_btn = ttk.Button(ctl, text="Limpar", command=self._clear_stage,
                                  state="disabled")
        self.clr_btn.pack(side="left", padx=(6, 0))
        self.show_all = tk.BooleanVar(value=False)
        self.all_chk = ttk.Checkbutton(
            ctl, text="mostrar campos que batem", variable=self.show_all,
            command=self._render, state="disabled")
        self.all_chk.pack(side="right")

        # ── verdict / status line ──
        self.verdict = tk.Label(
            self.root, text="Arraste os documentos e clique Comparar.",
            bg=BG, fg="#555", font=self.body_f, anchor="w")
        self.verdict.pack(fill="x", padx=14, pady=(10, 4))

        # ── results card (the only area that grows on resize) ──
        rcard, rin = card(self.root)
        rcard.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.out = tk.Text(rin, font=self.mono, bg="white", relief="flat",
                           wrap="word", state="disabled", padx=12, pady=10,
                           highlightthickness=0)
        sb = ttk.Scrollbar(rin, command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.out.pack(side="left", fill="both", expand=True)
        self.out.tag_configure("bad", foreground=ERR, font=self.mono_b)
        self.out.tag_configure("doc", foreground=DARK)
        self.out.tag_configure("odd", foreground=ERR, font=self.mono_b)
        self.out.tag_configure("src", foreground=DIM)
        self.out.tag_configure("okline", foreground=OK)
        self.out.tag_configure("partial", foreground=WARN)
        self.out.tag_configure("dim", foreground=DIM)
        self.out.tag_configure("h", foreground=DARK, font=self.mono_b)

        # ── question row ──
        qf = tk.Frame(self.root, bg=BG)
        qf.pack(fill="x", padx=14, pady=(0, 12))
        self.qvar = tk.StringVar()
        self.qentry = ttk.Entry(qf, textvariable=self.qvar, font=self.body_f)
        self.qentry.pack(side="left", fill="x", expand=True, ipady=2)
        self.qentry.configure(state="disabled")
        self.qentry.bind("<Return>", lambda e: self._send_question())
        self.send_btn = ttk.Button(qf, text="Perguntar",
                                   command=self._send_question, state="disabled")
        self.send_btn.pack(side="right", padx=(8, 0))

        self._refresh_stage()
        self._results_placeholder()

    def _results_placeholder(self) -> None:
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("end", "Os resultados da comparação aparecem aqui depois "
                        "que você clicar Comparar.", "dim")
        self.out.configure(state="disabled")

    # ── staging queue ────────────────────────────────────────────────────────
    def _on_drop(self, event) -> None:
        """Each drop ADDS to the queue (deduped) — no analysis until Comparar."""
        if self._busy:
            return
        added = 0
        for p in self.root.tk.splitlist(event.data):
            if not p.lower().endswith(".pdf"):
                continue
            key = os.path.normcase(os.path.abspath(p))
            if key not in (os.path.normcase(os.path.abspath(s))
                           for s in self._staged):
                self._staged.append(p)
                added += 1
        if not added:
            self.verdict.config(text="Solte arquivos PDF.", fg=ERR)
            return
        self._refresh_stage()

    def _refresh_stage(self) -> None:
        self.stage.delete(0, "end")
        n = len(self._staged)
        if n == 0:
            self.stage.insert("end", "   (nenhum documento ainda — arraste os PDFs)")
            self.stage.itemconfig(0, foreground=DIM)
        else:
            for p in self._staged:
                folder = os.path.basename(os.path.dirname(p)) or os.path.dirname(p)
                self.stage.insert("end", f" 📄  {os.path.basename(p)}      ·  {folder}")
        gated = "normal" if not self._busy else "disabled"
        self.compare_btn.config(
            text=f"Comparar ({n})" if n else "Comparar",
            state=("normal" if n >= 2 and not self._busy else "disabled"))
        self.rm_btn.config(state=gated if n else "disabled")
        self.clr_btn.config(state=gated if n else "disabled")
        if not self._busy:
            if n == 0:
                self.verdict.config(text="Arraste os documentos (de pastas "
                                    "diferentes, se precisar) e clique Comparar.",
                                    fg="#555")
            elif n == 1:
                self.verdict.config(text="1 documento na fila — arraste mais pelo "
                                    "menos 1.", fg=WARN)
            else:
                self.verdict.config(text=f"{n} documentos na fila. Clique "
                                    "Comparar quando estiver tudo.", fg=DARK)

    def _remove_selected(self) -> None:
        if self._busy:
            return
        for i in sorted(self.stage.curselection(), reverse=True):
            if i < len(self._staged):          # ignore the empty placeholder row
                del self._staged[i]
        self._refresh_stage()

    def _clear_stage(self) -> None:
        if self._busy:
            return
        self._staged.clear()
        self._refresh_stage()

    # ── analysis ─────────────────────────────────────────────────────────────
    def _compare(self) -> None:
        if self._busy or len(self._staged) < 2:
            return
        paths = list(self._staged)
        self._busy = True
        self.compare_btn.config(state="disabled")
        self.rm_btn.config(state="disabled")
        self.clr_btn.config(state="disabled")
        self._tick = 0
        self._tick_timer()
        self.root.update_idletasks()
        threading.Thread(target=self._analyze_worker, args=(paths,),
                         daemon=True).start()

    def _tick_timer(self) -> None:
        if not self._busy:
            return
        self.verdict.config(
            text=f"⏳ Analisando… {self._tick}s  (vários PDFs podem levar 1–4 min)",
            fg=DARK)
        self._tick += 1
        self.root.after(1000, self._tick_timer)

    def _analyze_worker(self, paths: list[str]) -> None:
        try:
            res = engine.analyze(paths)
            self.root.after(0, lambda: self._on_done(res))
        except GeminiError as e:
            self.root.after(0, lambda: self._fail("Falha no Gemini.\n" + str(e)))
        except ValueError as e:
            self.root.after(0, lambda: self._fail(
                "O Gemini não devolveu um resultado legível.\n" + str(e)))
        except Exception as e:  # noqa: BLE001
            self.root.after(0, lambda: self._fail(
                f"Erro inesperado: {type(e).__name__}: {e}"))

    def _fail(self, msg: str) -> None:
        self._busy = False
        self._refresh_stage()
        self.verdict.config(text="✗ " + msg.splitlines()[0], fg=ERR)
        messagebox.showerror("Não foi possível comparar", msg, parent=self.root)

    def _on_done(self, res: dict) -> None:
        self._busy = False
        self._comparison = res
        self._chat_history = []
        self._refresh_stage()                     # re-enable queue buttons
        self.all_chk.config(state="normal")
        self.qentry.config(state="normal")
        self.send_btn.config(state="normal")
        self._render()
        self._show()

    # ── render: divergences first, matches behind the toggle ─────────────────
    def _render(self) -> None:
        res = self._comparison
        if not res:
            return
        counts = res.get("_counts", {})
        n_mis = counts.get("mismatch", 0)
        n_short = counts.get("short", 0)
        n_alert = len(res.get("alerts") or [])
        if n_mis:
            self.verdict.config(
                text=f"🟥 {n_mis} divergência(s) — confira antes de embarcar.",
                fg=ERR)
        elif n_short:
            self.verdict.config(
                text=f"🟨 Totais batem, mas {n_short} soma(s) de partes não "
                     "fecham — confira se falta documento parcial.", fg=WARN)
        elif n_alert:
            self.verdict.config(
                text="✓ Números batem — mas há pontos a conferir (veja abaixo).",
                fg=WARN)
        else:
            self.verdict.config(text="✓ Os documentos batem nos campos comparáveis.",
                                fg=OK)

        doctype = {d["id"]: d.get("doc_type", d["id"])
                   for d in res.get("docs", [])}
        fields = res.get("fields", [])

        self.out.configure(state="normal")
        self.out.delete("1.0", "end")

        if res.get("summary"):
            self.out.insert("end", res["summary"].strip() + "\n\n", "dim")

        # alerts the human should check (e.g. "há 2 CSI — confira aceitação")
        alerts = res.get("alerts") or []
        if alerts:
            self.out.insert("end", "⚠ Conferir:\n", "h")
            for a in alerts:
                sev = (a.get("severity") or "").lower()
                tg = "bad" if sev == "alta" else "partial"
                self.out.insert("end", f"   • {a.get('text', a.get('field',''))}\n",
                                tg)
            self.out.insert("end", "\n")

        mism = [f for f in fields if f["status"] == "mismatch"]
        # split-shipment totals that ADD UP exactly: show the arithmetic (green)
        soma_ok = [f for f in fields
                   if f.get("_sum", {}).get("ok") and f["status"] != "mismatch"]
        # totals agree but the partial docs don't fully reconcile (yellow warn)
        soma_short = [f for f in fields if f["status"] == "short"]
        info = [f for f in fields if f["status"] == "info"]

        if mism:
            for f in mism:
                self._render_field(f, doctype, bad=True)
        elif not soma_ok and not soma_short:
            self.out.insert("end", "Nenhuma divergência nos campos que deveriam "
                            "bater.\n", "okline")
        for f in soma_short:
            self._render_short(f, doctype)
        for f in soma_ok:
            self._render_sum(f, doctype)
        for f in info:                       # differ but expected to (financeiro…)
            self._render_field(f, doctype, bad=False, tag="dim")

        if self.show_all.get():
            self.out.insert("end", "\n— campos que batem / só num doc —\n", "h")
            for f in fields:
                if f["status"] in ("match", "partial", "single") \
                        and not f.get("_sum"):
                    self._render_compact(f, doctype)
        self.out.configure(state="disabled")

    def _render_sum(self, f: dict, doctype: dict) -> None:
        """A relation='soma' field whose parts add up to the whole — consistent,
        shown green with the arithmetic so the human sees WHY it's ok. Roles may
        be missing (safety-net detection): infer total = the largest value."""
        s = f["_sum"]
        present = [c for c in f.get("cells", []) if c.get("present")]
        roles = {c["doc"]: c.get("role") for c in present}
        if not any(r == "parte" for r in roles.values()):
            top = max(present, key=lambda c: _flt(c.get("value")))
            roles = {c["doc"]: ("total" if c is top else "parte") for c in present}
        parts = [c for c in present if roles.get(c["doc"]) == "parte"]
        expr = " + ".join(str(c.get("value", "")).strip() for c in parts)
        self.out.insert("end", f"✓ {f.get('label','')} — soma confere\n", "okline")
        self.out.insert("end", f"     {expr} = {s['total']:g}\n", "doc")
        for c in present:
            dt = doctype.get(c["doc"], c["doc"])
            tag = {"total": " (total)", "parte": " (parte)"}.get(roles.get(c["doc"]), "")
            self.out.insert("end", f"     {dt[:16]:<16} "
                            f"{str(c.get('value','')).strip()}{tag}\n", "src")
        self.out.insert("end", "\n")

    def _render_short(self, f: dict, doctype: dict) -> None:
        """relation='soma' where the grand totals agree but the partial docs
        provided don't add up to it — likely an incomplete set. Yellow warning,
        NOT a red divergence: tells the human to check if a partial doc is
        missing rather than alarming about a number mismatch."""
        s = f["_sum"]
        present = [c for c in f.get("cells", []) if c.get("present")]
        self.out.insert("end", f"🟨 {f.get('label','')} — partes não fecham o "
                        "total\n", "partial")
        self.out.insert("end", f"     soma das partes = {s['soma']:g}  ·  total "
                        f"declarado = {s['total']:g}  → confira se falta um "
                        "documento parcial (CSI/draft)\n", "src")
        for c in present:
            dt = doctype.get(c["doc"], c["doc"])
            tag = {"total": " (total)", "parte": " (parte)"}.get(c.get("role"), "")
            self.out.insert("end", f"     {dt[:16]:<16} "
                            f"{str(c.get('value','')).strip()}{tag}\n", "src")
        self.out.insert("end", "\n")

    def _render_field(self, f: dict, doctype: dict, *, bad: bool,
                      tag: str | None = None) -> None:
        icon = "🟥" if bad else "•"
        head_tag = "bad" if bad else (tag or "h")
        self.out.insert("end", f"{icon} {f.get('label','')}\n", head_tag)
        if f.get("note") and bad:
            self.out.insert("end", f"     {f['note']}\n", "src")
        odd = minority_docs(f) if bad else set()
        cells = {c["doc"]: c for c in f.get("cells", [])}
        for did, dt in doctype.items():
            c = cells.get(did, {})
            if not c.get("present"):
                continue
            val = str(c.get("value", "")).strip()
            mark = "  ⚠" if did in odd else ""
            self.out.insert("end", f"     {dt[:14]:<14} ", "doc")
            self.out.insert("end", f"{val}{mark}\n", "odd" if did in odd else None)
        if bad:                              # source only for the odd-one-out docs
            for did in (odd or [c["doc"] for c in f.get("cells", [])
                               if c.get("present")]):
                src = cells.get(did, {}).get("source")
                if src:
                    self.out.insert("end", f"        origem {doctype.get(did,did)}: "
                                    f"“{src}”\n", "src")
        self.out.insert("end", "\n")

    def _render_compact(self, f: dict, doctype: dict) -> None:
        present = [c for c in f.get("cells", []) if c.get("present")]
        val = present[0].get("value", "") if present else "—"
        tag = "okline" if f["status"] == "match" else "partial"
        suffix = "" if f["status"] == "match" else f"  (só {len(present)}/"\
            f"{len(doctype)} docs)"
        self.out.insert("end", f"  ✓ {f.get('label','')}: ", tag)
        self.out.insert("end", f"{val}{suffix}\n", "dim")

    # ── question box ─────────────────────────────────────────────────────────
    def _send_question(self) -> None:
        if self._busy or not self._comparison:
            return
        q = self.qvar.get().strip()
        if not q:
            return
        self.qvar.set("")
        self.out.configure(state="normal")
        self.out.insert("end", f"\n❓ {q}\n", "h")
        self.out.insert("end", "   …\n", "dim")
        self.out.see("end")
        self.out.configure(state="disabled")
        self._chat_history.append({"role": "user", "text": q})
        self.send_btn.config(state="disabled")
        self.qentry.config(state="disabled")
        threading.Thread(target=self._answer_worker, args=(q,),
                         daemon=True).start()

    def _answer_worker(self, q: str) -> None:
        try:
            ans = engine.answer_question(self._comparison, self._chat_history, q)
        except Exception as e:  # noqa: BLE001
            ans = f"(erro ao responder: {type(e).__name__}: {e})"
        self.root.after(0, lambda: self._answer_done(ans))

    def _answer_done(self, ans: str) -> None:
        self.out.configure(state="normal")
        self.out.delete("end-2l", "end-1l")        # drop the "…" placeholder
        self.out.insert("end", f"   {ans}\n", "doc")
        self.out.see("end")
        self.out.configure(state="disabled")
        self._chat_history.append({"role": "assistant", "text": ans})
        self.send_btn.config(state="normal")
        self.qentry.config(state="normal")
        self.qentry.focus_set()

    # ── tray / lifecycle ─────────────────────────────────────────────────────
    def hide_window(self) -> None:
        self.root.withdraw()

    def _show_from_tray(self, *_a) -> None:
        self.root.after(0, self._show)

    def _show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))

    def _toggle_autostart(self, _icon=None, _item=None) -> None:
        try:
            autostart.disable() if autostart.is_enabled() else autostart.enable()
        except OSError:
            pass

    def _quit_from_tray(self, *_a) -> None:
        self.root.after(0, self._quit)

    def _quit(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _headless(pdfs: list[str]) -> int:
    import json
    try:
        res = engine.analyze(pdfs)
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: {type(e).__name__}: {e}")
        return 1
    out = {k: v for k, v in res.items() if not k.startswith("_")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    _pdfs = [a for a in sys.argv[1:] if a.lower().endswith(".pdf")]
    if _pdfs:
        sys.exit(_headless(_pdfs))
    App().run()
