import gradio as gr
import urllib.request, urllib.error, http.cookiejar, json, base64, tempfile, os
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urlparse

def odoo_jsonrpc(url, service, method, args, timeout=300):
    clean_url = url.strip().rstrip("/")
    target_url = f"{clean_url}/jsonrpc"
    payload = {"jsonrpc": "2.0", "method": "call", "params": {"service": service, "method": method, "args": args}, "id": 1}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    for _ in range(5):
        req = urllib.request.Request(target_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                if "error" in result:
                    err_msg = result["error"].get("data", {}).get("message") or result["error"].get("message")
                    raise Exception(f"Odoo Error: {err_msg}")
                return result.get("result")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                new_url = e.headers.get("Location")
                target_url = f"{urlparse(target_url).scheme}://{urlparse(target_url).netloc}{new_url}" if new_url.startswith("/") else new_url
            else:
                raise Exception(f"HTTP {e.code}: {e.reason}")
    raise Exception("Too many redirects.")

def authenticate_and_execute(url, db, username, password, model, method, args, kwargs=None):
    uid = odoo_jsonrpc(url, "common", "authenticate", [db, username, password, {}], timeout=30)
    if not uid:
        raise Exception("Authentication failed.")
    call_args = [db, uid, password, model, method, args]
    if kwargs is not None:
        call_args.append(kwargs)
    return odoo_jsonrpc(url, "object", "execute_kw", call_args, timeout=300)

def get_auth_opener(url, db, username, password):
    clean_url = url.strip().rstrip("/")
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    auth_payload = json.dumps({"jsonrpc": "2.0", "params": {"db": db, "login": username, "password": password}}).encode("utf-8")
    auth_req = urllib.request.Request(f"{clean_url}/web/session/authenticate", data=auth_payload, headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(auth_req, timeout=30) as resp:
        auth_res = json.loads(resp.read().decode("utf-8"))
        if "error" in auth_res or not auth_res.get("result", {}).get("uid"):
            raise Exception("Web session authentication failed.")
    return opener, clean_url

def download_odoo_report_web(url, db, username, password, report_name, record_id, report_type="qweb-pdf"):
    opener, clean_url = get_auth_opener(url, db, username, password)
    route_prefix = "xlsx" if "xlsx" in report_type else "pdf"
    ids_str = ",".join(str(x) for x in record_id) if isinstance(record_id, list) else str(record_id)
    report_url = f"{clean_url}/report/{route_prefix}/{report_name}/{ids_str}"
    with opener.open(report_url, timeout=300) as report_resp:
        return report_resp.read()

print("✅ Part 1 loaded!")

from datetime import datetime

def to_odoo_date(date_str):
    s = str(date_str or "").strip()
    if not s:
        return ""
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s

def build_wizard_profile(url, db, uid, pw, w, cur_from="", cur_to=""):
    """Inspects only the VISIBLE form fields on Odoo's wizard popup, plus defaults and buttons."""
    model = w["res_model"]
    f_info = execute_with_uid(url, db, uid, pw, model, "fields_get", [], {"attributes": ["string", "type", "relation", "selection", "required", "domain"]}, timeout=60)
    ignore = {"id", "create_uid", "create_date", "write_uid", "write_date", "__last_update", "display_name"}

    v_raw = w.get("view_id")
    v_id = next(iter(v_raw), False) if isinstance(v_raw, list) else False
    arch = ""
    try:
        arch = execute_with_uid(url, db, uid, pw, model, "get_views", [], {"views": [[v_id, "form"]]}, timeout=60).get("views", {}).get("form", {}).get("arch", "")
    except Exception:
        try:
            arch = execute_with_uid(url, db, uid, pw, model, "fields_view_get", [], {"view_id": v_id, "view_type": "form"}, timeout=60).get("arch", "")
        except Exception:
            pass

    btns, visible_names = [], []
    if arch:
        root = ET.fromstring(arch)
        btns = [b.attrib["name"] for b in root.iter("button") if b.attrib.get("name") and b.attrib.get("invisible") != "1"]
        for fld in root.iter("field"):
            fn = fld.attrib.get("name")
            if fn and fn in f_info and fn not in ignore and fld.attrib.get("invisible") != "1":
                if fn not in visible_names:
                    visible_names.append(fn)

    # Fallback to all non-binary fields only if no form XML was returned
    valid_fields = visible_names if visible_names else [k for k in f_info if k not in ignore and not k.startswith("message_") and f_info[k].get("type") != "binary"]

    try:
        default_vals = execute_with_uid(url, db, uid, pw, model, "default_get", [valid_fields], timeout=60) or {}
    except Exception:
        default_vals = {}

    field_summary, start_fields, end_fields, acc_map = [], [], [], {}
    for fname in valid_fields:
        meta = f_info[fname]
        ftype, label = meta.get("type"), meta.get("string") or fname
        req = bool(meta.get("required"))
        req_tag = " [REQUIRED]" if req else ""
        field_summary.append(f"- `{fname}` ({ftype}{req_tag}): {label}")

        if ftype in ("date", "datetime"):
            low_all = f"{fname.lower()} {label.lower()}"
            is_start = any(x in low_all for x in ("from", "start", "begin"))
            (start_fields if is_start else end_fields).append((fname, ftype))
            raw_d = to_odoo_date(cur_from) if is_start else to_odoo_date(cur_to)
            if raw_d:
                default_vals[fname] = f"{raw_d} 00:00:00" if ftype == "datetime" else raw_d
        elif ftype == "selection" and meta.get("selection") and fname not in default_vals:
            valid = [item for item in meta["selection"] if isinstance(item, (list, tuple)) and len(item) >= 2]
            if valid:
                default_vals[fname] = valid
        elif ftype == "many2one" and meta.get("relation") and req and not default_vals.get(fname):
            try:
                recs = execute_with_uid(url, db, uid, pw, meta["relation"], "search_read", [[]], {"fields": ["id"], "limit": 1}, timeout=30)
                if recs:
                    default_vals[fname] = recs["id"]
            except Exception:
                pass
        elif ftype in ("many2one", "many2many") and meta.get("relation") == "account.account":
            try:
                dom = [["name", "ilike", "GRIR"]] if "grir" in model.lower() or "grir" in w["name"].lower() else []
                accs = execute_with_uid(url, db, uid, pw, "account.account", "search_read", [dom], {"fields": ["id", "display_name"], "limit": 200}, timeout=30)
                if not accs:
                    accs = execute_with_uid(url, db, uid, pw, "account.account", "search_read", [[]], {"fields": ["id", "display_name"], "limit": 200}, timeout=30)
                acc_map = {f"{a['display_name']} (ID:{a['id']})": a["id"] for a in accs}
            except Exception:
                pass

    return {
        "valid_fields": valid_fields,
        "start_fields": start_fields,
        "end_fields": end_fields,
        "defaults": default_vals,
        "buttons": btns,
        "summary": field_summary,
        "acc_map": acc_map,
    }

def connect_and_discover(url, db, user, pw, progress=gr.Progress()):
    try:
        progress(0.05, desc="Authenticating & discovering wizards...")
        uid = odoo_jsonrpc(url, "common", "authenticate", [db, user, pw, {}], timeout=60)
        if not uid:
            raise Exception("Authentication failed.")
        wizards = execute_with_uid(url, db, uid, pw, "ir.actions.act_window", "search_read", [[["target", "=", "new"], ["res_model", "!=", False]]], {"fields": ["id", "name", "res_model", "view_id"], "limit": 500}, timeout=120)
        keywords = ["report", "ledger", "register", "grir", "aged", "tax", "statement", "summary", "breakup", "mis", "provision", "reconcile", "efd", "tansad", "kardex", "payroll", "employee", "stock"]
        filtered = [w for w in wizards if any(k in w["name"].lower() for k in keywords)]
        final_list = filtered if filtered else wizards

        wiz_map = {}
        total = len(final_list)
        for idx, w in enumerate(final_list):
            if idx % 5 == 0:
                progress(0.1 + 0.85 * (idx / max(total, 1)), desc=f"Testing wizard fields ({idx}/{total}): {w['name']}...")
            label = f"{w['name']} (Action ID: {w['id']} | {w['res_model']})"
            try:
                w["profile"] = build_wizard_profile(url, db, uid, pw, w)
            except Exception as prof_err:
                w["profile"] = {"error": str(prof_err), "valid_fields": [], "start_fields": [], "end_fields": [], "defaults": {}, "buttons": [], "summary": [], "acc_map": {}}
            wiz_map[label] = w

        choices = sorted(list(wiz_map.keys()))
        first_choice = next(iter(choices), None)
        progress(1.0, desc="All wizards tested and mapped!")
        return f"✅ Connected & tested **{len(choices)} wizards**! Every wizard's exact fields & defaults are now cached.", gr.update(choices=choices, value=first_choice), wiz_map
    except Exception as e:
        return f"❌ Error: {str(e)}", gr.update(choices=[]), {}

def inspect_wizard(url, db, user, pw, selected_label, wiz_map, cur_from, cur_to):
    if not selected_label or selected_label not in wiz_map:
        return "⚠️ Please select a wizard first.", "{}", "", False, gr.update(choices=[], value=[]), {}
    w = wiz_map[selected_label]
    prof = w.get("profile")
    if not prof or prof.get("error"):
        try:
            uid = odoo_jsonrpc(url, "common", "authenticate", [db, user, pw, {}], timeout=60)
            prof = build_wizard_profile(url, db, uid, pw, w, cur_from, cur_to)
            w["profile"] = prof
        except Exception as e:
            return f"❌ Error: {str(e)}", "{}", "", False, gr.update(choices=[], value=[]), {}

    vals = dict(prof.get("defaults", {}))
    clean_from, clean_to = to_odoo_date(cur_from), to_odoo_date(cur_to)
    if clean_from:
        for fname, ftype in prof.get("start_fields", []):
            vals[fname] = f"{clean_from} 00:00:00" if ftype == "datetime" else clean_from
    if clean_to:
        for fname, ftype in prof.get("end_fields", []):
            vals[fname] = f"{clean_to} 23:59:59" if ftype == "datetime" else clean_to

    btns = prof.get("buttons", [])
    first_btn = next(iter(btns), "action_print_report")
    acc_map = prof.get("acc_map", {})
    info_md = (
        f"### Wizard: **{w['name']}** (`{w['res_model']}`)\n"
        f"**Detected Buttons:** `{', '.join(btns) if btns else 'None (auto-trying standard methods)'}`\n\n"
        f"**Exact Model Fields:**\n" + "\n".join(prof.get("summary", []))
    )
    return info_md, json.dumps(vals, indent=2), first_btn, bool(vals.get("is_as_on", False)), gr.update(choices=sorted(list(acc_map.keys())), value=[]), acc_map

def sync_ui_to_json(params_json, d_from, d_to, is_as_on, selected_accs, acc_map, selected_label=None, wiz_map=None):
    try:
        data = json.loads(params_json) if params_json.strip() else {}
    except Exception:
        data = {}

    prof = (wiz_map or {}).get(selected_label, {}).get("profile", {})
    valid_set = set(prof.get("valid_fields", []))

    # Strip any invalid fields that do not exist on this wizard model
    if valid_set:
        data = {k: v for k, v in data.items() if k in valid_set}

    clean_from, clean_to = to_odoo_date(d_from), to_odoo_date(d_to)
    if clean_from:
        for fname, ftype in prof.get("start_fields", []):
            data[fname] = f"{clean_from} 00:00:00" if ftype == "datetime" else clean_from
    if clean_to:
        for fname, ftype in prof.get("end_fields", []):
            data[fname] = f"{clean_to} 23:59:59" if ftype == "datetime" else clean_to

    if "is_as_on" in data or "is_as_on" in valid_set:
        data["is_as_on"] = bool(is_as_on)
    if selected_accs and acc_map and (not valid_set or "account_ids" in valid_set):
        ids = [acc_map[a] for a in selected_accs if a in acc_map]
        data["account_ids"] = [[6, 0, ids]]
    return json.dumps(data, indent=2)

print("✅ Cell 2A loaded!")


import pandas as pd
import io, time, gc, xlsxwriter
from datetime import datetime, timedelta

def execute_with_uid(url, db, uid, pw, model, method, args, kwargs=None, timeout=900):
    call_args = [db, uid, pw, model, method, args]
    if kwargs is not None:
        call_args.append(kwargs)
    return odoo_jsonrpc(url, "object", "execute_kw", call_args, timeout=timeout)

def fetch_single_wizard_fast(url, db, uid, pw, opener, clean_base, w, wiz_vals, btn_method, user_ctx, timeout=900):
    # Strictly filter wiz_vals to fields that exist on this exact Odoo model
    f_info = execute_with_uid(url, db, uid, pw, w["res_model"], "fields_get", [], {"attributes": ["type"]}, timeout=timeout)
    safe_vals = {k: v for k, v in wiz_vals.items() if k in f_info}

    wiz_id = execute_with_uid(url, db, uid, pw, w["res_model"], "create", [safe_vals], {"context": user_ctx}, timeout=timeout)

    prof_btns = w.get("profile", {}).get("buttons", [])
    candidates = list(dict.fromkeys(
        ([btn_method.strip()] if btn_method and btn_method.strip() else [])
        + prof_btns
        + ["action_print_report", "print_report", "generate_report", "export_xls", "action_print_xlsx", "check_report"]
    ))

    res_action, used_m = None, None
    last_err = None
    for m in candidates:
        try:
            res_action = execute_with_uid(url, db, uid, pw, w["res_model"], m, [[wiz_id]], {"context": user_ctx}, timeout=timeout)
            used_m = m
            break
        except Exception as e:
            err_s = str(e)
            if "cannot marshal None" in err_s:
                used_m = m
                break
            if "has no attribute" in err_s or "does not exist" in err_s:
                last_err = e
                continue
            raise

    if used_m is None and last_err:
        raise last_err

    file_bytes, fname_out = None, f"{w['name'].replace(' ', '_')}.xlsx"
    if isinstance(res_action, dict) and res_action.get("report_name"):
        rep_type = res_action.get("report_type", "qweb-pdf")
        doc_ids = res_action.get("context", {}).get("active_ids") or [wiz_id]
        route_prefix = "xlsx" if "xlsx" in rep_type else ("html" if "html" in rep_type else "pdf")
        ids_str = ",".join(str(x) for x in doc_ids) if isinstance(doc_ids, list) else str(doc_ids)
        report_url = f"{clean_base}/report/{route_prefix}/{res_action['report_name']}/{ids_str}"
        with opener.open(report_url, timeout=timeout) as report_resp:
            file_bytes = report_resp.read()
        ext = "xlsx" if "xlsx" in rep_type else ("html" if "html" in rep_type else "pdf")
        fname_out = f"{w['name'].replace(' ', '_')}.{ext}"
    elif isinstance(res_action, dict) and res_action.get("url"):
        dl_url = res_action["url"]
        full_url = dl_url if dl_url.startswith("http") else f"{clean_base}{dl_url}"
        with opener.open(full_url, timeout=timeout) as report_resp:
            file_bytes = report_resp.read()
        url_tail = dl_url.split("/")[-1]
        fname_out = next(iter(url_tail.split("?")), fname_out) or fname_out
    else:
        bin_fields = [k for k, v in f_info.items() if v.get("type") == "binary"]
        char_fields = [k for k, v in f_info.items() if v.get("type") == "char"]
        recs = execute_with_uid(url, db, uid, pw, w["res_model"], "read", [[wiz_id]], {"fields": bin_fields + char_fields, "context": user_ctx}, timeout=timeout)
        rec = next(iter(recs), None) if isinstance(recs, list) else (recs if isinstance(recs, dict) else None)
        if isinstance(rec, dict):
            fname_out = next((rec[cf] for cf in char_fields if rec.get(cf) and "." in str(rec[cf])), fname_out)
            for bf in bin_fields:
                if rec.get(bf):
                    file_bytes = base64.b64decode(rec[bf])
                    break
        if not file_bytes:
            atts = execute_with_uid(url, db, uid, pw, "ir.attachment", "search_read", [[["res_model", "=", w["res_model"]], ["res_id", "=", wiz_id]]], {"fields": ["name", "datas"], "limit": 1, "context": user_ctx}, timeout=timeout)
            att = next(iter(atts), None) if isinstance(atts, list) else (atts if isinstance(atts, dict) else None)
            if isinstance(att, dict) and att.get("datas"):
                file_bytes = base64.b64decode(att["datas"])
                fname_out = att["name"]

    return file_bytes, fname_out, used_m

print("✅ Cell 3 loaded!")

import threading, re

STOP_EVENT = threading.Event()

def stop_generation():
    STOP_EVENT.set()
    return "🛑 Stop signal sent!"

def rpc_fetch_all(url, db, uid, pw, model, domain, fields, batch_size=2000, progress=None, label="Fetching", p_start=0.1, p_end=0.5):
    total = execute_with_uid(url, db, uid, pw, model, "search_count", [domain], timeout=120)
    if not total:
        return list()
    out = list()
    offset = 0
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

def extract_grir_transport_fast(url, db, uid, pw, acc_ids, d_from, d_to, out_xlsx_path, progress):
    d1_str = to_odoo_date(d_from)
    d2_str = to_odoo_date(d_to)
    dom = [["parent_state", "=", "posted"], ["date", ">=", d1_str], ["date", "<=", d2_str]]
    if acc_ids:
        dom.append(["account_id", "in", acc_ids])
    else:
        dom.extend([["account_id.name", "ilike", "GRIR"], ["account_id.name", "ilike", "TRANSPORT"]])

    progress(0.08, desc="Fetching posted GRIR Transport ledger items...")
    aml_fields = ["id", "date", "move_id", "move_name", "ref", "name", "po_ref", "partner_id", "account_id", "debit", "credit", "balance"]
    aml_rows = rpc_fetch_all(url, db, uid, pw, "account.move.line", dom, aml_fields, 2500, progress, "GRIR Ledger Lines", 0.10, 0.45)

    if not aml_rows:
        return 0, 0.0, 0.0, 0.0

    # Parse LC, PO, GRN from ref/name and separate LC accruals vs Bill debits
    lc_by_grn = {}
    all_lc_rows = list()
    bill_move_ids = set()
    bill_lines = list()

    for r in aml_rows:
        ref_txt = str(r.get("ref") or "")
        name_txt = str(r.get("name") or "")
        combo = f"{ref_txt} | {name_txt}"
        m_lc = re.search(r"(LC/\d{4}/\d+)", combo)
        m_grn = re.search(r"(CM/GRN/\d{4}/\d+)", combo)
        m_po = re.search(r"([A-Z]+ORD/\d{4}/\d+)", combo)
        lc_no = m_lc.group(1) if m_lc else ""
        grn_no = m_grn.group(1) if m_grn else ""
        po_no = m_po.group(1) if m_po else str(r.get("po_ref") or "")
        p_name = r["partner_id"][1] if isinstance(r.get("partner_id"), list) else ""
        p_clean = re.sub(r"^\[.*?\]\s*", "", p_name)

        entry = {
            "id": r["id"],
            "date": str(r.get("date") or ""),
            "move_id": r["move_id"][0] if isinstance(r.get("move_id"), list) else False,
            "move_name": str(r.get("move_name") or ""),
            "ref": ref_txt,
            "name": name_txt,
            "lc": lc_no,
            "grn": grn_no,
            "po": po_no,
            "partner": p_clean,
            "debit": float(r.get("debit") or 0.0),
            "credit": float(r.get("credit") or 0.0),
            "balance": float(r.get("balance") or 0.0),
        }
        if entry["debit"] > 0 and entry["move_id"]:
            bill_move_ids.add(entry["move_id"])
            bill_lines.append(entry)
        else:
            all_lc_rows.append(entry)
            if grn_no:
                lc_by_grn.setdefault(grn_no, list()).append(entry)

    # Fetch account.move for all bills to get tbill_purchase_id (TBS/PUR/...)
    progress(0.50, desc=f"Linking {len(bill_move_ids):,} Vendor Bills to TBS records...")
    move_map = {}
    tbs_ids = set()
    m_id_list = list(bill_move_ids)
    for i in range(0, len(m_id_list), 2000):
        batch_ids = m_id_list[i:i+2000]
        m_recs = execute_with_uid(url, db, uid, pw, "account.move", "read", [batch_ids], {"fields": ["id", "name", "ref", "invoice_date", "invoice_origin", "tbill_purchase_id", "partner_id"]}, timeout=180)
        for m in m_recs:
            move_map[m["id"]] = m
            if isinstance(m.get("tbill_purchase_id"), list):
                tbs_ids.add(m["tbill_purchase_id"][0])

    # Fetch transporter.bill.purchase.line for those TBS records to get exact GRN -> Bill links
    progress(0.65, desc=f"Fetching GRN links for {len(tbs_ids):,} TBS settlements...")
    tbs_lines_by_settle = {}
    t_id_list = list(tbs_ids)
    for i in range(0, len(t_id_list), 500):
        batch_tids = t_id_list[i:i+500]
        t_lines = execute_with_uid(
            url, db, uid, pw, "transporter.bill.purchase.line", "search_read",
            [[["settlement_id", "in", batch_tids]]],
            {"fields": ["settlement_id", "grn_nos", "grn_date", "vendor", "partner_id", "purchase_orders", "product", "qty_ton", "freight_amount", "vendor_bill_no", "vendor_bill_date"], "limit": 50000},
            timeout=180
        )
        for tl in t_lines:
            sid = tl["settlement_id"][0] if isinstance(tl.get("settlement_id"), list) else False
            if sid:
                tbs_lines_by_settle.setdefault(sid, list()).append(tl)

    progress(0.82, desc="Building Trial-Balance-exact GRIR Transport workbook...")
    workbook = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})
    ws_main = workbook.add_worksheet("GRIR_Transport_Breakup")
    ws_tb = workbook.add_worksheet("Trial_Balance_Check")
    ws_raw = workbook.add_worksheet("Raw_GRIR_Ledger_Lines")

    headers_main = [
        "TBS Bill No", "TBS Number", "TBS Vendor", "TBS Bill Date", "Posting Date",
        "LC", "LC Date", "Year (LC Date)", "Month (LC Date)",
        "Partner Name (GRN Vendor)", "Transport Name", "GRN", "PO Number", "Product",
        "Sum of TBS Amount", "Sum of LC Amount", "Sum of Bill Amount", "Net Amount", "Status"
    ]
    ws_main.write_row(0, 0, headers_main)

    curr_row = 1
    matched_lc_ids = set()

    for bl in bill_lines:
        m_info = move_map.get(bl["move_id"], {})
        bill_no = m_info.get("name") or bl["move_name"]
        tbs_pair = m_info.get("tbill_purchase_id")
        tbs_id = tbs_pair[0] if isinstance(tbs_pair, list) else False
        tbs_num = tbs_pair[1] if isinstance(tbs_pair, list) else str(m_info.get("invoice_origin") or "")
        tbs_vnd_inv = str(m_info.get("ref") or bl["ref"] or "")
        tbs_bill_dt = str(m_info.get("invoice_date") or bl["date"] or "")
        trans_name = bl["partner"]

        linked_grn_lines = tbs_lines_by_settle.get(tbs_id, list()) if tbs_id else list()
        if linked_grn_lines:
            for idx_g, gl in enumerate(linked_grn_lines):
                grn_code = str(gl.get("grn_nos") or "").strip()
                grn_vendor = str(gl.get("vendor") or "").strip()
                po_code = str(gl.get("purchase_orders") or bl["po"] or "").strip()
                prod_name = str(gl.get("product") or "").strip()
                t_name = re.sub(r"^\[.*?\]\s*", "", gl["partner_id"][1]) if isinstance(gl.get("partner_id"), list) else trans_name

                # Match LC credit line(s) for this GRN
                lc_matches = lc_by_grn.get(grn_code, list())
                lc_code = ", ".join(dict.fromkeys(x["lc"] for x in lc_matches if x["lc"]))
                lc_dt = next((x["date"] for x in lc_matches if x["date"]), "")
                lc_yr, lc_mo = "", ""
                if len(lc_dt) >= 7:
                    try:
                        dt_obj = datetime.strptime(lc_dt[:10], "%Y-%m-%d")
                        lc_yr, lc_mo = dt_obj.year, dt_obj.strftime("%b")
                    except Exception:
                        lc_yr = lc_dt[:4]

                lc_amt = sum(x["balance"] for x in lc_matches if x["id"] not in matched_lc_ids)
                for x in lc_matches:
                    matched_lc_ids.add(x["id"])

                # Put total TBS Bill debit on first GRN line of the bill so column sum equals exact GL debit!
                tbs_amt = bl["debit"] if idx_g == 0 else 0.0
                net_amt = tbs_amt + lc_amt
                status = "Matched (Bill + LC)" if lc_matches else "Bill Posted (No LC in Range)"

                ws_main.write_row(curr_row, 0, [
                    bill_no, tbs_num, tbs_vnd_inv, tbs_bill_dt, bl["date"],
                    lc_code, lc_dt, lc_yr, lc_mo,
                    grn_vendor, t_name, grn_code, po_code, prod_name,
                    tbs_amt, lc_amt, 0.0, net_amt, status
                ])
                curr_row += 1
        else:
            # Bill debit without TBS settlement lines
            ws_main.write_row(curr_row, 0, [
                bill_no, tbs_num, tbs_vnd_inv, tbs_bill_dt, bl["date"],
                bl["lc"], "", "", "",
                "", trans_name, bl["grn"], bl["po"], bl["name"],
                bl["debit"], 0.0, 0.0, bl["debit"], "Bill Posted (Direct)"
            ])
            curr_row += 1

    # Write all remaining/unbilled LC accrual lines so 100% of GL credits are included
    for lc_r in all_lc_rows:
        if lc_r["id"] in matched_lc_ids:
            continue
        lc_dt = lc_r["date"]
        lc_yr, lc_mo = "", ""
        if len(lc_dt) >= 7:
            try:
                dt_obj = datetime.strptime(lc_dt[:10], "%Y-%m-%d")
                lc_yr, lc_mo = dt_obj.year, dt_obj.strftime("%b")
            except Exception:
                lc_yr = lc_dt[:4]
        ws_main.write_row(curr_row, 0, [
            "", "", "", "", lc_dt,
            lc_r["lc"], lc_dt, lc_yr, lc_mo,
            lc_r["partner"], "", lc_r["grn"], lc_r["po"], lc_r["name"],
            0.0, lc_r["balance"], 0.0, lc_r["balance"], "Pending GRN / Unbilled LC"
        ])
        curr_row += 1

    tot_debit = sum(x["debit"] for x in aml_rows)
    tot_credit = sum(x["credit"] for x in aml_rows)
    net_gl = sum(x["balance"] for x in aml_rows)

    ws_tb.write_row(0, 0, ["Metric", "Amount (TZS)"])
    ws_tb.write_row(1, 0, ["Period", f"{d_from} to {d_to}"])
    ws_tb.write_row(2, 0, ["Total GL Debits (TBS Bills)", tot_debit])
    ws_tb.write_row(3, 0, ["Total GL Credits (LC Accruals)", tot_credit])
    ws_tb.write_row(4, 0, ["Net GL Movement (Debit - Credit)", net_gl])
    ws_tb.write_row(5, 0, ["Total Exported Rows", curr_row - 1])

    grn_meta = {str(tl.get("grn_nos") or "").strip(): tl for s in tbs_lines_by_settle.values() for tl in s if tl.get("grn_nos")}
    ws_raw.write_row(0, 0, ["AML_ID", "Date", "Journal_Entry", "Account", "Partner", "Reference", "Label", "GRN", "GRN Date", "Partner Name", "Transporter Name", "Vendor DC No", "DC Date", "Debit", "Credit", "Balance"])
    for idx_r, r in enumerate(aml_rows, start=1):
        acc_lbl = r["account_id"][-1] if isinstance(r.get("account_id"), list) and r["account_id"] else ""
        prt_lbl = r["partner_id"][-1] if isinstance(r.get("partner_id"), list) and r["partner_id"] else ""
        m_g = re.search(r"(CM/GRN/\d{4}/\d+)", f"{r.get('ref') or ''} | {r.get('name') or ''}")
        g_code = m_g.group(1) if m_g else ""
        tl = grn_meta.get(g_code, {})
        t_raw = tl.get("partner_id")
        t_name = re.sub(r"^\[.*?\]\s*", "", str(t_raw[-1])) if isinstance(t_raw, list) and t_raw else (re.sub(r"^\[.*?\]\s*", "", prt_lbl) if r.get("debit", 0) > 0 else "")
        ws_raw.write_row(idx_r, 0, [r["id"], r["date"], r["move_name"], acc_lbl, prt_lbl, r["ref"], r["name"], g_code, str(tl.get("grn_date") or ""), str(tl.get("vendor") or ""), t_name, str(tl.get("reference_no") or ""), str(tl.get("token_date") or ""), r["debit"], r["credit"], r["balance"]])

    workbook.close()
    progress(1.0, desc=f"Done! {curr_row - 1:,} rows exported (100% Trial Balance exact).")
    return curr_row - 1, tot_debit, tot_credit, net_gl
