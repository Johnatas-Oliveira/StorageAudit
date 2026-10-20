#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auditoria de Armazenamento Universal — Windows / Windows Server (Python 3.9+).

Sem bibliotecas externas. Varredura somente de leitura; grava apenas relatórios
HTML/CSV e o histórico SQLite no local configurado, nunca no volume analisado
(salvo se o usuário indicar explicitamente esse local).

Exemplos:
  python auditoria_armazenamento_universal.py
  python auditoria_armazenamento_universal.py --listar-unidades
  python auditoria_armazenamento_universal.py "D:\\"
  python auditoria_armazenamento_universal.py "D:\\Dados" --top 20 --csv
  python auditoria_armazenamento_universal.py "\\\\servidor\\compartilhamento" --limite-html 0
  python auditoria_armazenamento_universal.py --raiz "D:\\" --historico "C:\\Relatorios\\historico.db"

Notas:
  * Lista volumes MONTADOS (letras de unidade); partições não montadas não são
    acessíveis para a análise. Caminhos UNC continuam aceitos.
  * Totais de diretórios são inclusivos; rankings de pais e filhos se sobrepõem.
  * Arquivos movidos/renomeados são vistos como remoção + inclusão.
  * Erros de permissão tornam as comparações potencialmente incompletas.
  * Tamanho representa tamanho lógico, não espaço físico alocado em clusters.
  * Links simbólicos/junctions/reparse points não são seguidos.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import html
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPARSE_POINT = 0x400


def human_size(n):
    n = float(n or 0)
    sign = "−" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{sign}{n:.0f} {unit}" if unit == "B" else f"{sign}{n:.2f} {unit}"
        n /= 1024


def human_int(n):
    return f"{int(n or 0):,}".replace(",", ".")


def fmt_pct(n):
    return f"{n:.1f}%".replace(".", ",")


def esc(s):
    return html.escape(str(s), quote=True)


def path_extended(path):
    """Prefixo de caminho longo (somente Windows)."""
    path = os.path.abspath(path)
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def root_key(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def as_date(s):
    try:
        return datetime.fromisoformat(s).strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return str(s or "")


def volumes_montados():
    """Enumera letras de volume disponíveis no Windows, sem dependências."""
    if os.name != "nt":
        return [{"path": os.path.abspath(os.sep), "tipo": "sistema", "nome": "Raiz"}]
    kernel32 = ctypes.windll.kernel32
    kernel32.GetLogicalDrives.restype = ctypes.c_uint32
    mask = kernel32.GetLogicalDrives()
    tipos = {2: "removível", 3: "fixo", 4: "rede", 5: "óptico", 6: "RAM"}
    volumes = []
    for i in range(26):
        if not (mask & (1 << i)):
            continue
        volume = f"{chr(65 + i)}:\\"
        codigo = kernel32.GetDriveTypeW(ctypes.c_wchar_p(volume))
        if codigo not in tipos:
            continue
        nome = ctypes.create_unicode_buffer(260)
        serial = ctypes.c_uint32()
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(volume), nome, len(nome), ctypes.byref(serial),
            None, None, None, 0
        )
        volumes.append({"path": volume, "tipo": tipos[codigo],
                        "nome": nome.value if ok else "", "serial": f"{serial.value:08X}" if ok else ""})
    return volumes


def info_uso(path):
    try:
        uso = shutil.disk_usage(path)
        return int(uso.total), int(uso.used), int(uso.free)
    except (OSError, ValueError):
        return None, None, None


def escolher_raiz():
    """O usuário escolhe uma unidade descoberta ou informa qualquer caminho.

    Nenhum endereço, compartilhamento ou unidade é configurado como origem padrão.
    """
    while True:
        unidades = volumes_montados()
        print("\n=== AUDITORIA DE ARMAZENAMENTO | SELEÇÃO DA ORIGEM ===")
        if unidades:
            print("Unidades acessíveis identificadas nesta máquina:")
            for i, u in enumerate(unidades, 1):
                total, used, free = info_uso(u["path"])
                espaco = (f"{human_size(total)} total · {human_size(free)} livre"
                          if total is not None else "capacidade indisponível")
                print(f"  {i:>2}. {u['path']:<7} {u['nome'] or '(sem rótulo)'} | {u['tipo']} | {espaco}")
        else:
            print("Nenhuma unidade montada foi identificada.")
        print("   I. Informar um caminho local ou endereço de rede manualmente")
        print("   A. Atualizar a lista de unidades")
        print("   S. Sair")
        try:
            opcao = input("\nEscolha uma unidade ou opção: ").strip()
        except EOFError:
            raise SystemExit("Entrada interativa indisponível. Informe o caminho com --raiz.")
        if opcao.isdecimal() and 1 <= int(opcao) <= len(unidades):
            candidato = unidades[int(opcao) - 1]["path"]
        elif opcao.casefold() in ("i", "o"):
            try:
                candidato = input("Informe o caminho completo (local ou UNC): ").strip().strip('"')
            except EOFError:
                raise SystemExit("Caminho não informado. Informe o caminho com --raiz.")
            if not candidato:
                print("Nenhum caminho informado. Selecione novamente.")
                continue
        elif opcao.casefold() == "a":
            continue
        elif opcao.casefold() == "s":
            raise SystemExit(0)
        else:
            # Permite também colar diretamente um caminho no menu.
            candidato = opcao.strip('"')
            if not candidato:
                print("Opção inválida. Selecione novamente.")
                continue
        candidato = os.path.abspath(os.path.expanduser(candidato))
        if not os.path.isdir(path_extended(candidato)):
            print(f"Caminho inexistente ou sem permissão de leitura: {candidato}")
            print("Verifique o endereço e suas credenciais antes de tentar novamente.")
            continue
        print(f"\nOrigem selecionada: {candidato}")
        return candidato


def conectar_db(db_path):
    p = Path(db_path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=60)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root TEXT NOT NULL, label TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT,
            duration_sec REAL, total_bytes INTEGER DEFAULT 0,
            file_count INTEGER DEFAULT 0, dir_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0, skipped_links INTEGER DEFAULT 0,
            volume_total INTEGER, volume_used INTEGER, volume_free INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_snap_root ON snapshots (root, id DESC);
        CREATE TABLE IF NOT EXISTS entries (
            snapshot_id INTEGER NOT NULL,
            path TEXT NOT NULL, parent TEXT, name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('d', 'f')),
            own_bytes INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            own_files INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            dir_count INTEGER NOT NULL DEFAULT 0,
            mtime REAL, depth INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, path)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_ent_rank ON entries (snapshot_id, kind, total_bytes DESC);
        CREATE INDEX IF NOT EXISTS idx_ent_parent ON entries (snapshot_id, parent, total_bytes DESC);
        CREATE TABLE IF NOT EXISTS scan_errors (
            snapshot_id INTEGER NOT NULL, path TEXT NOT NULL, message TEXT NOT NULL
        );
    """)
    return con


INSERT_ENTRY = """INSERT INTO entries
(snapshot_id, path, parent, name, kind, own_bytes, total_bytes, own_files, file_count, dir_count, mtime, depth)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""


