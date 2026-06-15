# Conferência de Documentos (Document Comparator)

App de bandeja (Windows 11). Você **arrasta 2 ou mais PDFs do mesmo embarque**
(ex.: RASTREABILIDADE, ROMANEIO, rascunho de CSI/GTA, invoice, packing list,
booking) para a janela. O **Gemini** lê todos os documentos direto (sem extrair
texto), **alinha cada informação lado a lado** e **pinta de vermelho as linhas
que divergem** — para um humano conferir antes do embarque.

## O que faz
- **Matriz campo × documento**: cada linha é uma informação (contrato, peso
  líquido total, peso bruto, caixas, peças, container/lacre, SIF, importador,
  NCM, peso por produto…), cada coluna é um documento (D1, D2…).
- 🟥 divergem · 🟨 só alguns têm o campo · ⬜ batem. A célula "fora da curva"
  numa divergência ganha um ⚠ para o olho achar.
- **Clique numa linha** → painel de detalhe mostra o valor **e o trecho literal
  de origem em cada documento**, para você achar no PDF e conferir.
- **Resumo + alertas** do que conferir primeiro.
- **Perguntas**: caixa de chat para perguntar qualquer coisa sobre o conjunto
  ("por que o peso do CSI 2 está menor?", "qual produto falta?").

> Não escreve nada nas pastas de origem. Os PDFs são só lidos; o resultado
> fica na memória (re-arraste para refazer).

## Como rodar
- `run_tray.vbs` — sobe na bandeja, sem console.
- `Iniciar (com console).bat` — com console, para ver erros.
- Lote/teste: `python tray_app.py a.pdf b.pdf …` → imprime o JSON da comparação.

Menu da bandeja: **Mostrar janela · Iniciar com o Windows · Sair**.

## Chave do Gemini
Reutiliza a chave já existente em
um `config.json` ao lado do app (campo `gemini_api_key`, modelo `gemini-2.5-flash`),
ou a variável de ambiente `GEMINI_API_KEY`. Nenhuma chave fica no código.

## Dependências
`pip install -r requirements.txt` (pystray, Pillow, tkinterdnd2). Python 3.13.

## Notas
- Ler 5–6 PDFs leva ~1–4 min (o Gemini lê cada documento com cuidado). A janela
  mostra um cronômetro de "analisando…" enquanto trabalha.
- Funciona com qualquer mistura de documentos do mesmo embarque, não só os
  exemplos. Campos que naturalmente diferem (nº de página, datas de emissão de
  cada doc) não entram como divergência.