def extract_sale_register_fast(url, db, uid, pw, d_from, d_to, out_xlsx_path, progress, acc_ids=None):
    """Fetches all posted Income/Revenue GL lines (Invoices, Credit Notes & Journals) — 100% TB exact."""
    d1_str, d2_str = to_odoo_date(d_from), to_odoo_date(d_to)
    dom = [
        ["parent_state", "=", "posted"],
        ["date", ">=", d1_str],
        ["date", "<=", d2_str],
    ]
    if acc_ids:
        dom.append(["account_id", "in", acc_ids])
    else:
        dom.append(["account_id.code", "=like", "701%"])

    progress(0.08, desc="Fetching posted Revenue GL lines (100% TB exact)...")
    fields = [
        "id", "date", "move_id", "move_name", "ref", "journal_id", "partner_id",
        "customer_code", "customer_name", "tin_no", "vat", "product_id", "product_code",
        "name", "quantity", "product_uom_id", "price_unit", "discount",
        "price_subtotal", "taxed_amt", "price_total",
        "debit", "credit", "balance", "account_id", "analytic_account_id", "team_id", "currency_id", "amount_currency"
    ]
    rows = rpc_fetch_all(url, db, uid, pw, "account.move.line", dom, fields, 2500, progress, "Revenue GL Lines", 0.10, 0.75)
    if not rows:
        return 0, 0.0, 0.0, 0.0

    move_ids = list({r["move_id"][0] for r in rows if isinstance(r.get("move_id"), list) and r["move_id"]})
    progress(0.78, desc=f"Linking {len(move_ids):,} Invoices, Credit Notes & Journals...")
    move_map = {}
    for i in range(0, len(move_ids), 2000):
        for m in execute_with_uid(url, db, uid, pw, "account.move", "read", [move_ids[i:i+2000]], {"fields": ["id", "name", "move_type", "invoice_date", "invoice_origin", "ref", "payment_state", "invoice_user_id"]}, timeout=180):
            move_map[m["id"]] = m

    progress(0.88, desc="Writing Trial-Balance-exact Sale Register workbook...")
    workbook = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})
    ws_main = workbook.add_worksheet("Sale_Register_Data")
    ws_tb = workbook.add_worksheet("Trial_Balance_Check")

    headers = [
        "AML_ID", "Posting Date", "Invoice Date", "Journal", "Document Type", "Document No",
        "Source Order", "Reference", "Customer Code", "Customer Name", "TIN No", "VRN / VAT",
        "Product Code", "Product / Description", "Quantity", "UOM", "Unit Price", "Discount (%)",
        "GL Debit", "GL Credit", "Net Revenue (Credit - Debit)", "VAT / Tax Info", "Gross Invoice Line",
        "Revenue Account", "Analytic Account", "Sales Team", "Currency", "Amount Currency", "Salesperson", "Payment Status"
    ]
    ws_main.write_row(0, 0, headers)

    tot_dr = tot_cr = tot_vat = tot_gross = 0.0
    type_labels = {"out_invoice": "Customer Invoice", "out_refund": "Credit Note", "entry": "Journal Entry"}

    for idx_r, r in enumerate(rows, start=1):
        m_pair = r.get("move_id")
        m_id = m_pair[0] if isinstance(m_pair, list) and m_pair else False
        m_info = move_map.get(m_id, {})
        m_type = m_info.get("move_type", "entry")
        sign = -1.0 if m_type == "out_refund" else 1.0

        dr = float(r.get("debit") or 0.0)
        cr = float(r.get("credit") or 0.0)
        net_rev = cr - dr
        tax_signed = float(r.get("taxed_amt") or 0.0) * sign
        tot_signed = float(r.get("price_total") or 0.0) * sign

        tot_dr += dr
        tot_cr += cr
        tot_vat += tax_signed
        tot_gross += tot_signed

        get_lbl = lambda k: str(r[k][-1]) if isinstance(r.get(k), list) and r[k] else ""
        prt_name = r.get("customer_name") or re.sub(r"^\[.*?\]\s*", "", get_lbl("partner_id"))
        prod_lbl = get_lbl("product_id") or str(r.get("name") or "")
        usr_raw = m_info.get("invoice_user_id")
        usr_lbl = str(usr_raw[-1]) if isinstance(usr_raw, list) and usr_raw else ""

        ws_main.write_row(idx_r, 0, [
            r["id"], str(r.get("date") or ""), str(m_info.get("invoice_date") or ""), get_lbl("journal_id"),
            type_labels.get(m_type, m_type), str(r.get("move_name") or ""),
            str(m_info.get("invoice_origin") or ""), str(r.get("ref") or ""),
            str(r.get("customer_code") or ""), str(prt_name or ""), str(r.get("tin_no") or ""), str(r.get("vat") or ""),
            str(r.get("product_code") or ""), prod_lbl, float(r.get("quantity") or 0.0) * sign, get_lbl("product_uom_id"),
            float(r.get("price_unit") or 0.0), float(r.get("discount") or 0.0),
            dr, cr, net_rev, tax_signed, tot_signed,
            get_lbl("account_id"), get_lbl("analytic_account_id"), get_lbl("team_id"),
            get_lbl("currency_id"), float(r.get("amount_currency") or 0.0), usr_lbl, str(m_info.get("payment_state") or "")
        ])

    net_tb_rev = tot_cr - tot_dr
    ws_tb.write_row(0, 0, ["Metric", "Amount (TZS)"])
    ws_tb.write_row(1, 0, ["Period", f"{d_from} to {d_to}"])
    ws_tb.write_row(2, 0, ["Total Revenue GL Debits (Matches TB Debit)", tot_dr])
    ws_tb.write_row(3, 0, ["Total Revenue GL Credits (Matches TB Credit)", tot_cr])
    ws_tb.write_row(4, 0, ["Net Revenue (Credit - Debit — Matches TB Net)", net_tb_rev])
    ws_tb.write_row(5, 0, ["Output VAT Info (Invoice Lines)", tot_vat])
    ws_tb.write_row(6, 0, ["Total Exported GL Lines", len(rows)])

    workbook.close()
    progress(1.0, desc=f"Done! {len(rows):,} Revenue GL lines exported (100% TB exact).")
    return len(rows), net_tb_rev, tot_dr, tot_cr

