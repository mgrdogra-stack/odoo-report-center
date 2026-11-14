import os
import re
import json
import time
import tempfile
import threading
import urllib.request
import http.cookiejar
from datetime import datetime
import xlsxwriter

STOP_EVENT = threading.Event()

def stop_generation():
    STOP_EVENT.set()
    return "🛑 Stop signal sent!"

def to_odoo_date(d_str):
    s = str(d_str or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"Invalid date format '{s}'. Use DD-MM-YYYY.")

def odoo_jsonrpc(url, service, method, args, timeout=60):
    base = url.strip().rstrip("/")
    endpoint = f"{base}/jsonrpc"
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": int(time.time() * 1000) % 1000000,
    }).encode("utf-8")
    req = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if "error" in res:
            err_data = res["error"].get("data", {})
            raise Exception(err_data.get("message") or res["error"].get("message") or "Odoo RPC Error")
        return res.get("result")

def execute_with_uid(url, db, uid, pw, model, method, args, kwargs=None, timeout=120):
    call_args = [db, uid, pw, model, method, args]
    if kwargs:
        call_args.append(kwargs)
    return odoo_jsonrpc(url, "object", "execute_kw", call_args, timeout=timeout)

def get_auth_opener(url, db, user, pw):
    base = url.strip().rstrip("/")
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    auth_payload = json.dumps({
        "jsonrpc": "2.0",
        "params": {"db": db, "login": user, "password": pw},
    }).encode("utf-8")
    req = urllib.request.Request(f"{base}/web/session/authenticate", data=auth_payload, headers={"Content-Type": "application/json"})
    opener.open(req, timeout=60)
    return opener, base

def connect_and_discover(url, db, user, pw):
    if not url.strip() or not db.strip() or not user.strip() or not pw.strip():
        return "⚠️ Please fill in URL, Database, Username, and Password.", list(), {}
    try:
        uid = odoo_jsonrpc(url, "common", "authenticate", [db.strip(), user.strip(), pw, {}], timeout=60)
        if not uid:
            return "❌ Authentication failed. Check your credentials.", list(), {}
        actions = execute_with_uid(
            url, db.strip(), uid, pw, "ir.actions.act_window", "search_read",
            [[["target", "=", "new"], ["res_model", "!=", False]]],
            {"fields": ["id", "name", "res_model"], "limit": 500},
            timeout=90
        )
        wiz_map = {}
        choices = list()
        for a in actions:
            m_name = str(a.get("res_model") or "")
            if any(k in m_name for k in ["report", "register", "ledger", "grir", "wizard", "excel", "statement", "trial"]):
                lbl = f"{a['name']} ({m_name})"
                wiz_map[lbl] = {"id": a["id"], "name": a["name"], "model": m_name}
                choices.append(lbl)
        choices.sort()
        return f"✅ Connected (UID: `{uid}`). Discovered **{len(choices)}** report wizards.", choices, wiz_map
    except Exception as e:
        return f"❌ Connection Error: {str(e)}", list(), {}

def inspect_wizard(url, db, user, pw, selected_label, wiz_map, d_from, d_to):
    if not selected_label or selected_label not in wiz_map:
        return "⚠️ Select a wizard first.", "{}", "action_print_report", False, list(), {}
    w = wiz_map[selected_label]
    try:
        uid = odoo_jsonrpc(url, "common", "authenticate", [db.strip(), user.strip(), pw, {}], timeout=60)
        f_info = execute_with_uid(url, db.strip(), uid, pw, w["model"], "fields_get", list(), {"attributes": ["string", "type", "relation"]}, timeout=60)
        defaults = {}
        for k, meta in f_info.items():
            if k in ("from_date", "date_from", "start_date") and d_from.strip():
                defaults[k] = to_odoo_date(d_from)
            elif k in ("to_date", "date_to", "end_date") and d_to.strip():
                defaults[k] = to_odoo_date(d_to)
            elif meta.get("type") == "boolean":
                defaults[k] = False

        acc_choices, acc_map = list(), {}
        accs = execute_with_uid(
            url, db.strip(), uid, pw, "account.account", "search_read",
            [list()], {"fields": ["id", "code", "name"], "order": "code asc", "limit": 1000}, timeout=60
        )
        for a in accs:
            lbl = f"{a['code']} {a['name']}"
            acc_choices.append(lbl)
            acc_map[lbl] = a["id"]

        btn_guess = "sale_register_report_excel" if "sale" in w["model"] else ("purchase_register_report_excel" if "purchase" in w["model"] else "action_print_report")
        return f"### ⚙️ Loaded: **{w['name']}** (`{w['model']}`)", json.dumps(defaults, indent=2), btn_guess, False, acc_choices, acc_map
    except Exception as e:
        return f"❌ Error inspecting wizard: {str(e)}", "{}", "action_print_report", False, list(), {}

