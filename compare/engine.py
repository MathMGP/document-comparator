"""Comparison engine: ask Gemini to read several PDFs and line up every shared
field across them, flagging where they disagree, with a source quote per cell so
a human can verify exactly where to look.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from . import gemini

# ── Analysis prompt ──────────────────────────────────────────────────────────
_SCHEMA = r"""
Responda APENAS com um JSON válido (sem markdown, sem ```), neste formato exato:

{
  "docs": [
    {"id": "D1", "filename": "<nome do arquivo>", "doc_type": "<tipo curto, ex: RASTREABILIDADE, ROMANEIO, CSI/GTA, INVOICE, PACKING LIST, BOOKING>"}
  ],
  "fields": [
    {
      "label": "<nome do campo em português, ex: Número do contrato>",
      "kind": "numero" | "texto",
      "relation": "igual" | "soma" | "informativo",
      "should_match": true,
      "equivalent": true,
      "cells": [
        {"doc": "D1", "value": "<valor literal do documento ou '—' se ausente>", "present": true, "role": "total" | "parte" | "", "source": "<trecho LITERAL curto de onde veio, ou '' se ausente>"}
      ],
      "note": "<por que diverge, curto; '' se equivalentes>"
    }
  ],
  "alerts": [
    {"severity": "alta" | "media" | "baixa", "field": "<label>", "text": "<o que conferir e por quê>"}
  ],
  "summary": "<2-4 frases em português: o conjunto bate? o que conferir primeiro?>"
}

REGRAS:
- Uma entrada em "cells" para CADA documento, na ordem de "docs". Ausente → value="—", present=false, source="".
- "kind": "numero" para quantidades (peso líquido/bruto, caixas, peças, valores); "texto" para o resto (nomes, endereços, país, condições, identificadores alfanuméricos).

- "equivalent": os valores PRESENTES significam a MESMA coisa? Responda com critério SEMÂNTICO, NÃO textual:
  • Documentos podem estar em IDIOMAS diferentes (um CSI costuma vir em PT e EN). TRADUÇÃO É EQUIVALENTE: Egito=Egypt, CONGELADO=FROZEN, MARÍTIMO=SHIP, "MÚSCULO DIANTEIRO"=SHIN, ACÉM=CHUCK, PESCOÇO=NECK, PALETA=SHOULDER. NÃO marque tradução como divergência.
  • IGNORE diferença de: acento, maiúscula/minúscula, pontuação, espaços, hífen ("ALFARSI"="AL-FARSI"), abreviação de endereço, e PREFIXO de grupo/filial ("1-GRUPO EXEMPLO / Filial: 2-SUL..."=="SUL..."). Mesma entidade = equivalent:true.
  • equivalent=false SÓ quando há diferença SUBSTANTIVA: número/quantidade diferente, produto a mais/a menos, empresa realmente diferente, identificador que deveria repetir e não bate.

- "should_match": true quando o campo DEVERIA ser igual entre os documentos (contrato, container/lacre, SIF, totais de peso/caixas/peças, importador, exportador, NCM, destino, lista de produtos).
  false quando é NATURAL diferir: número PRÓPRIO de cada tipo de documento (cada um — rastreabilidade, romaneio, certificado/CSI — tem o SEU número; NÃO precisam bater entre si), data de emissão de cada documento, nº de página, valores financeiros internos. NÃO junte numa mesma linha os números próprios de documentos diferentes; se juntar, use should_match=false.

- DATAS — cuidado: NÃO misture tipos de data diferentes no mesmo campo. A data de EMBARQUE/SAÍDA (rastreabilidade, romaneio) é uma coisa; a data de EMISSÃO/CARIMBO do CSI/certificado é OUTRA e é NATURALMENTE POSTERIOR (a certificação sai depois do embarque). Isso NÃO é divergência: use campos separados, ou relation="informativo"/should_match=false. Só marque divergência de data se for o MESMO tipo de data divergindo (ex.: data de embarque diferente entre rastreabilidade e romaneio).

- ESPELHO / DRAFT / RASCUNHO de CSI: o "Nº Referência"/"Nro Referência" de um ESPELHO ou DRAFT é um identificador INTERNO do rascunho, NÃO é o número oficial do certificado (campo "Certificado N°"). São identificadores de naturezas diferentes — NÃO os alinhe no mesmo campo como se devessem bater (use should_match=false / informativo). Só marque divergência de número de certificado entre dois certificados OFICIAIS (ambos "Certificado N°").

- NÚMEROS: compare pelo valor (28.031,135 == 28031.135). Divergência de peso/caixa/peça/container/contrato é GRAVE.

- "relation" — MUITO IMPORTANTE para totais (peso, caixas, peças):
  • "igual" (padrão): todos os documentos deveriam ter o MESMO valor.
  • "soma": um embarque pode ter MÚLTIPLOS certificados/CSI/romaneios, cada um cobrindo PARTE dos produtos. Aí os totais NÃO são iguais — a SOMA das partes deve bater com o total geral (ex.: Rastreabilidade). Marque relation="soma"; em cada documento PARCIAL use cell.role="parte"; no documento com o total geral use cell.role="total". Se a soma das partes bate com o total → é CONSISTENTE, NÃO é divergência. (Continue gerando um ALERTA pedindo que o humano confira se múltiplos CSI são aceitos para o embarque.)
  • "informativo": diferem naturalmente, não alarmar.
  Só use "soma" quando realmente houver documentos parciais; na dúvida entre igual e soma, olhe se a soma das partes reproduz o total do outro documento.

- Produtos — REGRA IMPORTANTE para evitar ruído:
  • O check numérico principal são os TOTAIS do embarque (peso líquido total, peso bruto total, total de caixas, total de peças). Garanta esses sempre.
  • NÃO crie um campo por produto com peso/caixas (NÃO faça "Produto: X - Peso Líquido" para cada corte). Isso gera ruído porque os documentos listam produtos em granularidades diferentes (rastreabilidade por LOTE, CSI por produto) e em idiomas diferentes.
  • Em vez disso, crie UM ÚNICO campo "Lista de produtos" cujo valor em cada documento é o CONJUNTO de nomes de produtos/cortes daquele documento (ex.: "COLMEIA, BUCHO, LÍNGUA, …"). kind="texto". Marque divergência (equivalent=false) SÓ se um produto presente num documento estiver realmente AUSENTE de outro que deveria listá-lo (considerando tradução PT/EN e partes complementares de embarque dividido). Não duplique este campo.
- ROMANEIO de embarque — vocabulário próprio (NÃO crie divergência falsa):
  • O campo "Cliente" do Romaneio é o registro interno do EXPORTADOR, NÃO o importador/consignatário do destino. NÃO alinhe "Cliente" do Romaneio com Importador/Consignatário do CO/CSI.
  • "PL" no Romaneio costuma ser a PLACA do veículo, não o lacre. Não confunda com Número do Lacre.
  • Um Romaneio normalmente NÃO traz importador, consignatário, NCM nem lacre-SIF. A ausência desses campos NO ROMANEIO é ESPERADA — NÃO gere alerta nem divergência só porque estão ausentes nele.
- SIF: compare apenas o NÚMERO DO ESTABELECIMENTO (ex.: 9999). "9999", "SIF 9999" e "000001/SIF9999" são o MESMO SIF (000001 é lacre/sequencial) → equivalent=true.
- FATURA / NOTA FISCAL: o número de NOTA FISCAL (NF) do Romaneio é DIFERENTE do número de CONTRATO/PEDIDO que o Certificado de Origem rotula como "Fatura". Identificadores de naturezas distintas → should_match=false; NÃO alinhe NF com número de contrato.
- PESO: NÃO alinhe peso BRUTO com peso LÍQUIDO no mesmo campo (grandezas diferentes). Campos separados "Peso Líquido Total" e "Peso Bruto Total", cada um comparando só o MESMO tipo entre documentos.
- ALERTAS: gere alerta SÓ quando há algo REAL a conferir (valor divergente, parte faltando, número que deveria bater e não bate). NÃO gere alerta apenas porque um campo está ausente de um documento que estruturalmente não o carrega.
- "source": trecho REAL e curto do documento. NUNCA invente.
- Toda a saída em português; valores literais ficam como no documento.
"""

_ANALYSIS_INSTRUCTIONS = (
    "Você é um conferente de documentos de exportação (trading de alimentos). "
    "Recebe vários PDFs do MESMO embarque "
    "(ex.: RASTREABILIDADE, ROMANEIO, rascunho de CSI/GTA, invoice, packing "
    "list, booking). Sua tarefa: extrair cada informação e ALINHAR os mesmos "
    "campos entre todos os documentos, apontando onde divergem para um humano "
    "conferir antes do embarque. Seja rigoroso com números — divergência de "
    "peso, caixa, container, contrato ou SIF é grave.\n\n" + _SCHEMA
)


def analyze(pdf_paths: list[str], *, model: str | None = None) -> dict:
    """Returns the parsed comparison dict (see _SCHEMA). Raises GeminiError on
    API failure, ValueError if the model returns unparsable JSON even after a
    retry and a truncation-repair attempt."""
    parts = [{"text": _ANALYSIS_INSTRUCTIONS}]
    parts += gemini.pdf_parts(pdf_paths)

    data = None
    for attempt in range(2):                     # one retry: JSON errors are flaky
        raw = gemini.generate(parts, model=model, json_out=True, max_tokens=65536)
        try:
            data = _parse_json(raw)
            break
        except ValueError:
            if attempt == 0:
                continue
            data = _repair_truncated(raw)        # salvage what came through
            if data is None:
                raise
            data["_truncated"] = True
    # Backfill filenames ONLY when the model kept one doc per input file. If it
    # split a multi-certificate PDF into several logical docs (count mismatch),
    # trust the model's own filename/doc_type instead of corrupting by index.
    names = [Path(p).name for p in pdf_paths]
    docs = data.get("docs", [])
    if len(docs) == len(names):
        for d, n in zip(docs, names):
            d["filename"] = n
    data["_pdf_paths"] = pdf_paths
    classify(data)
    if data.get("_truncated"):
        data.setdefault("alerts", []).insert(0, {
            "severity": "alta",
            "field": "Análise incompleta",
            "text": "A resposta do Gemini foi cortada (documentos muito longos); "
                    "alguns campos do fim podem estar faltando. Compare em lotes "
                    "menores para garantir a conferência completa."})
    return data


# ── deterministic status (don't trust the model's own label) ─────────────────
def norm(v) -> str:
    """Normalize a value for equality: numbers BR-aware (28.031,135 == 28031.135
    == '28031,135', 1.089 == 1089), text lowered/space-collapsed. '' = absent."""
    if v is None:
        return ""
    s = str(v).strip().lower()
    if s in ("—", "-", "", "n/a", "na", "?"):
        return ""
    if re.fullmatch(r"[\d.,]+", s):
        if "," in s and "." in s:
            s = (s.replace(".", "").replace(",", ".")
                 if s.rfind(",") > s.rfind(".") else s.replace(",", ""))
        elif "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            return repr(round(float(s), 3))
        except ValueError:
            return re.sub(r"[.,]", "", s)
    # text: fold accents so INDÚSTRIA == INDUSTRIA never counts as a divergence
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


def _text_key(v) -> str:
    """Aggressive equality key for the deterministic text floor: accent-fold,
    lowercase, drop ALL non-alphanumerics. Two present values with the same key
    are the SAME (modulo accents/case/punctuation/spacing) and cannot be a real
    divergence — used to override the model when it mislabels them as different
    (e.g. 'CO., LTD' vs 'CO.,LTD', 'INDÚSTRIA' vs 'INDUSTRIA')."""
    n = norm(v)
    return re.sub(r"[^0-9a-z]+", "", n)


def _as_float(v) -> float | None:
    n = norm(v)
    try:
        return float(n)
    except ValueError:
        return None


def _is_numeric_field(present: list) -> bool:
    """All present cells parse as numbers → compare deterministically."""
    return bool(present) and all(_as_float(c.get("value")) is not None
                                 for c in present)


# Numbers within this absolute spread are considered equal — absorbs 2-dec vs
# 3-dec rounding (e.g. 12.558,125 vs 12.558,13 = 0.005) without hiding real
# divergences, which are whole kilos/boxes.
_NUM_TOL = 0.05


def _nums_differ(values: list) -> bool:
    """True if numeric values disagree beyond the rounding tolerance."""
    nums = [v for v in (_as_float(x) for x in values) if v is not None]
    return bool(nums) and (max(nums) - min(nums)) > _NUM_TOL


def _split_status(present: list, *, use_roles: bool):
    """Resolve a split-shipment (relation='soma') field deterministically.

    A field is CONSISTENT when EITHER:
      (a) ≥2 documents carry the grand total and those totals agree — multiple
          independent docs agreeing on the whole is strong evidence, even if not
          every partial doc is present; OR
      (b) the part values add up to the total (deduping a part that appears in
          two documents, e.g. a combined CSI PDF + its individual drafts).

    `use_roles=True` reads the model's total/parte tags; `use_roles=False` infers
    them (largest value = the whole, strictly-smaller = parts) as a safety net.

    Returns (state, shown, total) or None. state is:
      "ok"    — parts add up to the total exactly (or only agreeing totals);
      "short" — the grand totals agree but the parts present DON'T reconcile
                (likely an incomplete set of partial docs) → warn, NOT a red error;
      "bad"   — the grand totals themselves disagree → real divergence."""
    nums = [(c, _as_float(c.get("value"))) for c in present]
    nums = [(c, v) for c, v in nums if v is not None]

    if use_roles:
        totals = [v for c, v in nums if c.get("role") == "total"]
        parts = [v for c, v in nums if c.get("role") == "parte"]
        if not totals:
            return None
    else:
        if len(nums) < 3:                   # need a total + at least 2 parts
            return None
        big = max(v for _, v in nums)
        if big <= 0:
            return None
        margin = max(_NUM_TOL, abs(big) * 0.001)
        totals = [v for _, v in nums if v >= big - margin]
        parts = [v for _, v in nums if v < big - margin]
        if len(parts) < 2:
            return None
        for c, v in nums:                   # tag for the green arithmetic view
            c["role"] = "total" if v >= big - margin else "parte"

    total = max(totals)
    # Fixed 0.5 absorbs per-line 2-dec rounding accumulated across products
    # (≈0.05 total) without the slack of a percentage tolerance, so a real
    # shortfall of whole kilos/boxes is caught.
    tol = 0.5
    totals_spread_ok = (max(totals) - min(totals)) <= tol
    # "short" needs ≥2 INDEPENDENT master totals that corroborate each other; a
    # lone total with parts that don't sum is a genuine disagreement, not a
    # missing-partial situation.
    masters_corroborate = len(totals) >= 2 and totals_spread_ok
    psum = round(sum(parts), 3) if parts else None
    usum = (round(sum(sorted({round(p, 3) for p in parts})), 3)
            if parts else None)
    parts_ok = bool(parts) and (abs(psum - total) <= tol or
                                abs(usum - total) <= tol)
    if parts_ok:
        return ("ok", psum, total)
    if not parts:                           # only totals present
        return ("ok" if totals_spread_ok else "bad", total, total)
    if masters_corroborate:                 # masters agree, parts incomplete → warn
        return ("short", psum, total)
    return ("bad", psum, total)             # lone/disagreeing master → divergence


def minority_docs(field: dict) -> set:
    """Doc ids whose value is the odd one out among the present cells. For
    numeric fields this is exact; for text it falls back to normalized strings.
    Not meaningful for 'soma' fields (parts are SUPPOSED to differ)."""
    if field.get("relation") == "soma":
        return set()
    present = [c for c in field.get("cells", []) if c.get("present")]
    if len(present) < 2:
        return set()
    if _is_numeric_field(present):
        keyed = [(round(_as_float(c["value"]), 3), c["doc"]) for c in present]
    else:
        keyed = [(norm(c.get("value")), c["doc"]) for c in present
                 if norm(c.get("value"))]
        if len(keyed) < 2:
            return set()
    top = Counter(v for v, _ in keyed).most_common(1)[0][0]
    return {d for v, d in keyed if v != top}


def classify(data: dict) -> None:
    """Recompute each field's status. Hybrid: NUMBERS are compared by value here
    (the model mislabels them); TEXT trusts the model's language-aware
    'equivalent' verdict (so Egito==Egypt isn't a false divergence).
    Statuses: mismatch | short | partial | single | match | info."""
    rank = {"mismatch": 0, "short": 1, "partial": 2, "single": 3,
            "match": 4, "info": 5}
    ndocs = len(data.get("docs", []))
    for f in data.get("fields", []):
        present = [c for c in f.get("cells", []) if c.get("present")]
        n = len(present)
        should = f.get("should_match", True)

        if n <= 1:
            f["status"] = "single"
            continue

        rel = f.get("relation")
        short = False
        if rel == "soma" and _is_numeric_field(present):
            # split shipment: verify it HERE. Prefer VALUE-based inference
            # (largest = whole) — it's deterministic; the model's total/parte
            # tags are unreliable. Roles only as a fallback for <3 values.
            # state: ok | short (warn) | bad (red).
            chk = _split_status(present, use_roles=False)
            if chk is None:
                chk = _split_status(present, use_roles=True)
            if chk is None:
                differ = _nums_differ([c["value"] for c in present])
            else:
                state, soma, total = chk
                f["_sum"] = {"soma": soma, "total": total,
                             "ok": state == "ok", "short": state == "short"}
                differ = state == "bad"
                short = state == "short"
        elif rel == "informativo":
            f["status"] = "info"
            continue
        elif _is_numeric_field(present):
            differ = _nums_differ([c["value"] for c in present])
            if differ:
                chk = _split_status(present, use_roles=False)  # model forgot tag?
                if chk is not None:
                    state, soma, total = chk
                    f["relation"] = "soma"
                    f["_sum"] = {"soma": soma, "total": total,
                                 "ok": state == "ok", "short": state == "short"}
                    differ = state == "bad"
                    short = state == "short"
        else:
            keys = {_text_key(c.get("value")) for c in present
                    if _text_key(c.get("value"))}
            if len(keys) <= 1:
                # deterministic floor: all PRESENT values identical modulo
                # accent/case/punctuation/spacing → never a divergence, even if
                # the model labelled equivalent=false (it mislabels text often).
                differ = False
            else:
                equ = f.get("equivalent")
                if equ is None:              # model didn't say → fall back to text
                    differ = len({norm(c.get("value")) for c in present
                                  if norm(c.get("value"))}) > 1
                else:
                    differ = not equ         # trust model for genuine PT/EN, synonyms

        if differ:
            f["status"] = "mismatch" if should else "info"
        elif short:
            f["status"] = "short"            # totais batem, partes não fecham
        elif n < ndocs and rel != "soma":
            f["status"] = "partial"
        else:
            f["status"] = "match"
    data.get("fields", []).sort(key=lambda f: rank.get(f.get("status"), 9))
    data["_counts"] = Counter(f["status"] for f in data.get("fields", []))


# ── Follow-up chat ───────────────────────────────────────────────────────────
def answer_question(comparison: dict, history: list[dict], question: str,
                    *, model: str | None = None) -> str:
    """Answer a free-form question grounded in the comparison already produced.
    `history` is a list of {"role": "user"|"assistant", "text": ...}."""
    grounding = json.dumps(
        {k: v for k, v in comparison.items() if not k.startswith("_")},
        ensure_ascii=False, indent=None)
    convo = "\n".join(
        f"{'PERGUNTA' if h['role'] == 'user' else 'RESPOSTA'}: {h['text']}"
        for h in history[-8:])
    prompt = (
        "Você é o conferente que acabou de comparar os documentos de exportação "
        "abaixo (resultado em JSON). Responda à pergunta do usuário de forma "
        "curta, direta e em português, baseando-se SÓ neste resultado. Se a "
        "informação não estiver no resultado, diga que não consta. Quando útil, "
        "cite o documento (D1, D2…) e o trecho.\n\n"
        f"=== RESULTADO DA COMPARAÇÃO (JSON) ===\n{grounding}\n\n"
        + (f"=== CONVERSA ANTERIOR ===\n{convo}\n\n" if convo else "")
        + f"=== PERGUNTA ===\n{question}\n\n=== RESPOSTA ===")
    return gemini.generate([{"text": prompt}], model=model,
                           json_out=False, max_tokens=2048, temperature=0.2).strip()


# ── helpers ──────────────────────────────────────────────────────────────────
def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


def _parse_json(raw: str) -> dict:
    raw = _strip_fences(raw)
    try:
        return json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            return json.loads(raw[start:end + 1])
        raise ValueError("Gemini não retornou JSON válido.")


def _repair_truncated(raw: str):
    """Salvage a JSON object that was cut off mid-stream (model hit the token
    limit): cut back to the last complete '}' and re-balance the open braces /
    brackets so at least the fields parsed so far are usable. Returns a dict with
    a non-empty 'fields', or None if nothing usable can be recovered."""
    s = _strip_fences(raw)
    cut = s.rfind("}")
    if cut == -1:
        return None
    s = s[:cut + 1]
    stack, in_str, esc = [], False, False
    for ch in s:
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch in "{[":
                stack.append(ch)
            elif ch == "}" and stack and stack[-1] == "{":
                stack.pop()
            elif ch == "]" and stack and stack[-1] == "[":
                stack.pop()
    closing = "".join("}" if c == "{" else "]" for c in reversed(stack))
    try:
        data = json.loads(s + closing)
    except ValueError:
        return None
    return data if data.get("fields") else None