def extract_purchase_register_fast(url, db, uid, pw, d_from, d_to, out_xlsx_path, progress, acc_ids=None):
    """Fetches Inventory (551%, 731004, 751018) & GRIR (181%) into one single TB-exact sheet with PR, PO, GRN, Bill & TANSAD."""
    d1_str, d2_str = to_odoo_date(d_from), to_odoo_date(d_to)
    if acc_ids:
        dom = [["parent_state", "=", "posted"], ["date", ">=", d1_str], ["date", "<=", d2_str], ["account_id", "in", acc_ids]]
    else:
        dom = [
            ["parent_state", "=", "posted"], ["date", ">=", d1_str], ["date", "<=", d2_str],
            "|", "|",
            ["account_id.code", "=like", "551%"],
            ["account_id.code", "=like", "181%"],
            ["account_id.code", "in", ["731004", "751018"]],
        ]

    progress(0.08, desc="Fetching Inventory (551%, 731004, 751018) & GRIR (181%) GL lines...")
    fields = [
        "id", "date", "move_id", "move_name", "ref", "journal_id", "partner_id",
        "tin_no", "vat", "product_id", "product_code", "name", "quantity", "product_uom_id",
        "price_unit", "price_subtotal", "taxed_amt", "price_total",
        "debit", "credit", "balance", "account_id", "analytic_account_id",
        "po_ref", "purchase_order_id", "is_landed_costs_line"
    ]
    rows = rpc_fetch_all(url, db, uid, pw, "account.move.line", dom, fields, 2500, progress, "Inventory & GRIR GL", 0.10, 0.65)
    if not rows:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    first_id = lambda val: next(iter(val), 0) if isinstance(val, list) and val else 0

    acc_id_set = list({first_id(r.get("account_id")) for r in rows if first_id(r.get("account_id"))})
    acc_code_map = {}
    for i in range(0, len(acc_id_set), 1000):
        for a in execute_with_uid(url, db, uid, pw, "account.account", "read", [acc_id_set[i:i+1000]], {"fields": ["id", "code"]}, timeout=120):
            acc_code_map[a["id"]] = str(a.get("code") or "")

    move_ids = list({first_id(r.get("move_id")) for r in rows if first_id(r.get("move_id"))})
    progress(0.68, desc=f"Linking {len(move_ids):,} Bills, GRNs, POs, PRs & TANSAD records...")
    move_map, all_picking_ids = {}, set()
    for i in range(0, len(move_ids), 2000):
        for m in execute_with_uid(url, db, uid, pw, "account.move", "read", [move_ids[i:i+2000]], {"fields": ["id", "move_type", "invoice_date", "invoice_origin", "ref", "payment_state", "tbill_purchase_id", "custom_doc_ref_id", "grn_ids"]}, timeout=180):
            move_map[m["id"]] = m
            all_picking_ids.update(m.get("grn_ids") or list())

    po_ids = list({first_id(r.get("purchase_order_id")) for r in rows if first_id(r.get("purchase_order_id"))})
    po_names = list({str(r.get("po_ref") or "").strip() for r in rows if r.get("po_ref") and not r.get("purchase_order_id")})
    po_map, po_by_name = {}, {}
    for i in range(0, len(po_ids), 2000):
        for po in execute_with_uid(url, db, uid, pw, "purchase.order", "read", [po_ids[i:i+2000]], {"fields": ["id", "name", "pr_id", "requisition_id", "custom_doc_ref_id", "picking_ids"]}, timeout=180):
            po_map[po["id"]] = po_by_name[str(po.get("name") or "")] = po
            all_picking_ids.update(po.get("picking_ids") or list())
    for i in range(0, len(po_names), 1000):
        for po in execute_with_uid(url, db, uid, pw, "purchase.order", "search_read", [[["name", "in", po_names[i:i+1000]]]], {"fields": ["id", "name", "pr_id", "requisition_id", "custom_doc_ref_id", "picking_ids"]}, timeout=180):
            po_map[po["id"]] = po_by_name[str(po.get("name") or "")] = po

    pick_name_map = {}
    pick_list = list(all_picking_ids)
    for i in range(0, len(pick_list), 2000):
        for pk in execute_with_uid(url, db, uid, pw, "stock.picking", "read", [pick_list[i:i+2000]], {"fields": ["id", "name"]}, timeout=120):
            pick_name_map[pk["id"]] = str(pk.get("name") or "")

    progress(0.85, desc="Writing single TB-exact Purchase Register workbook...")
    workbook = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})
    ws_main = workbook.add_worksheet("Purchase_Register_Data")
    ws_tb = workbook.add_worksheet("Trial_Balance_Check")

    headers = [
        "AML_ID", "Ledger Group", "Account Code", "Account Name", "Entry Source",
        "Posting Date", "Bill / Doc Date", "Journal", "Bill / Document No", "Vendor Invoice Ref",
        "Vendor Name", "TIN No", "VRN / VAT", "PR Number", "PO Number", "GRN Number", "TANSAD No", "LC Number", "TBS / Origin",
        "Product Code", "Product / Description", "Quantity", "UOM", "Unit Price",
        "Taxable Amount (Bills)", "Input VAT (Bills)", "Gross Bill Amount",
        "GL Debit", "GL Credit", "Net GL Balance (Debit - Credit)", "Analytic Account", "Payment Status"
    ]
    ws_main.write_row(0, 0, headers)

    acc_summary = {}
    inv_dr = inv_cr = grir_dr = grir_cr = 0.0

    for idx_r, r in enumerate(rows, start=1):
        acc_pair = r.get("account_id")
        acc_id = first_id(acc_pair)
        acc_full = str(acc_pair[-1]) if isinstance(acc_pair, list) and acc_pair else ""
        acc_code = acc_code_map.get(acc_id, next(iter(acc_full.split(" ")), "") if acc_full else "")
        grp = "INVENTORY" if (acc_code.startswith("551") or acc_code in ("731004", "751018")) else "GRIR"

        m_id = first_id(r.get("move_id"))
        m_info = move_map.get(m_id, {})
        m_type = m_info.get("move_type", "entry")

        ref_txt, name_txt, orig_txt = str(r.get("ref") or ""), str(r.get("name") or ""), str(m_info.get("invoice_origin") or "")
        combo = f"{ref_txt} | {name_txt} | {orig_txt}"

        po_pair = r.get("purchase_order_id")
        po_id = first_id(po_pair)
        m_po = re.search(r"([A-Z]+ORD/\d{4}/\d+)", combo)
        po_no = (str(po_pair[-1]) if isinstance(po_pair, list) and po_pair else "") or str(r.get("po_ref") or "") or (m_po.group(1) if m_po else "")
        po_info = po_map.get(po_id) or po_by_name.get(po_no, {})

        pr_pair = po_info.get("pr_id") or po_info.get("requisition_id")
        pr_no = str(pr_pair[-1]) if isinstance(pr_pair, list) and pr_pair else ""
        tansad_pair = m_info.get("custom_doc_ref_id") or po_info.get("custom_doc_ref_id")
        tansad_no = str(tansad_pair[-1]) if isinstance(tansad_pair, list) and tansad_pair else ""

        m_grn = re.search(r"(CM/GRN/\d{4}/\d+|[A-Z]+/GRN/\d{4}/\d+)", combo)
        grn_from_move = ", ".join(dict.fromkeys(pick_name_map[pid] for pid in (m_info.get("grn_ids") or list()) if pick_name_map.get(pid)))
        grn_no = (m_grn.group(1) if m_grn else "") or grn_from_move

        m_lc = re.search(r"(LC/\d{4}/\d+)", combo)
        lc_no = m_lc.group(1) if m_lc else ""
        tbs_pair = m_info.get("tbill_purchase_id")
        tbs_origin = str(tbs_pair[-1]) if isinstance(tbs_pair, list) and tbs_pair else orig_txt

        src_label = "Vendor Bill" if m_type == "in_invoice" else ("Vendor Debit Note" if m_type == "in_refund" else ("Landed Cost / LC" if (r.get("is_landed_costs_line") or lc_no) else ("GRN / Stock Valuation" if grn_no else "Journal / Adjustment")))
        dr, cr, bal = float(r.get("debit") or 0.0), float(r.get("credit") or 0.0), float(r.get("balance") or 0.0)

        if grp == "INVENTORY":
            inv_dr += dr
            inv_cr += cr
        else:
            grir_dr += dr
            grir_cr += cr

        prev_d, prev_c = acc_summary.get((grp, acc_code, acc_full), (0.0, 0.0))
        acc_summary[(grp, acc_code, acc_full)] = (prev_d + dr, prev_c + cr)


        sign = -1.0 if m_type == "in_refund" else 1.0
        is_bill_line = m_type in ("in_invoice", "in_refund")
        sub_amt = float(r.get("price_subtotal") or 0.0) * sign if is_bill_line else 0.0
        vat_amt = float(r.get("taxed_amt") or 0.0) * sign if is_bill_line else 0.0
        tot_amt = float(r.get("price_total") or 0.0) * sign if is_bill_line else 0.0

        get_lbl = lambda k: str(r[k][-1]) if isinstance(r.get(k), list) and r[k] else ""
        prt_name = re.sub(r"^\[.*?\]\s*", "", get_lbl("partner_id"))
        prod_lbl = get_lbl("product_id") or name_txt

        ws_main.write_row(idx_r, 0, [
            r["id"], grp, acc_code, acc_full, src_label,
            str(r.get("date") or ""), str(m_info.get("invoice_date") or str(r.get("date") or "")),
            get_lbl("journal_id"), str(r.get("move_name") or ""), str(m_info.get("ref") or ref_txt),
            prt_name, str(r.get("tin_no") or ""), str(r.get("vat") or ""),
            pr_no, po_no, grn_no, tansad_no, lc_no, tbs_origin,
            str(r.get("product_code") or ""), prod_lbl, float(r.get("quantity") or 0.0) * sign, get_lbl("product_uom_id"),
            float(r.get("price_unit") or 0.0), sub_amt, vat_amt, tot_amt,
            dr, cr, bal, get_lbl("analytic_account_id"), str(m_info.get("payment_state") or "")
        ])

    ws_tb.write_row(0, 0, ["Ledger Group", "Account Code", "Account Name", "Period Debit (Matches TB)", "Period Credit (Matches TB)", "Net Movement (Debit - Credit)"])
    tb_r = 1
    for (grp, acode, aname), (d_sum, c_sum) in sorted(acc_summary.items()):
        ws_tb.write_row(tb_r, 0, [grp, acode, aname, d_sum, c_sum, d_sum - c_sum])
        tb_r += 1
    ws_tb.write_row(tb_r + 1, 0, ["SUMMARY", "INVENTORY TOTAL", "551xxx + 731004 + 751018", inv_dr, inv_cr, inv_dr - inv_cr])
    ws_tb.write_row(tb_r + 2, 0, ["SUMMARY", "GRIR TOTAL", "181001 to 181004", grir_dr, grir_cr, grir_dr - grir_cr])

    workbook.close()
    progress(1.0, desc=f"Done! {len(rows):,} Purchase Register lines exported (100% TB exact).")
    return len(rows), inv_dr, inv_cr, inv_dr - inv_cr, grir_dr, grir_cr, grir_dr - grir_cr