def executar_scan(con, raiz, total_vol, used_vol, free_vol, silencioso=False, ignorados=None):
    """DFS iterativa; evita retenção de toda a árvore na memória Python."""
    inicio = time.monotonic()
    inicio_data = datetime.now().astimezone().isoformat(timespec="seconds")
    label = os.path.abspath(raiz)
    root = root_key(raiz)
    raiz_ext = path_extended(raiz)
    anterior = con.execute(
        "SELECT * FROM snapshots WHERE root = ? ORDER BY id DESC LIMIT 1", (root,)
    ).fetchone()
    con.execute("BEGIN")
    snap = con.execute(
        "INSERT INTO snapshots (root,label,started_at,volume_total,volume_used,volume_free) VALUES (?,?,?,?,?,?)",
        (root, label, inicio_data, total_vol, used_vol, free_vol),
    ).lastrowid
    erros = 0
    pulados = 0
    arquivos_vistos = 0
    ignorados = ignorados or set()
    lote = []

    def salvar(val):
        lote.append(val)
        if len(lote) >= 1000:
            con.executemany(INSERT_ENTRY, lote)
            lote.clear()

    def erro(path, ex):
        nonlocal erros
        erros += 1
        con.execute("INSERT INTO scan_errors VALUES (?,?,?)", (snap, path, str(ex)[:1000]))

    def criar_contexto(caminho, rel, pai, depth, name, mtime=None):
        try:
            it = os.scandir(caminho)
        except OSError as ex:
            erro(rel, ex)
            it = iter(())
        return {"it": it, "rel": rel, "pai": pai, "depth": depth, "nome": name,
                "mtime": mtime, "own": 0, "ownfiles": 0,
                "size": 0, "files": 0, "dirs": 0}

    stack = [criar_contexto(raiz_ext, ".", None, 0, os.path.basename(os.path.normpath(label)) or label)]
    try:
        while stack:
            atual = stack[-1]
            try:
                item = next(atual["it"])
            except StopIteration:
                if hasattr(atual["it"], "close"):
                    atual["it"].close()
                tamanho = atual["size"] + atual["own"]
                nfiles = atual["files"] + atual["ownfiles"]
                salvar((snap, atual["rel"], atual["pai"], atual["nome"], "d", atual["own"],
                        tamanho, atual["ownfiles"], nfiles, atual["dirs"], atual["mtime"], atual["depth"]))
                stack.pop()
                if stack:
                    parent = stack[-1]
                    parent["size"] += tamanho
                    parent["files"] += nfiles
                    parent["dirs"] += 1 + atual["dirs"]
                else:
                    total_bytes, n_arquivos, n_pastas = tamanho, nfiles, atual["dirs"]
                continue
            except OSError as ex:
                erro(atual["rel"], ex)
                if hasattr(atual["it"], "close"):
                    atual["it"].close()
                atual["it"] = iter(())
                continue
            subpath = item.name if atual["rel"] == "." else atual["rel"] + "/" + item.name
            # Evita contar o próprio banco de histórico e o relatório da execução.
            if ignorados and root_key(item.path.replace("\\\\?\\UNC\\", "\\\\").replace("\\\\?\\", "")) in ignorados:
                continue
            try:
                st = item.stat(follow_symlinks=False)
                if item.is_symlink() or (getattr(st, "st_file_attributes", 0) & REPARSE_POINT):
                    pulados += 1
                    continue
                if item.is_dir(follow_symlinks=False):
                    stack.append(criar_contexto(item.path, subpath, atual["rel"], atual["depth"] + 1,
                                                item.name, st.st_mtime))
                elif item.is_file(follow_symlinks=False):
                    size = int(st.st_size)
                    atual["own"] += size
                    atual["ownfiles"] += 1
                    arquivos_vistos += 1
                    salvar((snap, subpath, atual["rel"], item.name, "f", size, size,
                            1, 1, 0, st.st_mtime, atual["depth"] + 1))
                    if not silencioso and arquivos_vistos % 25000 == 0:
                        print(f"  {human_int(arquivos_vistos)} arquivos varridos...", flush=True)
            except OSError as ex:
                erro(subpath, ex)
        if lote:
            con.executemany(INSERT_ENTRY, lote)
        duracao = time.monotonic() - inicio
        con.execute("""UPDATE snapshots SET finished_at=?, duration_sec=?,total_bytes=?,
            file_count=?,dir_count=?,error_count=?,skipped_links=? WHERE id=?""",
            (datetime.now().astimezone().isoformat(timespec="seconds"), duracao,
             total_bytes, n_arquivos, n_pastas, erros, pulados, snap))
        con.commit()
        return snap, anterior
    except BaseException:
        for ctx in stack:
            if hasattr(ctx["it"], "close"):
                ctx["it"].close()
        con.rollback()
        raise


def rows(con, sql, *params):
    return [dict(x) for x in con.execute(sql, params).fetchall()]


