# Document Comparator

You have a shipment going out. There are five PDFs on your desk — an inspection
certificate, a packing list, a commercial invoice, a booking confirmation, a
customs form. They were all written by different people, at different times, in
different formats. One of them has the wrong net weight. One has a container
number that doesn't match the others. You won't know until you find it by hand.

This app finds it for you.

Drag 2+ PDFs onto the window. [Gemini](https://deepmind.google/technologies/gemini/)
reads every document natively, aligns each field across all of them side by side,
and highlights every mismatch in red — before anything ships.

![Platform](https://img.shields.io/badge/platform-Windows%2011-lightgrey)
![Python](https://img.shields.io/badge/python-3.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What you see

A grid: one row per field (contract number, net weight, gross weight, container,
seal, importer, product quantities…), one column per document.

- 🟥 **Red** — documents disagree on this field
- 🟨 **Yellow** — only some documents have this field, or partial sums don't match
- ⬜ **White** — all documents agree

Click any row to see the exact value each document contains, along with the
literal source snippet Gemini pulled it from — so you can verify it in the PDF.

There's also a **chat box**: ask anything about the document set ("why is the
weight in document 2 smaller?", "which product is missing?").

---

## How it works

**Hybrid engine.** Gemini handles extraction and alignment — it reads each
document natively (no OCR step) and understands that `28.031,135 kg` and
`28031.135 kg` are the same number written in two different locales.

For number comparisons, the app recalculates matches deterministically after
Gemini returns, because LLMs can mislabel numeric equality. Text equivalence
(bilingual field names, address formats) stays with Gemini.

---

## Setup

```bash
pip install -r requirements.txt
```

Create a `config.json` next to `tray_app.py`:

```json
{
  "gemini_api_key": "YOUR_KEY_HERE"
}
```

Or set the environment variable `GEMINI_API_KEY`. No key is hardcoded.

---

## Run

```bash
pythonw tray_app.py                    # system tray, no console window
python tray_app.py a.pdf b.pdf c.pdf   # batch / test mode — prints JSON
```

Or double-click `run_tray.vbs` on Windows.

Tray menu: **Show window · Start with Windows · Quit**

Analyzing 5–6 PDFs takes 1–4 minutes. A live timer shows while it works.

---

## Key decisions

| Decision | Reason |
|---|---|
| Gemini native PDF reading (no OCR step) | Reads scanned and digital PDFs equally. Extracting text first loses layout context that helps identify field boundaries. |
| Hybrid engine: Gemini extracts, app recalculates numbers | LLMs occasionally mislabel numeric matches due to locale formatting. Deterministic recalculation after the fact fixes this without re-prompting. |
| Click-to-source for every field | A red cell is only useful if you can immediately verify it. The source snippet turns "something's wrong" into "I know exactly where to look." |
| Re-drag to re-run, no state saved | No database, no history. The documents are the source of truth. |
| `gemini-2.5-flash` with fallback chain | Flash balances speed and accuracy. The fallback chain (`flash → flash-lite → 2.0-flash`) handles quota errors automatically. |

---

## License

MIT