def _safe_fields(url, db, uid, pw, model, wanted):
    try:
        meta = execute_with_uid(url, db, uid, pw, model, "fields_get", [], {"attributes": ["type", "string"]}, timeout=60)
        return [f for f in wanted if f in meta], meta
    except Exception:
        return [], {}

def _lbl(v):
    return str(v[-1]) if isinstance(v, list) and v else ("" if v in (False, None) else str(v))

def _id(v):
    return v[0] if isinstance(v, list) and v else (v if isinstance(v, int) else 0)

def extract_universal_fast(url, db, uid, pw, w, d_from, d_to, out_xlsx_path, progress, acc_ids=None):
    d1, d2 = to_odoo_date(d_from), to_odoo_date(d_to)
    rep, mod = w.get("name", "Report"), w.get("res_model", "")
    key = f"{rep} {mod}".lower()
    wb = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})

    if any(k in key for k in ("kardex", "stock.move", "stock.statement", "stock.ledger", "bin.card")):
        flds, _ = _safe_fields(url, db, uid, pw, "stock.move.line", ["id", "date", "reference", "picking_id", "product_id", "lot_id", "location_id", "location_dest_id", "qty_done", "quantity", "product_uom_id"])
        qcol = "qty_done" if "qty_done" in flds else "quantity"
        rows = rpc_fetch_all(url, db, uid, pw, "stock.move.line", [["state", "=", "done"], ["date", ">=", f"{d1} 00:00:00"], ["date", "<=", f"{d2} 23:59:59"]], flds, 2500, progress, f"{rep} (Moves)", 0.1, 0.8)
        if not rows:
            wb.close(); return 0, "No stock movements found."
        int_ids = {l["id"] for l in execute_with_uid(url, db, uid, pw, "stock.location", "search_read", [[["usage", "=", "internal"]]], {"fields": ["id"]}, timeout=60)}
        ws = wb.add_worksheet("Kardex_Data")
        ws.write_row(0, 0, ["ID", "Date", "Reference", "Product", "Lot", "From", "To", "Type", "Qty In", "Qty Out", "Net Qty", "UOM"])
        tin = tout = tnet = 0.0
        for i, r in enumerate(rows, 1):
            si, di, q = _id(r.get("location_id")) in int_ids, _id(r.get("location_dest_id")) in int_ids, float(r.get(qcol) or 0.0)
            mt, qi, qo, nq = ("INWARD", q, 0.0, q) if (not si and di) else (("OUTWARD", 0.0, q, -q) if (si and not di) else ("INTERNAL", q, q, 0.0))
            tin += qi; tout += qo; tnet += nq
            ws.write_row(i, 0, [r["id"], _lbl(r.get("date")), _lbl(r.get("picking_id")) or _lbl(r.get("reference")), _lbl(r.get("product_id")), _lbl(r.get("lot_id")), _lbl(r.get("location_id")), _lbl(r.get("location_dest_id")), mt, qi, qo, nq, _lbl(r.get("product_uom_id"))])
        wb.close()
        return len(rows), f"- **Qty In:** `{tin:,.2f}` | **Qty Out:** `{tout:,.2f}` | **Net:** `{tnet:,.2f}`"

    if any(k in key for k in ("payroll", "payslip", "salary", "wage", "hr.payslip")):
        pf, _ = _safe_fields(url, db, uid, pw, "hr.payslip", ["id", "number", "employee_id", "department_id", "date_from", "date_to", "state"])
        slips = rpc_fetch_all(url, db, uid, pw, "hr.payslip", [["date_from", ">=", d1], ["date_to", "<=", d2]], pf, 2000, progress, f"{rep} (Payslips)", 0.1, 0.45)
        if not slips:
            wb.close(); return 0, "No payslips found."
        smap = {s["id"]: s for s in slips}
        lf, _ = _safe_fields(url, db, uid, pw, "hr.payslip.line", ["id", "slip_id", "employee_id", "code", "name", "category_id", "amount", "total"])
        lines, sids = [], list(smap.keys())
        for i in range(0, len(sids), 1000):
            progress(0.45 + 0.4 * (i / max(len(sids), 1)), desc="Fetching Payroll Lines...")
            lines.extend(execute_with_uid(url, db, uid, pw, "hr.payslip.line", "search_read", [[["slip_id", "in", sids[i:i+1000]]]], {"fields": lf, "limit": 50000}, timeout=180))
        ws = wb.add_worksheet("Payroll_Data")
        ws.write_row(0, 0, ["Line_ID", "Payslip", "Employee", "Department", "From", "To", "Code", "Rule", "Category", "Amount", "Total"])
        tnet = 0.0
        for i, ln in enumerate(lines, 1):
            s = smap.get(_id(ln.get("slip_id")), {})
            tv = float(ln.get("total") or ln.get("amount") or 0.0)
            if str(ln.get("code") or "").upper() in ("NET", "NETPAY"): tnet += tv
            ws.write_row(i, 0, [ln["id"], _lbl(s.get("number")), _lbl(ln.get("employee_id")) or _lbl(s.get("employee_id")), _lbl(s.get("department_id")), _lbl(s.get("date_from")), _lbl(s.get("date_to")), _lbl(ln.get("code")), _lbl(ln.get("name")), _lbl(ln.get("category_id")), float(ln.get("amount") or 0.0), tv])
        wb.close()
        return len(lines) or len(slips), f"- **Payslips:** `{len(slips):,}` | **Lines:** `{len(lines):,}` | **Net Pay:** `{tnet:,.2f}`"

    if any(k in key for k in ("employee", "hr.employee", "staff")):
        ef, _ = _safe_fields(url, db, uid, pw, "hr.employee", ["id", "registration_number", "name", "department_id", "job_id", "work_email", "mobile_phone", "identification_id", "tin_no", "nssf_no", "active"])
        emps = rpc_fetch_all(url, db, uid, pw, "hr.employee", [["active", "in", [True, False]]], ef, 2000, progress, f"{rep} (Employees)", 0.15, 0.85)
        if not emps:
            wb.close(); return 0, "No employees found."
        ws = wb.add_worksheet("Employees")
        ws.write_row(0, 0, [f.upper() for f in ef])
        for i, e in enumerate(emps, 1): ws.write_row(i, 0, [_lbl(e.get(f)) for f in ef])
        wb.close()
        return len(emps), f"- **Employees Exported:** `{len(emps):,}`"

    if any(k in key for k in ("ledger", "trial", "balance", "aged", "aging", "receivable", "payable", "tax", "vat", "efd", "tansad", "statement", "summary", "breakup", "mis", "provision", "reconcile", "account", "journal", "cash", "bank", "expense", "asset")):
        af, _ = _safe_fields(url, db, uid, pw, "account.move.line", ["id", "date", "date_maturity", "move_name", "journal_id", "account_id", "partner_id", "tin_no", "vat", "ref", "name", "product_id", "quantity", "price_unit", "debit", "credit", "balance", "amount_residual", "reconciled"])
        aged = any(k in key for k in ("aged", "aging", "receivable", "payable"))
        dom = [["parent_state", "=", "posted"], ["date", "<=", d2], ["reconciled", "=", False]] if aged else [["parent_state", "=", "posted"], ["date", ">=", d1], ["date", "<=", d2]]
        if acc_ids: dom.append(["account_id", "in", acc_ids])
        elif "receivable" in key: dom.append(["account_id.account_type", "=", "asset_receivable"])
        elif "payable" in key: dom.append(["account_id.account_type", "=", "liability_payable"])
        elif aged: dom.append(["account_id.account_type", "in", ["asset_receivable", "liability_payable"]])
        rows = rpc_fetch_all(url, db, uid, pw, "account.move.line", dom, af, 2500, progress, f"{rep} (GL Lines)", 0.1, 0.82)
        if not rows:
            wb.close(); return 0, "No posted GL entries found."
        ws, ws_tb = wb.add_worksheet("Report_GL_Data"), wb.add_worksheet("Trial_Balance_Check")
        ws.write_row(0, 0, ["AML_ID", "Date", "Due Date", "Document", "Journal", "Account", "Partner", "TIN", "VAT", "Ref", "Label", "Product", "Qty", "Price", "Debit", "Credit", "Balance", "Residual"])
        acctot, tdr, tcr = {}, 0.0, 0.0
        for i, r in enumerate(rows, 1):
            al = _lbl(r.get("account_id"))
            dr, cr, bal, res = float(r.get("debit") or 0.0), float(r.get("credit") or 0.0), float(r.get("balance") or 0.0), float(r.get("amount_residual") or 0.0)
            tdr += dr; tcr += cr
            pd_s, pc_s = acctot.get(al, (0.0, 0.0)); acctot[al] = (pd_s + dr, pc_s + cr)
            ws.write_row(i, 0, [r["id"], _lbl(r.get("date")), _lbl(r.get("date_maturity")), _lbl(r.get("move_name")), _lbl(r.get("journal_id")), al, _lbl(r.get("partner_id")), _lbl(r.get("tin_no")), _lbl(r.get("vat")), _lbl(r.get("ref")), _lbl(r.get("name")), _lbl(r.get("product_id")), float(r.get("quantity") or 0.0), float(r.get("price_unit") or 0.0), dr, cr, bal, res])
        ws_tb.write_row(0, 0, ["Account", "Debit (TB Exact)", "Credit (TB Exact)", "Net (Dr - Cr)"])
        for i, (an, (ds, cs)) in enumerate(sorted(acctot.items()), 1): ws_tb.write_row(i, 0, [an, ds, cs, ds - cs])
        ws_tb.write_row(len(acctot) + 2, 0, ["GRAND TOTAL", tdr, tcr, tdr - tcr])
        wb.close()
        return len(rows), f"- **Debits (TB Exact):** `{tdr:,.2f}` | **Credits (TB Exact):** `{tcr:,.2f}` | **Net:** `{tdr - tcr:,.2f}`"

    _, fm = _safe_fields(url, db, uid, pw, mod, [])
    if not fm: fm = execute_with_uid(url, db, uid, pw, mod, "fields_get", [], {"attributes": ["type", "string"]}, timeout=60)
    ex = [k for k, v in fm.items() if k not in {"create_uid", "create_date", "write_uid", "write_date", "__last_update"} and not k.startswith("message_") and v.get("type") not in ("binary", "one2many", "many2many")]
    dom = [["date", ">=", d1], ["date", "<=", d2]] if "date" in fm else ([["create_date", ">=", f"{d1} 00:00:00"], ["create_date", "<=", f"{d2} 23:59:59"]] if "create_date" in fm else [])
    rows = rpc_fetch_all(url, db, uid, pw, mod, dom, ex, 2000, progress, f"{rep} (Records)", 0.15, 0.85)
    ws = wb.add_worksheet("Report_Data")
    ws.write_row(0, 0, [fm[f].get("string") or f for f in ex])
    for i, r in enumerate(rows, 1): ws.write_row(i, 0, [_lbl(r.get(f)) for f in ex])
    wb.close()
    return len(rows), f"- **Model:** `{mod}` | **Rows:** `{len(rows):,}`"