def coletar_metricas(con, snap_id, previo_id, top, limite_html):
    atual = dict(con.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone())
    previo_row = con.execute("SELECT * FROM snapshots WHERE id=?", (previo_id,)).fetchone() if previo_id else None
    previo = dict(previo_row) if previo_row else None
    folders = rows(con, """SELECT path,name,total_bytes,own_bytes,file_count,dir_count,depth
        FROM entries WHERE snapshot_id=? AND kind='d' AND path<>'.'
        ORDER BY total_bytes DESC,path LIMIT ?""", snap_id, top)
    files = rows(con, """SELECT path,name,total_bytes,mtime,depth
        FROM entries WHERE snapshot_id=? AND kind='f'
        ORDER BY total_bytes DESC,path LIMIT ?""", snap_id, top)
    topo = rows(con, """SELECT path,name,total_bytes,kind FROM entries
        WHERE snapshot_id=? AND parent='.' ORDER BY total_bytes DESC LIMIT 12""", snap_id)
    tendencia = rows(con, """SELECT id,finished_at,total_bytes,file_count,dir_count,error_count
        FROM snapshots WHERE root=? ORDER BY id DESC LIMIT 30""", atual["root"])
    tendencia.reverse()
    erros = rows(con, "SELECT path,message FROM scan_errors WHERE snapshot_id=? LIMIT 100", snap_id)
    total_folders = atual["dir_count"] + 1
    if limite_html == 0:
        max_files = None
    else:
        max_files = max(0, limite_html - total_folders)
    arvore = []
    for ent in con.execute("""SELECT e.path,e.parent,e.name,e.kind,e.own_bytes,e.total_bytes,e.own_files,
          e.file_count,e.dir_count,e.mtime,e.depth,p.total_bytes AS prev_bytes
        FROM entries e LEFT JOIN entries p ON p.snapshot_id=? AND p.path=e.path AND p.kind=e.kind
        WHERE e.snapshot_id=? AND e.kind='d' ORDER BY e.depth,e.path""", (previo_id, snap_id)):
        arvore.append([ent[c] for c in ("path", "parent", "name", "kind", "own_bytes", "total_bytes",
                                         "own_files", "file_count", "dir_count", "mtime", "depth", "prev_bytes")])
    sql_files = """SELECT e.path,e.parent,e.name,e.kind,e.own_bytes,e.total_bytes,e.own_files,
          e.file_count,e.dir_count,e.mtime,e.depth,p.total_bytes AS prev_bytes
        FROM entries e LEFT JOIN entries p ON p.snapshot_id=? AND p.path=e.path AND p.kind=e.kind
        WHERE e.snapshot_id=? AND e.kind='f' ORDER BY e.total_bytes DESC,e.path"""
    if max_files is not None:
        sql_files += " LIMIT ?"
        cursor = con.execute(sql_files, (previo_id, snap_id, max_files))
    else:
        cursor = con.execute(sql_files, (previo_id, snap_id))
    for ent in cursor:
        arvore.append([ent[c] for c in ("path", "parent", "name", "kind", "own_bytes", "total_bytes",
                                         "own_files", "file_count", "dir_count", "mtime", "depth", "prev_bytes")])
    arquivos_html = len(arvore) - total_folders
    diffs = None
    if previo:
        # Diferenciais por caminho, sem duplicar linhas. Use dados históricos completos do banco,
        # não apenas os itens que couberam no HTML.
        changes = """WITH mudanças AS (
            SELECT c.path AS path,c.name AS name,c.kind AS kind,
                   c.total_bytes AS agora,COALESCE(p.total_bytes,0) AS antes,
                   c.total_bytes-COALESCE(p.total_bytes,0) AS delta,
                   CASE WHEN p.path IS NULL THEN 'novo' ELSE 'existente' END AS status
              FROM entries c LEFT JOIN entries p ON p.snapshot_id=? AND p.path=c.path AND p.kind=c.kind
             WHERE c.snapshot_id=? AND c.path <> '.'
            UNION ALL
            SELECT p.path,p.name,p.kind,0,p.total_bytes,-p.total_bytes,'removido'
              FROM entries p LEFT JOIN entries c ON c.snapshot_id=? AND c.path=p.path AND c.kind=p.kind
             WHERE p.snapshot_id=? AND c.path IS NULL AND p.path <> '.'
            ) """
        def biggest(where, ascending=False):
            sorting = "ASC" if ascending else "DESC"
            return rows(con, changes + f" SELECT path,name,kind,agora,antes,delta,status FROM mudanças WHERE {where} "
                        f" ORDER BY delta {sorting},path LIMIT ?", previo_id, snap_id, snap_id, previo_id, top)
        file_stats = con.execute(changes + """ SELECT
            SUM(CASE WHEN kind='f' AND status='novo' THEN 1 ELSE 0 END) AS novos,
            SUM(CASE WHEN kind='f' AND status='removido' THEN 1 ELSE 0 END) AS removidos,
            SUM(CASE WHEN kind='f' AND status='existente' AND delta>0 THEN 1 ELSE 0 END) AS aumentaram,
            SUM(CASE WHEN kind='f' AND status='existente' AND delta<0 THEN 1 ELSE 0 END) AS diminuiram
            FROM mudanças""", (previo_id, snap_id, snap_id, previo_id)).fetchone()
        diffs = {
            "folder_plus": biggest("kind='d' AND delta>0"),
            "folder_minus": biggest("kind='d' AND delta<0", ascending=True),
            "file_plus": biggest("kind='f' AND delta>0"),
            "file_minus": biggest("kind='f' AND delta<0", ascending=True),
            "stats": dict(file_stats),
            "before": previo,
            "delta": atual["total_bytes"] - previo["total_bytes"]
        }
    return {"now": atual, "before": previo, "folder_rank": folders, "file_rank": files,
            "top_level": topo, "history": tendencia, "errors": erros, "tree": arvore,
            "tree_files": arquivos_html, "diff": diffs}


def pill_delta(d):
    sign = "+" if d > 0 else ""
    css = "up" if d > 0 else "down" if d < 0 else "flat"
    return f'<span class="delta {css}">{sign}{esc(human_size(d))}</span>'


def stat_card(label, value, detail="", tag=""):
    return (f'<div class="stat"><span class="kicker">{esc(label)}</span>'
            f'<strong>{esc(value)}</strong><small>{esc(detail)}</small>{tag}</div>')


def ranking_table(registros, total, modo="storage", max_reference=None):
    if not registros:
        return '<div class="empty">Não há registros para apresentar.</div>'
    maior = max_reference if max_reference is not None else max(abs(r["total_bytes"]) for r in registros)
    maior = max(1, maior)
    out = ['<div class="rank-list">']
    for i, x in enumerate(registros, 1):
        n = x["delta"] if modo == "delta" else x["total_bytes"]
        nome = x["path"]
        width = max(1.5, min(100, abs(n) / maior * 100))
        frac = (abs(n) / total * 100) if total else 0
        kind = x.get("kind", "d" if "dir_count" in x else "f")
        color = "bar-negative" if n < 0 else "bar-positive" if modo == "delta" else ""
        icon = "📁" if kind == "d" else "📄"
        val = pill_delta(n) if modo == "delta" else esc(human_size(n))
        linkpath = esc(nome)
        out.append(f'<div class="rank-row"><span class="rank-n">{i:02d}</span>'
                   f'<div class="rank-main"><button type="button" class="open-node" data-path="{linkpath}">'
                   f'{icon} {esc(nome)}</button><div class="track"><div class="bar {color}" style="width:{width:.3f}%"></div></div></div>'
                   f'<div class="rank-num">{val}<small>{fmt_pct(frac)} do total</small></div></div>')
    out.append('</div>')
    return "".join(out)