def sync_ui_to_json(params_json, d_from, d_to, is_as_on, selected_accs, acc_map):
    try:
        data = json.loads(params_json) if params_json.strip() else {}
    except Exception:
        data = {}
    if d_from.strip():
        for k in ("from_date", "date_from", "start_date"):
            if k in data or k == "from_date":
                try:
                    data[k] = to_odoo_date(d_from)
                except Exception:
                    pass
    if d_to.strip():
        for k in ("to_date", "date_to", "end_date"):
            if k in data or k == "to_date":
                try:
                    data[k] = to_odoo_date(d_to)
                except Exception:
                    pass
    if selected_accs and acc_map:
        data["account_ids"] = [acc_map[a] for a in selected_accs if a in acc_map]
    return json.dumps(data, indent=2)

def rpc_fetch_all(url, db, uid, pw, model, domain, fields, batch_size=2500, progress=None, label="Fetching", p_start=0.1, p_end=0.65):
    total = execute_with_uid(url, db, uid, pw, model, "search_count", [domain], timeout=120)
    if not total:
        return list()
    out, offset = list(), 0
    while offset < total:
        if STOP_EVENT.is_set():
            break
        if progress:
            frac = p_start + (p_end - p_start) * (offset / max(total, 1))
            progress(frac, desc=f"{label}: {offset:,} / {total:,} records...")
        chunk = execute_with_uid(url, db, uid, pw, model, "search_read", [domain], {"fields": fields, "offset": offset, "limit": batch_size, "order": "id asc"}, timeout=180)
        if not chunk:
            break
        out.extend(chunk)
        offset += len(chunk)
    return out

def fetch_single_wizard_fast(url, db, uid, pw, opener, clean_base, w, vals, btn_method, user_ctx, timeout=900):
    wiz_id = execute_with_uid(url, db, uid, pw, w["model"], "create", [vals], timeout=60)
    act = execute_with_uid(url, db, uid, pw, w["model"], btn_method.strip() or "action_print_report", [[wiz_id]], {"context": user_ctx}, timeout=timeout)
    if isinstance(act, dict) and act.get("type") == "ir.actions.act_url" and act.get("url"):
        dl_url = f"{clean_base}{act['url']}" if act["url"].startswith("/") else act["url"]
        with opener.open(dl_url, timeout=timeout) as resp:
            return resp.read(), f"{w['name'].replace(' ', '_')}.xlsx", btn_method
    if isinstance(act, dict) and act.get("report_name"):
        rpt_url = f"{clean_base}/report/xlsx/{act['report_name']}"
        with opener.open(rpt_url, timeout=timeout) as resp:
            return resp.read(), f"{w['name'].replace(' ', '_')}.xlsx", btn_method
    return None, "", btn_method

def extract_grir_transport_fast(url, db, uid, pw, acc_ids, d_from, d_to, out_xlsx_path, progress):
    d1_str, d2_str = to_odoo_date(d_from), to_odoo_date(d_to)
    dom = [["parent_state", "=", "posted"], ["date", ">=", d1_str], ["date", "<=", d2_str]]
    if acc_ids:
        dom.append(["account_id", "in", acc_ids])
    else:
        dom.extend([["account_id.name", "ilike", "GRIR"], ["account_id.name", "ilike", "TRANSPORT"]])

    if progress:
        progress(0.08, desc="Fetching posted GRIR Transport ledger items...")
    aml_fields = ["id", "date", "move_id", "move_name", "ref", "name", "po_ref", "partner_id", "account_id", "debit", "credit", "balance"]
    aml_rows = rpc_fetch_all(url, db, uid, pw, "account.move.line", dom, aml_fields, 2500, progress, "GRIR Ledger Lines", 0.10, 0.45)
    if not aml_rows:
        return 0, 0.0, 0.0, 0.0

    first_id = lambda val: next(iter(val), 0) if isinstance(val, list) and val else 0
    lc_by_grn, all_lc_rows, bill_move_ids, bill_lines = {}, list(), set(), list()
    for r in aml_rows:
        ref_txt, name_txt = str(r.get("ref") or ""), str(r.get("name") or "")
        combo = f"{ref_txt} | {name_txt}"
        m_lc = re.search(r"(LC/\d{4}/\d+)", combo)
        m_grn = re.search(r"(CM/GRN/\d{4}/\d+)", combo)
        m_po = re.search(r"([A-Z]+ORD/\d{4}/\d+)", combo)
        p_name = r["partner_id"][-1] if isinstance(r.get("partner_id"), list) and r["partner_id"] else ""
        m_id = first_id(r.get("move_id"))

        entry = {
            "id": r["id"], "date": str(r.get("date") or ""), "move_id": m_id,
            "move_name": str(r.get("move_name") or ""), "ref": ref_txt, "name": name_txt,
            "lc": m_lc.group(1) if m_lc else "", "grn": m_grn.group(1) if m_grn else "",
            "po": m_po.group(1) if m_po else str(r.get("po_ref") or ""),
            "partner": re.sub(r"^\[.*?\]\s*", "", str(p_name)),
            "debit": float(r.get("debit") or 0.0), "credit": float(r.get("credit") or 0.0), "balance": float(r.get("balance") or 0.0),