def extract_kardex_fast(url, db, uid, pw, d_from, d_to, out_xlsx_path, progress):
    d1, d2 = to_odoo_date(d_from), to_odoo_date(d_to)
    f_meta = execute_with_uid(url, db, uid, pw, "stock.move.line", "fields_get", [], {"attributes": ["type"]}, timeout=60)
    wanted = ["id", "date", "reference", "picking_id", "product_id", "lot_id", "location_id", "location_dest_id", "qty_done", "quantity", "product_uom_id"]
    flds = [f for f in wanted if f in f_meta]
    qcol = "qty_done" if "qty_done" in flds else "quantity"

    dom = [["state", "=", "done"], ["date", ">=", f"{d1} 00:00:00"], ["date", "<=", f"{d2} 23:59:59"]]
    rows = rpc_fetch_all(url, db, uid, pw, "stock.move.line", dom, flds, 2500, progress, "Kardex Stock Moves", 0.10, 0.80)
    if not rows:
        return 0, 0.0, 0.0, 0.0

    locs = execute_with_uid(url, db, uid, pw, "stock.location", "search_read", [[["usage", "=", "internal"]]], {"fields": ["id"]}, timeout=60)
    int_ids = {l["id"] for l in locs}

    lbl = lambda v: str(v[-1]) if isinstance(v, list) and v else ("" if v in (False, None) else str(v))
    rid = lambda v: v[0] if isinstance(v, list) and v else (v if isinstance(v, int) else 0)

    wb = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})
    ws = wb.add_worksheet("Kardex_Stock_Ledger")
    ws.write_row(0, 0, ["Move_Line_ID", "Date", "Reference / Picking", "Product", "Lot / Serial", "From Location", "To Location", "Movement Type", "Qty In", "Qty Out", "Net Qty", "UOM"])

    tin = tout = tnet = 0.0
    for i, r in enumerate(rows, 1):
        si, di, q = rid(r.get("location_id")) in int_ids, rid(r.get("location_dest_id")) in int_ids, float(r.get(qcol) or 0.0)
        mt, qi, qo, nq = ("INWARD", q, 0.0, q) if (not si and di) else (("OUTWARD", 0.0, q, -q) if (si and not di) else ("INTERNAL", q, q, 0.0))
        tin += qi; tout += qo; tnet += nq
        ws.write_row(i, 0, [r["id"], lbl(r.get("date")), lbl(r.get("picking_id")) or lbl(r.get("reference")), lbl(r.get("product_id")), lbl(r.get("lot_id")), lbl(r.get("location_id")), lbl(r.get("location_dest_id")), mt, qi, qo, nq, lbl(r.get("product_uom_id"))])

    wb.close()
    return len(rows), tin, tout, tnet

