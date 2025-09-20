#!/usr/bin/env python3
import argparse
import datetime
import sys
import json
import re
import time

import meshtastic
from meshtastic import tcp_interface


def human_time(ts: int) -> str:
    try:
        if ts is None or ts == 0:
            return "-"
        return datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return str(ts)


def flatten_dict(d, parent_key='', sep='_'):
    """Appiattisce un dizionario annidato in chiavi semplici"""
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        elif isinstance(v, (list, tuple)):
            items.append((new_key, json.dumps(v, ensure_ascii=False)))
        else:
            items.append((new_key, v))
    return dict(items)


def clean_name(name: str) -> str:
    """Normalizza il campo name con lunghezza fissa 35 caratteri"""
    if not name:
        return "".ljust(35)
    # rimuove caratteri non alfanumerici, lascia spazio, underscore o trattino e punto
    name = re.sub(r'[^a-zA-Z0-9 _\.-]', '', name)
    # rimuove spazi iniziali/finali
    name = name.strip()
    # tronca o riempie per arrivare a 35 caratteri fissi
    if len(name) > 35:
        name = name[:35]
    return name.ljust(35)


def normalize_node(node):
    """Converte un nodo protobuf/dict in un dict serializzabile e leggibile"""
    if hasattr(node, "to_dict"):
        d = node.to_dict()
    else:
        try:
            d = dict(node)
        except Exception:
            d = {}
            for attr in dir(node):
                if attr.startswith("_"):
                    continue
                try:
                    val = getattr(node, attr)
                    if callable(val):
                        continue
                    d[attr] = val
                except Exception:
                    pass

    flat = flatten_dict(d)

    # lastHeard
    if "lastHeard" in d:
        flat["lastHeard_human"] = human_time(d.get("lastHeard"))
        tmp = flat["lastHeard_human"].split()
        flat["lastHeard_human"] = tmp[1] + " " + tmp[0]

    # nome utente / descrizione
    if "user" in d:
        u = d["user"]
        if isinstance(u, dict):
            name = u.get("longName") or u.get("shortName") or ""
        else:
            name = getattr(u, "longName", None) or getattr(u, "shortName", None) or ""
        flat["name"] = clean_name(name)
    else:
        flat["name"] = "".ljust(35)

    # viaHop basato su hopsAway
    hops = flat.get("hopsAway")
    try:
        hops_int = int(hops) if hops is not None else 0
    except Exception:
        hops_int = 0
    flat["viaHop"] = True if hops_int > 0 else False

    # snr con due decimali
    if "snr" in flat:
        try:
            flat["snr"] = f"{float(flat['snr']):.2f}"
        except Exception:
            flat["snr"] = ""

    return flat


def print_table(nodes, compact=True, sort_hop=False):
    rows = []
    headers = set()

    for node in nodes.values():
        flat = normalize_node(node)
        rows.append(flat)
        headers.update(flat.keys())

    if sort_hop:
        def hop_key(r):
            try:
                return int(r.get("hopsAway", 0))
            except Exception:
                return 0
        rows.sort(key=hop_key)
    else:
        # ordinamento di default per lastHeard_human (decrescente = più recenti in alto)
        def ts_key(r):
            try:
                # ricostruisco il timestamp originale se presente
                val = r.get("lastHeard_human", "-")
                if val == "-" or not val.strip():
                    return 0
                # invertiamo giorno/mese per parsing sicuro
                # formato: "HH:MM:SS dd/mm/YYYY"
                return datetime.datetime.strptime(val, "%H:%M:%S %d/%m/%Y").timestamp()
            except Exception:
                return 0
        rows.sort(key=ts_key, reverse=True)

    if compact:
        headers = [
            "#",  # nuova colonna progressiva
            "user_id",
            "user_hwModel",
            "name",
            "hopsAway",
            "snr",
            "position_latitude",
            "position_longitude",
            "lastHeard_human",
        ]
    else:
        headers = ["#"] + sorted(headers)

    # calcolo larghezza colonne
    col_widths = {}
    for h in headers:
        if h == "#":
            col_widths[h] = 5
        elif h == "name":
            col_widths[h] = 35  # fisso a 35
        else:
            maxlen = max((len(str(r.get(h, ""))) for r in rows), default=0)
            header_len = len(h)
            col_widths[h] = max(maxlen, header_len) + 2

    # intestazione
    print("")
    header_line = "".join(f"{h:<{col_widths[h]}}" for h in headers)
    print(header_line)
    print("=" * len(header_line))

    # righe
    for idx, r in enumerate(rows, start=1):
        line_parts = []
        for h in headers:
            if h == "#":
                val = f"{idx:03d}"
                line_parts.append(f"{val:<{col_widths[h]}}")
            elif h == "snr" and r.get(h, "") != "":
                val = str(r[h]) + "  "  # due spazi extra
                line_parts.append(f"{val:<{col_widths[h]}}")
            else:
                val = str(r.get(h, ""))
                line_parts.append(f"{val:<{col_widths[h]}}")

        line = "".join(line_parts)

        # Sfondo verde chiaro se hopsAway=0 e snr presente
        if str(r.get("hopsAway", "0")) == "0" and r.get("snr", "") != "":
            print(f"\033[42m\033[97m{line}\033[0m")
        else:
            print(line)


def main():
    parser = argparse.ArgumentParser(description="Interroga una scheda LoRa Meshtastic via TCP e mostra la lista dei nodi v.160925-IK5XMK")
    parser.add_argument("--host", help="Hostname/IP del nodo Meshtastic", required=True)
    parser.add_argument("--port", type=int, default=4403, help="Porta TCP (default 4403)")
    #parser.add_argument("--no-compact", action="store_true", help="Mostra una tabella completa invece che compatta")
    parser.add_argument("--sort-hop", action="store_true", help="Ordina le righe per hopsAway")
    args = parser.parse_args()

    iface = None
    try:
        print(f"Connessione TCP a {args.host}:{args.port}")
        iface = tcp_interface.TCPInterface(hostname=args.host, portNumber=args.port)
        print("Connesso!")
        # attesa per permettere il download completo della lista nodi
        time.sleep(3)

        # Usa nodesByNum che contiene tutti i nodi, non solo i primi 100
        nodes = getattr(iface, "nodesByNum", None)
        if not nodes:
            print("Errore: impossibile recuperare i nodi", file=sys.stderr)
            iface.close()
            sys.exit(1)

        # togliere se vogliamo gestire l'argomento --no-compact
        args.no_compact = False
        
        print_table(nodes, compact=not args.no_compact, sort_hop=args.sort_hop)

        iface.close()

    except Exception as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        if iface:
            try:
                iface.close()
            except:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