def chart_history(history):
    """Gráfico SVG offline (sem bibliotecas ou internet)."""
    if not history:
        return '<div class="empty">Sem capturas no histórico.</div>'
    w, h = 940, 270
    l, r, t, b = 88, 24, 24, 53
    ys = [int(x["total_bytes"]) for x in history]
    low = min(ys)
    high = max(ys)
    span = high - low
    pad = max(span * .15, high * .04, 1024)
    low = max(0, low - pad)
    high += pad
    points = []
    for i, val in enumerate(ys):
        x = l + (w - l - r) * (i / max(len(ys)-1, 1))
        y = t + (h - t - b) * (1 - (val - low) / (high-low))
        points.append((x, y))
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Evolução do tamanho analisado" class="history-svg">']
    for k in range(5):
        val = low + (high-low) * k / 4
        yy = t + (h-t-b) * (1 - k/4)
        out.append(f'<line x1="{l}" y1="{yy:.1f}" x2="{w-r}" y2="{yy:.1f}" class="gridline"/>')
        out.append(f'<text x="{l-10}" y="{yy+4:.1f}" class="axis" text-anchor="end">{esc(human_size(val))}</text>')
    pathline = " ".join(f"{x:.1f},{y:.1f}" for x,y in points)
    first = points[0]
    last = points[-1]
    poly = f"{first[0]:.1f},{h-b} " + pathline + f" {last[0]:.1f},{h-b}"
    out.append(f'<polygon points="{poly}" fill="#d6eff2" opacity=".65"/>')
    out.append(f'<polyline points="{pathline}" fill="none" stroke="#008a9b" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')
    for i, ((x, y), item) in enumerate(zip(points, history)):
        legend = f"{as_date(item['finished_at'])} • {human_size(item['total_bytes'])}"
        if len(history) <= 8 or i in (0,len(history)-1) or i % max(1,len(history)//6)==0:
            out.append(f'<text x="{x:.1f}" y="{h-22}" text-anchor="middle" class="axis">{esc(as_date(item["finished_at"]).split(" ")[0])}</text>')
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#008a9b"><title>{esc(legend)}</title></circle>')
    out.append('</svg>')
    return "".join(out)


def cambios_pane(d, total, titulo, positive=True):
    records = d or []
    return (f'<div class="inner-card"><h3>{esc(titulo)}</h3>'
            + ranking_table(records, total, "delta", max_reference=max((abs(x["delta"]) for x in records), default=1))
            + '</div>')


CSS = r"""
:root{--navy:#12253c;--navy2:#213e5d;--teal:#008a9b;--ink:#172738;--muted:#647487;
  --line:#e3eaf0;--bg:#f4f7fa;--card:#fff;--green:#067d56;--red:#ba4f50}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 'Segoe UI',Inter,Arial,sans-serif}button,input,select{font:inherit}
.top{background:linear-gradient(110deg,#142b44,#234868);color:#fff;padding:40px max(24px,calc((100vw - 1280px)/2));}
.brand{font-size:11px;letter-spacing:.23em;font-weight:800;color:#87d2da;text-transform:uppercase}
h1{font-size:clamp(24px,3vw,37px);line-height:1.2;margin:12px 0 8px;font-weight:750;letter-spacing:-.03em}
.top p{color:#d0dfeb;margin:0;overflow-wrap:anywhere}.shell{max-width:1330px;margin:auto;padding:22px 24px 55px}
.note{background:#eaf5f6;border:1px solid #bedde0;color:#1c5260;padding:13px 18px;border-radius:12px;margin:16px 0}
.note.warn{background:#fff7eb;border-color:#f1d4a1;color:#88531d}
.section-title{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin:32px 0 12px}
.section-title h2{font-size:20px;letter-spacing:-.025em;margin:0}.section-title p{color:var(--muted);margin:0;font-size:12px}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-top:20px}
.stat{background:#fff;border:1px solid var(--line);border-radius:13px;padding:18px;min-height:122px;
 box-shadow:0 4px 18px rgba(31,56,81,.035)}.stat strong{display:block;font-size:clamp(19px,2.5vw,29px);margin:9px 0 0;color:var(--navy);line-height:1.2}
.stat small{display:block;color:var(--muted);font-size:12px;margin-top:6px}
.kicker{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:#637487;font-weight:800}
.card{background:#fff;border:1px solid var(--line);border-radius:13px;box-shadow:0 4px 18px rgba(31,56,81,.035);padding:22px;min-width:0}
.card h3,.inner-card h3{font-size:15px;margin:0 0 17px;color:var(--navy)}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.gridwide{display:grid;grid-template-columns:1.3fr .7fr;gap:16px}
.rank-list{display:flex;flex-direction:column}.rank-row{display:grid;grid-template-columns:27px minmax(0,1fr) 122px;gap:12px;
 border-top:1px solid #eef2f6;align-items:center;padding:11px 0}.rank-row:first-child{border:0}
.rank-n{color:#94a2ae;font-weight:700}.rank-main{min-width:0}.open-node{border:0;background:none;color:#203c5a;
 cursor:pointer;text-align:left;display:block;max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
 padding:0;font-weight:600;font-size:12px}.open-node:hover{color:var(--teal);text-decoration:underline}
.track{height:6px;background:#e9eff3;border-radius:99px;overflow:hidden;margin-top:8px}
.bar{height:100%;background:linear-gradient(90deg,#008a9b,#5cbfc7);border-radius:99px}
.bar-negative{background:linear-gradient(90deg,#cf7777,#e6aaaa)}.bar-positive{background:linear-gradient(90deg,#279779,#75c4a6)}
.rank-num{text-align:right;font-weight:700;white-space:nowrap;font-size:12px}.rank-num small{display:block;font-size:10px;color:var(--muted);font-weight:400}
.delta{font-size:12px;font-weight:750}.delta.up{color:var(--green)}.delta.down{color:var(--red)}
.history-svg{width:100%;max-height:290px;display:block}.axis{font-size:12px;fill:#738497}.gridline{stroke:#e9eff3;stroke-width:1}
.empty{color:var(--muted);padding:16px 0}.chip{display:inline-block;border-radius:8px;padding:4px 9px;background:#edf5fa;color:#325a74;font-size:11px}
.scope{border-radius:13px;border:1px solid var(--line);padding:22px;background:#fff}
.scope-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;flex-wrap:wrap}
.crumb{display:flex;gap:5px;flex-wrap:wrap}.crumb button{border:0;border-radius:7px;padding:4px 7px;background:#f0f6f8;
 cursor:pointer;color:#176579;font-size:12px}.crumb button:hover{background:#d9edf0}
.scope h3{margin:14px 0 5px;font-size:23px;overflow-wrap:anywhere;color:#193a54}
.scope-meta{color:#617587;font-size:12px}.folder-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:18px 0}
.folder-stats>div{background:#f3f7fa;padding:11px;border-radius:8px}.folder-stats strong{display:block;font-size:18px;color:#1b3d57}
.folder-stats span{display:block;font-size:11px;color:#728090;text-transform:uppercase;letter-spacing:.03em}
.controls{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:16px 0}
.controls input{border:1px solid #cbd8e2;border-radius:8px;padding:9px 11px;min-width:180px;flex:1;outline-color:var(--teal)}
.controls select,.controls button,.pager button{border:1px solid #d2dfe7;border-radius:8px;background:#fff;padding:9px 12px;cursor:pointer;color:#25445e}
.controls button{background:#ebf5f6}.tablewrap{width:100%;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:12px}th{color:#6e8091;text-transform:uppercase;letter-spacing:.05em;
 font-weight:700;font-size:10px;text-align:left;border-bottom:2px solid #e8eef2;padding:11px}
td{padding:10px 11px;border-bottom:1px solid #edf1f5}td.num,th.num{text-align:right;white-space:nowrap}
.item-link{border:0;background:none;color:#23516d;cursor:pointer;text-align:left;padding:0;max-width:560px;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block;font-size:12px}
.item-link:hover{text-decoration:underline;color:var(--teal)}.pager{display:flex;justify-content:space-between;align-items:center;
 color:var(--muted);font-size:12px;margin-top:15px;gap:10px}.pager button:disabled{opacity:.4;cursor:default}
.inner-card{min-width:0}.footer{font-size:12px;color:var(--muted);padding-top:28px}.dot{height:9px;width:9px;background:#008a9b;border-radius:50%;display:inline-block}
.usagebar{background:#e8f0f3;height:12px;border-radius:12px;overflow:hidden;margin:18px 0}.usagebar i{height:100%;display:block;background:#2f8094}
.distribution .dist{margin:14px 0}.distribution .dist-head{display:flex;gap:10px;justify-content:space-between;font-size:12px}
.search-result{display:flex;justify-content:space-between;gap:15px;border-top:1px solid #edf1f5;padding:8px 0;align-items:center}
.search-result:first-child{border:0}.search-result small{color:var(--muted);white-space:nowrap}
.search-result button{border:0;background:none;text-align:left;cursor:pointer;color:#1f5b76;min-width:0;
 text-overflow:ellipsis;overflow:hidden;white-space:nowrap}.search-result button:hover{text-decoration:underline}
@media(max-width:940px){.stats{grid-template-columns:repeat(2,1fr)}.grid2,.gridwide{grid-template-columns:1fr}.folder-stats{grid-template-columns:repeat(2,1fr)}}
@media(max-width:570px){.stats{grid-template-columns:1fr}.shell{padding:14px}.top{padding:30px 18px}.card,.scope{padding:15px}.rank-row{grid-template-columns:21px minmax(0,1fr) 95px;gap:6px}}
@media print{.top{padding:16px;background:#142b44!important}.shell{padding:8px}.card{break-inside:avoid}.controls,.pager{display:none}}
"""


JS = r"""
const M = new Map(), kids = new Map();
for (const v of DETAIL) {
  const x = {path:v[0],parent:v[1],name:v[2],kind:v[3],own:v[4],size:v[5],ownfiles:v[6],files:v[7],dirs:v[8],mtime:v[9],depth:v[10],previous:v[11]};
  M.set(x.path,x);
  if(x.parent!==null){if(!kids.has(x.parent))kids.set(x.parent,[]);kids.get(x.parent).push(x)}
}
let selected = '.', page = 0, filter = 'all', q = '', order = 'size';
const $ = id=>document.getElementById(id);
function size(n){if(n===0)return '0 B';let v=Math.abs(n),i=0;const u=['B','KB','MB','GB','TB','PB'];while(v>=1024&&i<u.length-1){v/=1024;i++};return (n<0?'−':'')+v.toLocaleString('pt-BR',{maximumFractionDigits:i===0?0:2,minimumFractionDigits:i===0?0:2})+' '+u[i]}
function deltaSize(n){return (n>0?'+':'')+size(n)}
function dateTime(s){if(!s)return '—';return new Date(s*1000).toLocaleString('pt-BR')}
function el(tag, cls, val){let node=document.createElement(tag);if(cls)node.className=cls;if(val!==undefined)node.textContent=val;return node}
function choose(path){let x=M.get(path);
  if(!x && path!=='.'){
    const pos=path.lastIndexOf('/');path=pos<0?'.':path.slice(0,pos);x=M.get(path)
  }
  if(!x)return;
  if(x.kind==='f'){path=x.parent; x=M.get(path);$('localSearch').value='';q=''}
  if(!x)return;selected=path;page=0;render();document.getElementById('explorar').scrollIntoView({behavior:'smooth',block:'start'})
}
function render(){const current=M.get(selected)||M.get('.');if(!current)return;
  $('folderName').textContent='📁 '+current.name;
  $('folderPath').textContent=selected==='.'?ROOT_LABEL:ROOT_LABEL.replace(/[\\/]+$/,'')+' / '+selected;
  const stats=[['Tamanho total',size(current.size)],['Arquivos (incl. subpastas)',current.files.toLocaleString('pt-BR')],
               ['Subpastas',current.dirs.toLocaleString('pt-BR')],['Direto nesta pasta',size(current.own)]];
  if(HAS_PREVIOUS)stats.push(['Mudança vs. anterior',current.previous===null?'Pasta nova':deltaSize(current.size-current.previous)]);
  $('folderStats').replaceChildren(...stats.map(([t,v])=>{let d=el('div');d.append(el('span','',t),el('strong','',v));return d}));
  let crumbs=el('div','crumb');let root=el('button','', 'Início');root.onclick=()=>choose('.');crumbs.append(root);
  if(selected!=='.'){const parts=selected.split('/');let path='';for(let p of parts){path=path?path+'/'+p:p;const target=path;let b=el('button','',p);b.onclick=()=>choose(target);crumbs.append(b)}}
  $('breadcrumbs').replaceChildren(crumbs);
  const items=(kids.get(selected)||[]).filter(x=>(filter==='all'||(filter==='d'&&x.kind==='d')||(filter==='f'&&x.kind==='f'))&&x.name.toLocaleLowerCase().includes(q));
  items.sort((a,b)=>order==='name'?a.name.localeCompare(b.name,'pt-BR'):order==='date'?(b.mtime||0)-(a.mtime||0):(b.size-a.size||a.name.localeCompare(b.name)));
  const limit=50,maxPage=Math.max(0,Math.ceil(items.length/limit)-1);page=Math.min(page,maxPage);
  const tb=$('childRows');tb.replaceChildren();for(const x of items.slice(page*limit,(page+1)*limit)){
    const tr=el('tr'), a=el('button','item-link',(x.kind==='d'?'📁 ':'📄 ')+x.name);
    a.title=x.path;a.onclick=()=>x.kind==='d'?choose(x.path):showFile(x);
    const td=el('td');td.append(a);tr.append(td);
    const variation=!HAS_PREVIOUS?'—':x.previous===null?'Novo':deltaSize(x.size-x.previous);
    for(const [idx,val] of [x.kind==='d'?'Pasta':'Arquivo',size(x.size),variation,x.kind==='d'?x.files.toLocaleString('pt-BR')+' arquivos':'—',dateTime(x.mtime)].entries()){
      let cell=el('td',idx===1||idx===2?'num':'',val);
      if(idx===2&&HAS_PREVIOUS&&x.previous!==null){cell.style.color=x.size>x.previous?'#067d56':x.size<x.previous?'#ba4f50':''}
      tr.append(cell)
    }tb.append(tr)
  }
  $('shownCount').textContent=`${items.length.toLocaleString('pt-BR')} itens disponíveis no relatório · ${(page+1)} / ${maxPage+1} página(s)`;
  $('omittedNote').textContent=current.ownfiles > (kids.get(selected)||[]).filter(x=>x.kind==='f').length ?
    'Existem arquivos adicionais nesta pasta disponíveis no histórico SQLite. Gere com --limite-html 0 para incluir todos no HTML.' : '';
  $('btnPrev').disabled=page===0;$('btnNext').disabled=page>=maxPage;
  if(!items.length){const tr=el('tr'),td=el('td','', 'Nenhum item encontrado nesta pasta.');td.colSpan=6;tr.append(td);tb.append(tr)}
}
function showFile(x){$('fileInfo').textContent='Arquivo: '+x.path+' | Tamanho: '+size(x.size)+' | Modificado: '+dateTime(x.mtime);$('fileInfo').hidden=false;
  setTimeout(()=>{$('fileInfo').hidden=true},8500)}
function globalLookup(){
  const term=$('globalSearch').value.trim().toLocaleLowerCase();
  const res=$('globalResults');res.replaceChildren();
  if(term.length<2){res.append(el('div','empty','Digite pelo menos 2 caracteres para buscar em todo o detalhamento incluído no HTML.'));return}
  const found=[];let count=0;
  for(const x of M.values())if(x.path.toLocaleLowerCase().includes(term)){
    count++;if(found.length<30){found.push(x);found.sort((a,b)=>b.size-a.size)}
    else if(x.size>found[found.length-1].size){found[found.length-1]=x;found.sort((a,b)=>b.size-a.size)}
  }
  res.append(el('div','scope-meta',`${count.toLocaleString('pt-BR')} resultados · exibindo os 30 maiores (quando disponíveis)`));
  for(const x of found){const row=el('div','search-result');const btn=el('button','',(x.kind==='d'?'📁 ':'📄 ')+x.path);
    btn.title=x.path;btn.onclick=()=>choose(x.path);row.append(btn,el('small','',size(x.size)));res.append(row)}
  if(!count)res.append(el('div','empty','Nenhum item localizado.'))
}
$('globalSearch').addEventListener('input',globalLookup);
document.querySelectorAll('.open-node').forEach(b=>b.addEventListener('click',()=>choose(b.dataset.path)));
$('localSearch').addEventListener('input',e=>{q=e.target.value.toLocaleLowerCase();page=0;render()});
$('kindSelect').addEventListener('change',e=>{filter=e.target.value;page=0;render()});
$('sortSelect').addEventListener('change',e=>{order=e.target.value;page=0;render()});
$('btnPrev').addEventListener('click',()=>{page--;render()});
$('btnNext').addEventListener('click',()=>{page++;render()});
$('backRoot').addEventListener('click',()=>{q='';$('localSearch').value='';choose('.')});
render();
"""


def gerar_html(dados, top, limite_html):
    a = dados["now"]
    b = dados["before"]
    diff = dados["diff"]
    now_files = a["file_count"]
    total = a["total_bytes"]
    path = a["label"]
    sections = []
    sections.append('<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>Auditoria de armazenamento • {esc(path)}</title><style>{CSS}</style></head><body>')
    sections.append(f'<header class="top"><div class="brand">Inteligência de Armazenamento</div>'
                    f'<h1>Relatório de armazenamento</h1><p>{esc(path)} · '
                    f'Captura em {esc(as_date(a["finished_at"]))} · ID #{a["id"]} · '
                    f'Duração {a["duration_sec"]:.1f}s</p></header><main class="shell">')
    if a["error_count"] or b and b["error_count"]:
        sections.append(f'<div class="note warn"><strong>Atenção: varredura com restrições de acesso.</strong> '
                        f'Atual: {a["error_count"]} erro(s); anterior: {b["error_count"] if b else 0}. '
                        'Arquivos inacessíveis não são medidos. Mudanças aparentes podem resultar de permissões ou arquivos em uso.</div>')
    if a["skipped_links"]:
        sections.append(f'<div class="note">{human_int(a["skipped_links"])} links simbólicos/junctions ignorados para evitar dupla contagem.</div>')
    if len(dados["tree"]) < a["file_count"] + a["dir_count"] + 1:
        sections.append(f'<div class="note">Detalhamento HTML otimizado: {human_int(dados["tree_files"])} de '
                        f'{human_int(a["file_count"])} arquivos incluídos; <strong>100% das pastas, métricas e histórico</strong> '
                        'continuam armazenados no SQLite. Para incluir todos os arquivos no HTML, use <code>--limite-html 0</code>.</div>')
    sections.append('<div class="stats">')
    sections.append(stat_card('Dados encontrados',human_size(total),'Soma dos arquivos acessíveis'))
    sections.append(stat_card('Arquivos',human_int(now_files),'Contagem em todos os níveis'))
    sections.append(stat_card('Pastas e subpastas',human_int(a["dir_count"]),'Não inclui a própria raiz'))
    sections.append(stat_card('Variação desde a última coleta',
                              ('+' if diff["delta"]>0 else '')+human_size(diff["delta"]) if diff else 'Primeira coleta',
                              (fmt_pct(diff["delta"]/b["total_bytes"]*100) if b["total_bytes"] else 'Base anterior vazia')
                              if diff else 'Cria a linha de base'))
    sections.append('</div>')
    sections.append('<div class="section-title"><h2>Capacidade e evolução</h2><p>Dados medidos na raiz escolhida</p></div>')
    sections.append('<div class="gridwide"><section class="card"><h3>Histórico das análises</h3>'
                    +chart_history(dados["history"])+f'<small class="scope-meta">{len(dados["history"])} coleta(s) recentes exibidas. '
                    'Os dados completos permanecem no banco local.</small></section>')
    sections.append('<section class="card"><h3>Utilização do volume</h3>')
    vol = a["volume_total"]
    if vol and vol>0:
        sections.append(f'<strong style="font-size:29px">{esc(human_size(a["volume_used"]))}</strong>'
                        f'<span class="scope-meta"> ocupados de {esc(human_size(vol))} no volume</span>'
                        f'<div class="usagebar"><i style="width:{a["volume_used"]/vol*100:.3f}%"></i></div>'
                        f'<div class="scope-meta">Livre: <strong>{esc(human_size(a["volume_free"]))}</strong> '
                        f'({fmt_pct(a["volume_free"]/vol*100)})</div>'
                        '<p class="scope-meta">A capacidade do volume considera todo o espaço ocupado; '
                        'o total da varredura contabiliza apenas os arquivos acessíveis dentro do caminho selecionado.</p>')
    else:
        sections.append('<div class="empty">Capacidade do volume não disponível para este caminho de rede ou sistema.</div>')
    sections.append('</section></div>')
    sections.append('<div class="section-title"><h2>Distribuição do espaço</h2><p>Entradas diretamente na raiz analisada</p></div>')
    sections.append('<section class="card distribution">')
    if dados["top_level"]:
        for o in dados["top_level"]:
            frac = o["total_bytes"]/total*100 if total else 0
            sections.append(f'<div class="dist"><div class="dist-head"><span>{"📁" if o["kind"]=="d" else "📄"} '
                            f'{esc(o["name"])}</span><strong>{human_size(o["total_bytes"])} · {fmt_pct(frac)}</strong></div>'
                            f'<div class="track"><div class="bar" style="width:{frac:.3f}%"></div></div></div>')
    else:
        sections.append('<div class="empty">Raiz sem conteúdo encontrado.</div>')
    sections.append('</section>')
    sections.append(f'<div class="section-title"><h2>Ranking de consumo</h2><p>As {top} maiores entradas por categoria</p></div>')
    sections.append('<div class="grid2"><section class="card"><h3>📁 Pastas que mais ocupam espaço</h3>'
                    +ranking_table(dados["folder_rank"], total)
                    +'</section><section class="card"><h3>📄 Maiores arquivos individuais</h3>'
                    +ranking_table(dados["file_rank"], total)+'</section></div>')
    if diff:
        percent = (diff["delta"]/b["total_bytes"]*100) if b["total_bytes"] else None
        sections.append('<div class="section-title"><h2>Comparação histórica</h2>'
                        f'<p>{esc(as_date(b["finished_at"]))} → {esc(as_date(a["finished_at"]))}</p></div>')
        sections.append('<div class="stats">')
        s = diff["stats"]
        sections.append(stat_card('Arquivos novos',human_int(s["novos"]),'Identificados por caminho'))
        sections.append(stat_card('Arquivos removidos',human_int(s["removidos"]),'Ausentes na coleta atual'))
        sections.append(stat_card('Arquivos que cresceram',human_int(s["aumentaram"]),'Mesmo caminho, tamanho maior'))
        sections.append(stat_card('Arquivos que diminuíram',human_int(s["diminuiram"]),'Mesmo caminho, tamanho menor'))
        sections.append('</div>')
        sections.append('<div class="grid2" style="margin-top:16px">')
        sections.append('<section class="card">'+cambios_pane(diff["folder_plus"],total,'📈 Pastas com maior crescimento')+'</section>')
        sections.append('<section class="card">'+cambios_pane(diff["folder_minus"],total,'📉 Pastas com maior redução')+'</section>')
        sections.append('<section class="card">'+cambios_pane(diff["file_plus"],total,'📄 Arquivos que mais aumentaram')+'</section>')
        sections.append('<section class="card">'+cambios_pane(diff["file_minus"],total,'📄 Arquivos que mais diminuíram / foram removidos')+'</section>')
        sections.append('</div>')
    else:
        sections.append('<div class="note" style="margin-top:23px">Primeiro levantamento registrado para este caminho. '
                        'Execute novamente no futuro para visualizar alterações, aumentos, reduções e gráficos comparativos.</div>')
    sections.append('<div class="section-title"><h2>Pesquisa geral de arquivos e pastas</h2>'
                    '<p>Localização por caminho em todo o conteúdo incorporado ao HTML</p></div>')
    sections.append('<section class="card"><div class="controls"><input type="search" id="globalSearch" '
                    'placeholder="Pesquise nome ou parte do caminho (ex.: Contratos)..." '
                    'aria-label="Buscar no relatório completo"></div><div id="globalResults" '
                    'class="scope-meta">Digite pelo menos 2 caracteres para pesquisar.</div></section>')
    sections.append('<div class="section-title" id="explorar"><h2>Explorador detalhado</h2>'
                    '<p>Pastas, subpastas, arquivos e metadados • navegação interativa</p></div>')
    sections.append('''<section class="scope">
        <div class="scope-head"><div id="breadcrumbs"></div><span class="chip">Somente leitura</span></div>
        <h3 id="folderName"></h3><div class="scope-meta" id="folderPath"></div>
        <div class="folder-stats" id="folderStats"></div>
        <div class="controls"><input type="search" id="localSearch" placeholder="Filtrar itens desta pasta..." aria-label="Filtrar conteúdo da pasta">
        <select id="kindSelect" aria-label="Tipo de item"><option value="all">Todos</option><option value="d">Somente pastas</option><option value="f">Somente arquivos</option></select>
        <select id="sortSelect" aria-label="Ordenar"><option value="size">Tamanho ↓</option><option value="name">Nome A–Z</option><option value="date">Última modificação</option></select>
        <button id="backRoot" type="button">↖ Voltar à raiz</button></div>
        <div id="fileInfo" hidden class="note"></div>
        <div class="tablewrap"><table><thead><tr><th>Nome</th><th>Tipo</th><th class="num">Tamanho (incl. subpastas)</th><th class="num">Δ anterior</th><th>Conteúdo</th><th>Modificado</th></tr></thead>
        <tbody id="childRows"></tbody></table></div>
        <div class="pager"><div><span id="shownCount"></span><div id="omittedNote"></div></div>
        <div><button id="btnPrev" type="button">← Anterior</button> <button id="btnNext" type="button">Próxima →</button></div></div>
        </section>''')
    if dados["errors"]:
        sections.append(f'<div class="section-title"><h2>Erros de acesso</h2><p>Exibindo até 100 de {a["error_count"]} ocorrências</p></div>'
                        '<section class="card"><div class="tablewrap"><table><thead><tr><th>Caminho</th><th>Motivo</th></tr></thead><tbody>')
        for e in dados["errors"]:
            sections.append(f'<tr><td>{esc(e["path"])}</td><td>{esc(e["message"])}</td></tr>')
        sections.append('</tbody></table></div></section>')
    sections.append('<div class="footer">Auditoria• Relatório gerado localmente • Dados históricos preservados em SQLite. '
                    'Totais de pastas incluem descendentes; não some rankings de pai e filho. '
                    'Mudanças de caminho são tratadas como arquivos novos/removidos. </div></main>')
    # Escapa sequências de fechamento HTML para incorporar JSON seguramente num único arquivo.
    raw = json.dumps(dados["tree"], ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    root_js = json.dumps(path, ensure_ascii=False).replace('<', '\\u003c')
    sections.append('<script>const ROOT_LABEL='+root_js+';const HAS_PREVIOUS='+('true' if b else 'false')+';const DETAIL='+raw+';\n'+JS+'</script></body></html>')
    return ''.join(sections)


def exportar_csv(con, snap_id, nome_arquivo):
    dest = Path(nome_arquivo).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp, delimiter=";")
        writer.writerow(["Caminho relativo", "Tipo", "Tamanho próprio (bytes)", "Tamanho total (bytes)",
                         "Arquivos diretos", "Arquivos totais", "Subpastas totais", "Data modificação (local)", "Nível"])
        for ent in con.execute("""SELECT path,kind,own_bytes,total_bytes,own_files,file_count,dir_count,mtime,depth
            FROM entries WHERE snapshot_id=? ORDER BY path""", (snap_id,)):
            date = datetime.fromtimestamp(ent["mtime"]).strftime("%d/%m/%Y %H:%M:%S") if ent["mtime"] else ""
            writer.writerow([ent["path"],"Pasta" if ent["kind"]=="d" else "Arquivo",
                             ent["own_bytes"],ent["total_bytes"],ent["own_files"],ent["file_count"],
                             ent["dir_count"],date,ent["depth"]])
    return dest


def main():
    ap = argparse.ArgumentParser(description="Auditoria universal de armazenamento com histórico e relatório HTML.")
    ap.add_argument("raiz", nargs="?", help="Unidade, pasta local ou compartilhamento UNC escolhido pelo usuário (sem argumento = menu)")
    ap.add_argument("--raiz", dest="raiz_opcao", help="Alternativa explícita ao argumento posicional")
    ap.add_argument("--listar-unidades", action="store_true", help="Exibe volumes montados e encerra")
    ap.add_argument("--top", type=int, default=15, help="Quantidade de posições por ranking (padrão: 15)")
    ap.add_argument("--limite-html", type=int, default=120000,
                    help="Limite de registros na árvore HTML (pastas têm prioridade; 0 = todos, padrão: 120000)")
    ap.add_argument("--historico", default="historico_armazenamento.sqlite3", help="Arquivo persistente SQLite")
    ap.add_argument("--saida", help="Arquivo HTML (padrão: relatorio_armazenamento_YYYYMMDD_HHMMSS.html)")
    ap.add_argument("--csv", nargs="?", const="", default=None,
                    help="Opcional: exporta todos os itens como CSV, com nome opcional")
    ap.add_argument("--silencioso", action="store_true", help="Oculta progresso parcial")
    args = ap.parse_args()
    if args.top < 1 or args.limite_html < 0:
        ap.error("--top precisa ser >= 1; --limite-html precisa ser >= 0")
    if args.listar_unidades:
        for u in volumes_montados():
            total, used, free = info_uso(u["path"])
            print(f"{u['path']}  {u['tipo']:<9}  {u['nome']:<24}  Total: {human_size(total) if total else '?':<12}  Livre: {human_size(free) if free is not None else '?'}")
        return 0
    raiz = args.raiz_opcao or args.raiz or escolher_raiz()
    raiz = os.path.abspath(os.path.expanduser(raiz))
    if not os.path.isdir(path_extended(raiz)):
        ap.error(f"Caminho inexistente, inacessível ou sem permissão: {raiz}")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saida = Path(args.saida or f"relatorio_armazenamento_{ts}.html").expanduser().resolve()
    db_path = Path(args.historico).expanduser().resolve()
    if saida == db_path:
        ap.error("Relatório HTML e histórico SQLite não podem usar o mesmo caminho.")
    saida.parent.mkdir(parents=True, exist_ok=True)
    free_total, free_used, free_avail = info_uso(raiz)
    print(f"\nAnalisando: {raiz}")
    print(f"Histórico: {db_path}")
    con = conectar_db(db_path)
    ignorados = {root_key(str(db_path)), root_key(str(saida)),
                 root_key(str(db_path) + '-journal'), root_key(str(db_path) + '-wal'),
                 root_key(str(db_path) + '-shm')}
    if args.csv is not None:
        ignorados.add(root_key(args.csv or f"detalhamento_armazenamento_{ts}.csv"))
    try:
        current_id, previous = executar_scan(con, raiz, free_total, free_used, free_avail,
                                               args.silencioso, ignorados)
        print("Consolidando rankings e comparações...")
        dados = coletar_metricas(con, current_id, previous["id"] if previous else None,
                                 args.top, args.limite_html)
        saida.write_text(gerar_html(dados, args.top, args.limite_html), encoding="utf-8")
        print(f"Arquivos: {human_int(dados['now']['file_count'])} | Pastas: {human_int(dados['now']['dir_count'])}"
              f" | Dados: {human_size(dados['now']['total_bytes'])} | Erros: {dados['now']['error_count']}")
        if previous:
            delta = dados["diff"]["delta"]
            print(f"Variação vs. {as_date(previous['finished_at'])}: {'+' if delta>0 else ''}{human_size(delta)}")
        else:
            print("Primeira coleta desse caminho: histórico inicial criado.")
        print(f"Relatório HTML: {saida}")
        if args.csv is not None:
            csv_file = args.csv or f"detalhamento_armazenamento_{ts}.csv"
            dest = exportar_csv(con, current_id, csv_file)
            print(f"Relatório CSV: {dest}")
        print(f"Histórico SQLite: {db_path}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nOperação interrompida; a coleta incompleta não foi gravada.", file=sys.stderr)
        sys.exit(130)
    except (OSError, sqlite3.Error) as ex:
        print(f"Erro: {ex}", file=sys.stderr)
        sys.exit(1)