def download_native_odoo_wizard(url, db, uid, pw, opener, clean_base, w, base_vals, btn_method, d_from, d_to, user_ctx, timeout=90):
    model = w["res_model"]
    f_info = execute_with_uid(url, db, uid, pw, model, "fields_get", [], {"attributes": ["type", "string", "required", "relation"]}, timeout=30)
    ignore = {"id", "create_uid", "create_date", "write_uid", "write_date", "__last_update", "display_name"}
    valid_fields = [k for k in f_info if k not in ignore and not k.startswith("message_") and f_info[k].get("type") != "binary"]

    try:
        vals = execute_with_uid(url, db, uid, pw, model, "default_get", [valid_fields], {"context": user_ctx}, timeout=30) or {}
    except Exception:
        vals = {}

    for k, v in (base_vals or {}).items():
        if k in f_info:
            vals[k] = v

    clean_from, clean_to = to_odoo_date(d_from), to_odoo_date(d_to)
    has_date = False
    for fname in valid_fields:
        meta = f_info[fname]
        ftype = meta.get("type")
        if ftype in ("date", "datetime"):
            has_date = True
            low = f"{fname.lower()} {str(meta.get('string') or '').lower()}"
            is_start = any(x in low for x in ("from", "start", "begin"))
            raw_d = clean_from if is_start else clean_to
            if raw_d:
                vals[fname] = f"{raw_d} 00:00:00" if ftype == "datetime" else raw_d
        elif ftype == "many2one" and meta.get("relation") and meta.get("required") and not vals.get(fname):
            try:
                recs = execute_with_uid(url, db, uid, pw, meta["relation"], "search_read", [[]], {"fields": ["id"], "order": "id desc", "limit": 1}, timeout=20)
                if recs:
                    vals[fname] = recs["id"]
            except Exception:
                pass

    # Match date-scoped relational fields (e.g. Payslip Batches, Payslips, Periods) to From/To Date automatically
    if clean_from and clean_to:
        date_rel_map = {
            "hr.payslip.run": [["date_start", "<=", clean_to], ["date_end", ">=", clean_from]],
            "hr.payslip": [["date_from", "<=", clean_to], ["date_to", ">=", clean_from], ["state", "!=", "cancel"]],
            "account.period": [["date_start", "<=", clean_to], ["date_stop", ">=", clean_from]],
        }
        for fname in valid_fields:
            meta = f_info[fname]
            rel = meta.get("relation")
            if rel in date_rel_map and not vals.get(fname):
                try:
                    matched = execute_with_uid(url, db, uid, pw, rel, "search_read", [date_rel_map[rel]], {"fields": ["id"], "limit": 20}, timeout=20)
                    if matched:
                        ids = [r["id"] for r in matched]
                        vals[fname] = [[6, 0, ids]] if meta.get("type") == "many2many" else ids
                except Exception:
                    pass

    # Trigger Odoo's native onchange so hidden fields auto-populate just like in Odoo's browser popup
    try:
        spec = {f: "1" for f in valid_fields}
        oc = execute_with_uid(url, db, uid, pw, model, "onchange", [[], vals, list(vals.keys()), spec], {"context": user_ctx}, timeout=30)
        if isinstance(oc, dict) and isinstance(oc.get("value"), dict):
            for k, v in oc["value"].items():
                if k in f_info and k not in vals and v is not False:
                    vals[k] = v[0] if isinstance(v, list) and len(v) == 2 and isinstance(v, int) else v
    except Exception:
        pass

    wiz_id = execute_with_uid(url, db, uid, pw, model, "create", [vals], {"context": user_ctx}, timeout=timeout)

    prof_btns = w.get("profile", {}).get("buttons", [])
    candidates = list(dict.fromkeys(
        ([btn_method.strip()] if btn_method and btn_method.strip() else [])
        + prof_btns
        + ["action_print_report", "print_report", "generate_report", "export_xls", "action_print_xlsx", "action_generate_report", "check_report"]
    ))
    res_action, used_m, last_err = None, None, None
    for m in candidates:
        try:
            res_action = execute_with_uid(url, db, uid, pw, model, m, [[wiz_id]], {"context": user_ctx}, timeout=timeout)
            used_m = m
            break
        except Exception as e:
            if "cannot marshal None" in str(e):
                used_m = m
                break
            if "has no attribute" in str(e) or "does not exist" in str(e):
                last_err = e
                continue
            raise
    if used_m is None and last_err:
        raise last_err

    file_bytes, fname_out = None, f"{w['name'].replace(' ', '_')}.xlsx"

    if isinstance(res_action, dict) and res_action.get("report_name"):
        rep_type = str(res_action.get("report_type") or "qweb-pdf")
        doc_ids = res_action.get("context", {}).get("active_ids") or [wiz_id]
        route_prefix = "xlsx" if "xlsx" in rep_type else ("html" if "html" in rep_type else "pdf")
        ids_str = ",".join(str(x) for x in doc_ids) if isinstance(doc_ids, list) else str(doc_ids)
        report_url = f"{clean_base}/report/{route_prefix}/{res_action['report_name']}/{ids_str}"
        with opener.open(report_url, timeout=timeout) as resp:
            file_bytes = resp.read()
        ext = "xlsx" if "xlsx" in rep_type else ("html" if "html" in rep_type else "pdf")
        fname_out = f"{w['name'].replace(' ', '_')}.{ext}"

    elif isinstance(res_action, dict) and res_action.get("url"):
        dl_url = res_action["url"]
        full_url = dl_url if dl_url.startswith("http") else f"{clean_base}{dl_url}"
        with opener.open(full_url, timeout=timeout) as resp:
            file_bytes = resp.read()
        tail = dl_url.split("/")[-1].split("?")
        if "." in tail:
            fname_out = tail

    if not file_bytes:
        bin_fields = [k for k, v in f_info.items() if v.get("type") == "binary"]
        char_fields = [k for k, v in f_info.items() if v.get("type") == "char"]
        if bin_fields:
            recs = execute_with_uid(url, db, uid, pw, model, "read", [[wiz_id]], {"fields": bin_fields + char_fields, "context": user_ctx}, timeout=30)
            rec = next(iter(recs), {}) if isinstance(recs, list) else (recs or {})
            fname_out = next((rec[cf] for cf in char_fields if rec.get(cf) and "." in str(rec[cf])), fname_out)
            for bf in bin_fields:
                if rec.get(bf):
                    file_bytes = base64.b64decode(rec[bf])
                    break

    if not file_bytes:
        atts = execute_with_uid(url, db, uid, pw, "ir.attachment", "search_read", [[["res_model", "=", model], ["res_id", "=", wiz_id]]], {"fields": ["name", "datas"], "order": "id desc", "limit": 1, "context": user_ctx}, timeout=30)
        att = next(iter(atts), {}) if isinstance(atts, list) else (atts or {})
        if att.get("datas"):
            file_bytes = base64.b64decode(att["datas"])
            fname_out = att["name"]

    return file_bytes, fname_out, used_m

def run_wizard_and_download(url, db, user, pw, selected_label, wiz_map, params_json, btn_method, d_from, d_to, is_as_on, selected_accs, acc_map, progress=gr.Progress()):
    STOP_EVENT.clear()
    if not d_from.strip() or not d_to.strip():
        return "⚠️ Please enter both From Date and To Date (DD-MM-YYYY).", None
    try:
        progress(0.02, desc="Authenticating with Odoo...")
        uid = odoo_jsonrpc(url, "common", "authenticate", [db, user, pw, {}], timeout=60)
        if not uid:
            raise Exception("Authentication failed.")
        sel_label_low = str(selected_label or "").lower()
        w = wiz_map[selected_label]
        clean_rep_name = re.sub(r"[^A-Za-z0-9_-]+", "_", w["name"].strip()).strip("_")
        sel_low = w["name"].lower()
        sel_ids = [acc_map[a] for a in selected_accs if a in acc_map] if selected_accs else list()

        if not selected_label or "grir" in sel_label_low:
            out_path = os.path.join(tempfile.gettempdir(), f"GRIR_Transport_Breakup_{d_from.strip()}_to_{d_to.strip()}.xlsx")
            if os.path.exists(out_path): os.remove(out_path)
            row_cnt, tot_dr, tot_cr, net_bal = extract_grir_transport_fast(url, db, uid, pw, sel_ids, d_from.strip(), d_to.strip(), out_path, progress)
            if row_cnt == 0: return "⚠️ No posted entries found for the selected GRIR account and date range.", None
            return f"✅ **Extracted {row_cnt:,} rows in seconds (100% Trial Balance Exact)!**\n- **Total Debits (TBS Bills):** `{tot_dr:,.2f}`\n- **Total Credits (LC Accruals):** `{tot_cr:,.2f}`\n- **Net GL Movement:** `{net_bal:,.2f}`", out_path

        if "sale" in sel_low:
            out_path = os.path.join(tempfile.gettempdir(), f"{clean_rep_name}_{d_from.strip()}_to_{d_to.strip()}.xlsx")
            if os.path.exists(out_path): os.remove(out_path)
            row_cnt, net_rev, tot_dr, tot_cr = extract_sale_register_fast(url, db, uid, pw, d_from.strip(), d_to.strip(), out_path, progress, sel_ids)
            if row_cnt == 0: return "⚠️ No posted revenue entries found in this date range.", None
            return f"✅ **{w['name']}** — Extracted {row_cnt:,} lines (100% Trial Balance Exact)!\n- **Total GL Debits:** `{tot_dr:,.2f}`\n- **Total GL Credits:** `{tot_cr:,.2f}`\n- **Net Revenue (Credit - Debit):** `{net_rev:,.2f}`", out_path

        if "purchase" in sel_low:
            out_path = os.path.join(tempfile.gettempdir(), f"{clean_rep_name}_{d_from.strip()}_to_{d_to.strip()}.xlsx")
            if os.path.exists(out_path): os.remove(out_path)
            row_cnt, inv_dr, inv_cr, inv_net, grir_dr, grir_cr, grir_net = extract_purchase_register_fast(url, db, uid, pw, d_from.strip(), d_to.strip(), out_path, progress, sel_ids)
            if row_cnt == 0: return "⚠️ No posted Inventory or GRIR entries found in this date range.", None
            return f"✅ **{w['name']}** — Extracted {row_cnt:,} lines (100% Trial Balance Exact)!\n- **Inventory (`551xxx` + `731004` + `751018`):** Debit `{inv_dr:,.2f}` | Credit `{inv_cr:,.2f}` | Net `{inv_net:,.2f}`\n- **GRIR (`181001`–`181004`):** Debit `{grir_dr:,.2f}` | Credit `{grir_cr:,.2f}` | Net `{grir_net:,.2f}`", out_path
        # 3B. Kardex Report (Fast direct RPC — prevents Odoo server timeout)
        if "kardex" in sel_low or "kardex" in sel_label_low:
            out_path = os.path.join(tempfile.gettempdir(), f"{clean_rep_name}_{d_from.strip()}_to_{d_to.strip()}.xlsx")
            if os.path.exists(out_path): os.remove(out_path)
            row_cnt, tin, tout, tnet = extract_kardex_fast(url, db, uid, pw, d_from.strip(), d_to.strip(), out_path, progress)
            if row_cnt == 0: return "⚠️ No completed stock movements found in this date range.", None
            return f"✅ **{w['name']}** — Extracted {row_cnt:,} stock movements in seconds!\n- **Total Qty In:** `{tin:,.2f}` | **Qty Out:** `{tout:,.2f}` | **Net Change:** `{tnet:,.2f}`", out_path

        final_json = sync_ui_to_json(params_json, d_from, d_to, is_as_on, selected_accs, acc_map, selected_label, wiz_map)
        base_vals = json.loads(final_json) if final_json.strip() else {}
        opener, clean_base = get_auth_opener(url, db, user, pw)
        try:
            user_ctx = execute_with_uid(url, db, uid, pw, "res.users", "context_get", list(), timeout=60)
        except Exception:
            user_ctx = {}

        progress(0.25, desc=f"Running native Odoo report '{w['name']}'...")
        file_bytes, fname_out, used_m = download_native_odoo_wizard(url, db, uid, pw, opener, clean_base, w, base_vals, btn_method, d_from.strip(), d_to.strip(), user_ctx, timeout=300)
        progress(1.0, desc="Native Odoo report ready!")
        if file_bytes:
            ext = os.path.splitext(fname_out) or ".xlsx"
            out_path = os.path.join(tempfile.gettempdir(), f"{clean_rep_name}_{d_from.strip()}_to_{d_to.strip()}{ext}")
            with open(out_path, "wb") as f:
                f.write(file_bytes)
            return f"✅ **{w['name']}** downloaded directly from Odoo ERP via `{used_m}`!", out_path
        return f"⚠️ Executed `{used_m}` on `{w['res_model']}`, but Odoo returned no file.", None
    except Exception as e:
        return f"❌ Error: {str(e)}", None

def disconnect_session():
    STOP_EVENT.set()
    return ("", "🔒 Disconnected. Session cleared.", gr.update(choices=list(), value=None), {}, "", "{}", gr.update(choices=list(), value=list()), {}, "", None)

with gr.Blocks(title="Odoo Report & Ledger Center - MGR Dogra") as demo:
    gr.Markdown("# 📋 Odoo Native Report & Ledger Center\n#### *App by Muneesh Kumar Dogra (MGR Dogra)*")
    wiz_state, acc_state = gr.State({}), gr.State({})
    with gr.Row():
        in_url = gr.Textbox(label="Odoo URL", value="https://erp.lakecement.co.tz/")
        in_db = gr.Textbox(label="Database", value="parallel")
        in_user = gr.Textbox(label="Odoo Email / Username")
        in_pass = gr.Textbox(label="Odoo Password", type="password")
    with gr.Row():
        btn_connect = gr.Button("🔓 Connect & Discover Wizards", variant="primary")
        btn_disconnect = gr.Button("🔒 Disconnect & Clear Session", variant="secondary")
    status_box = gr.Markdown()
    wiz_dropdown = gr.Dropdown(label="Select Custom Report Wizard", choices=list())
    btn_load = gr.Button("⚙️ Load Report Parameters")
    wiz_details = gr.Markdown()
    with gr.Row():
        in_from_date = gr.Textbox(label="From Date (DD-MM-YYYY)", value="", placeholder="e.g. 01-04-2024")
        in_to_date = gr.Textbox(label="To Date (DD-MM-YYYY)", value="", placeholder="e.g. 31-10-2026")
        in_as_on = gr.Checkbox(label="Print As On", value=False)
    in_accounts = gr.Dropdown(label="Select Accounts (leave blank for default, or pick specific accounts)", choices=list(), multiselect=True)
    with gr.Row():
        params_editor = gr.Code(label="Wizard Parameters (JSON)", language="json", value="{}")
        btn_method_in = gr.Textbox(label="Button Method Name", value="action_print_report")
    with gr.Row():
        btn_generate = gr.Button("🖨️ Generate & Download Report", variant="primary")
        btn_stop = gr.Button("⏹️ Stop / Cancel Generation", variant="stop")
    gen_status = gr.Markdown()
    download_file = gr.File(label="📥 Download Generated Report")

    btn_connect.click(connect_and_discover, [in_url, in_db, in_user, in_pass], [status_box, wiz_dropdown, wiz_state])
    btn_disconnect.click(disconnect_session, list(), [in_pass, status_box, wiz_dropdown, wiz_state, wiz_details, params_editor, in_accounts, acc_state, gen_status, download_file])
    wiz_dropdown.change(inspect_wizard, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, in_from_date, in_to_date], [wiz_details, params_editor, btn_method_in, in_as_on, in_accounts, acc_state])
    btn_load.click(inspect_wizard, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, in_from_date, in_to_date], [wiz_details, params_editor, btn_method_in, in_as_on, in_accounts, acc_state])
    for ctrl in [in_from_date, in_to_date, in_as_on, in_accounts]:
        ctrl.change(sync_ui_to_json, [params_editor, in_from_date, in_to_date, in_as_on, in_accounts, acc_state, wiz_dropdown, wiz_state], [params_editor])
    gen_event = btn_generate.click(run_wizard_and_download, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, params_editor, btn_method_in, in_from_date, in_to_date, in_as_on, in_accounts, acc_state], [gen_status, download_file])
    btn_stop.click(stop_generation, list(), [gen_status], cancels=[gen_event])

gr.close_all()
print(f"✅ Local URL: {local_url}", flush=True)
print(f"🌍 Public URL: {share_url}", flush=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port, share=False)
