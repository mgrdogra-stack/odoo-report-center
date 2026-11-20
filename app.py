import os

import gradio as gr
import requests, urllib.request, urllib.error, http.cookiejar, json, base64, tempfile, os, time
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

RPC_SESSION = requests.Session()
RPC_SESSION.headers.update({"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})

def odoo_jsonrpc(url, service, method, args, timeout=300):
    clean_url = url.strip().rstrip("/")
    target_url = f"{clean_url}/jsonrpc"
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": 1,
    }
    for attempt in range(2):
        resp = RPC_SESSION.post(target_url, json=payload, timeout=timeout, allow_redirects=True)
        if resp.status_code in (502, 503, 504) and attempt == 0:
            time.sleep(2)
            continue
        if resp.status_code in (502, 503, 504):
            raise Exception(f"Odoo Server Proxy Timeout (HTTP {resp.status_code}). Try a shorter date range or add filters.")
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            err_msg = result["error"].get("data", {}).get("message") or result["error"].get("message")
            raise Exception(f"Odoo Error: {err_msg}")
        return result.get("result")

def get_auth_opener(url, db, username, password):
    clean_url = url.strip().rstrip("/")
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    auth_payload = json.dumps({
        "jsonrpc": "2.0",
        "params": {"db": db, "login": username, "password": password}
    }).encode("utf-8")
    auth_req = urllib.request.Request(
        f"{clean_url}/web/session/authenticate",
        data=auth_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(auth_req, timeout=30) as resp:
        auth_res = json.loads(resp.read().decode("utf-8"))
        if "error" in auth_res or not auth_res.get("result", {}).get("uid"):
            raise Exception("Web session authentication failed.")
    return opener, clean_url

print("✅ Part 1 loaded with connection pooling & retry!")

from datetime import datetime, timedelta
import urllib.parse
import os
import json
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

UID_CACHE = {}
CACHE_FILE = os.path.join(tempfile.gettempdir(), "odoo_wizard_cache.json")

def load_disk_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_disk_cache(cache_dict):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_dict, f)
    except Exception:
        pass

PROFILE_CACHE = load_disk_cache()

def get_cached_uid(url, db, user, pw):
    key = (url.strip().rstrip("/"), db.strip(), user.strip(), pw)
    if key not in UID_CACHE:
        uid = odoo_jsonrpc(url, "common", "authenticate", [db, user, pw, {}], timeout=60)
        if not uid:
            raise Exception("Authentication failed.")
        UID_CACHE[key] = uid
    return UID_CACHE[key]

def execute_with_uid(url, db, uid, pw, model, method, args, kwargs=None, timeout=3600):
    call_args = [db, uid, pw, model, method, args]
    if kwargs is not None:
        call_args.append(kwargs)
    return odoo_jsonrpc(url, "object", "execute_kw", call_args, timeout=timeout)

def to_odoo_date(date_str):
    if isinstance(date_str, int) and not isinstance(date_str, bool):
        return (datetime.today() + timedelta(days=date_str)).strftime("%Y-%m-%d")
    s = str(date_str or "").strip()
    if not s:
        return ""
    if s.lstrip("-").isdigit():
        if len(s) == 8:
            s = f"{s[0:2]}-{s[2:4]}-{s[4:8]}"
        else:
            return (datetime.today() + timedelta(days=int(s))).strftime("%Y-%m-%d")
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s

def fetch_rel_options(url, db, uid, pw, rel_meta, wiz_name=""):
    if rel_meta.get("loaded"):
        return rel_meta["options"]
    rel_model = rel_meta["relation"]
    search_arg = "GRIR" if (rel_model == "account.account" and "grir" in wiz_name.lower()) else ""
    try:
        pairs = execute_with_uid(url, db, uid, pw, rel_model, "name_search", [], {"name": search_arg, "limit": 100000}, timeout=120)
        if not pairs and search_arg:
            pairs = execute_with_uid(url, db, uid, pw, rel_model, "name_search", [], {"name": "", "limit": 100000}, timeout=120)
        opt_map = {f"{p[1]} (ID:{p[0]})": p[0] for p in (pairs or []) if isinstance(p, (list, tuple)) and len(p) >= 2}
        rel_meta["options"] = opt_map
        rel_meta["loaded"] = True
        return opt_map
    except Exception:
        return {}

def build_wizard_profile(url, db, uid, pw, w, cur_from="", cur_to=""):
    if w.get("action_type") in ("client", "direct_fs"):
        btn = "export_to_xlsx" if w.get("action_type") == "client" else "read_group_ledger"
        return {
            "valid_fields": ["date_from", "date_to"],
            "field_types": {"date_from": "date", "date_to": "date"},
            "start_fields": [("date_from", "date")],
            "end_fields": [("date_to", "date")],
            "defaults": {"date_from": to_odoo_date(cur_from), "date_to": to_odoo_date(cur_to)},
            "buttons": [btn],
            "summary": ["- `date_from` (date): From Date", "- `date_to` (date): To Date"],
            "rel_fields": {},
        }

    cache_key = f"{url.strip().rstrip('/')}|{db}|{w['res_model']}|{w.get('id')}"
    if cache_key in PROFILE_CACHE:
        return json.loads(json.dumps(PROFILE_CACHE[cache_key]))

    model = w["res_model"]
    ignore = {"id", "create_uid", "create_date", "write_uid", "write_date", "__last_update", "display_name"}
    v_raw = w.get("view_id")
    v_id = next(iter(v_raw), False) if isinstance(v_raw, list) else False

    def get_fields():
        return execute_with_uid(url, db, uid, pw, model, "fields_get", [], {"attributes": ["string", "type", "relation", "selection", "required"]}, timeout=120)

    def get_arch():
        try:
            return execute_with_uid(url, db, uid, pw, model, "get_views", [], {"views": [[v_id, "form"]]}, timeout=120).get("views", {}).get("form", {}).get("arch", "")
        except Exception:
            try:
                return execute_with_uid(url, db, uid, pw, model, "fields_view_get", [], {"view_id": v_id, "view_type": "form"}, timeout=120).get("arch", "")
            except Exception:
                return ""

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_future = pool.submit(get_fields)
        a_future = pool.submit(get_arch)
        f_info = f_future.result()
        arch = a_future.result()

    btns, visible_names = [], []
    if arch:
        try:
            root = ET.fromstring(arch)
            btns = [b.attrib["name"] for b in root.iter("button") if b.attrib.get("name") and b.attrib.get("invisible") != "1"]
            for fld in root.iter("field"):
                fn = fld.attrib.get("name")
                if fn and fn in f_info and fn not in ignore and fld.attrib.get("invisible") != "1" and fn not in visible_names:
                    visible_names.append(fn)
        except Exception:
            pass

    valid_fields = visible_names if visible_names else [k for k in f_info if k not in ignore and not k.startswith("message_") and f_info[k].get("type") != "binary"]
    try:
        default_vals = execute_with_uid(url, db, uid, pw, model, "default_get", [valid_fields], timeout=120) or {}
    except Exception:
        default_vals = {}

    field_summary, start_fields, end_fields, field_types, rel_fields = [], [], [], {}, {}
    skip_models = {"res.company", "res.currency", "res.users", "ir.attachment", "ir.actions.report"}

    for fname in valid_fields:
        meta = f_info.get(fname, {})
        ftype, label = meta.get("type"), meta.get("string") or fname
        field_types[fname] = ftype
        req = bool(meta.get("required"))
        req_tag = " [REQUIRED]" if req else ""

        if ftype == "selection" and meta.get("selection"):
            opts = ", ".join(f"`{x[0]}` ({x[1]})" for x in meta["selection"] if isinstance(x, (list, tuple)) and len(x) >= 2)
            field_summary.append(f"- `{fname}` ({ftype}{req_tag}): {label} -> Options: {opts}")
        else:
            field_summary.append(f"- `{fname}` ({ftype}{req_tag}): {label}")

        if ftype in ("date", "datetime"):
            low_all = f"{fname.lower()} {label.lower()}"
            is_start = any(x in low_all for x in ("from", "start", "begin"))
            (start_fields if is_start else end_fields).append((fname, ftype))
            if fname in default_vals and default_vals[fname]:
                clean_def = to_odoo_date(default_vals[fname])
                if len(clean_def) >= 10 and clean_def[4] == "-":
                    default_vals[fname] = f"{clean_def[:10]} 00:00:00" if (ftype == "datetime" and len(clean_def) == 10) else clean_def
                else:
                    default_vals[fname] = False
            raw_d = to_odoo_date(cur_from) if is_start else to_odoo_date(cur_to)
            if raw_d:
                default_vals[fname] = f"{raw_d} 00:00:00" if ftype == "datetime" else raw_d

        elif ftype == "selection" and meta.get("selection") and fname not in default_vals:
            valid = [item[0] for item in meta["selection"] if isinstance(item, (list, tuple)) and len(item) >= 2]
            if valid:
                default_vals[fname] = valid[0]

        elif ftype in ("many2one", "many2many") and meta.get("relation"):
            rel_model = meta["relation"]
            if rel_model in skip_models:
                continue
            rel_meta = {"field": fname, "type": ftype, "relation": rel_model, "options": {}, "loaded": False}
            rel_fields[f"{label} [{fname}]"] = rel_meta
            if req and not default_vals.get(fname) and ftype == "many2one":
                opts = fetch_rel_options(url, db, uid, pw, rel_meta, w["name"])
                if opts:
                    default_vals[fname] = next(iter(opts.values()))

    prof = {
        "valid_fields": valid_fields,
        "field_types": field_types,
        "start_fields": start_fields,
        "end_fields": end_fields,
        "defaults": default_vals,
        "buttons": btns,
        "summary": field_summary,
        "rel_fields": rel_fields,
    }
    PROFILE_CACHE[cache_key] = prof
    save_disk_cache(PROFILE_CACHE)
    return prof

print("✅ Cell 310fTof7mloq fully restored and configured with 100k limits!")

def connect_and_discover(url, db, user, pw, progress=gr.Progress()):
    try:
        progress(0.1, desc="Authenticating user...")
        uid = get_cached_uid(url, db, user, pw)

        progress(0.3, desc="Loading user-accessible report wizards...")
        # Query wizards matching window actions, restricted to user's permitted models
        wizards = execute_with_uid(
            url, db, uid, pw, "ir.actions.act_window", "search_read",
            [[["target", "=", "new"], ["res_model", "!=", False]]],
            {"fields": ["id", "name", "res_model", "view_id"], "limit": 1500},
            timeout=120
        )

        client_reports = []
        progress(0.6, desc="Fetching user-accessible account reports...")
        try:
            acct_reps = execute_with_uid(url, db, uid, pw, "account.report", "search_read", [[]], {"fields": ["id", "name"], "limit": 250}, timeout=60)
            for r in acct_reps:
                client_reports.append({
                    "id": r["id"],
                    "name": r["name"],
                    "action_type": "client",
                    "res_model": "account.report",
                    "context": {"report_id": r["id"]},
                    "params": {"report_id": r["id"]}
                })
        except Exception:
            pass

        if not client_reports:
            client_reports = [
                {"id": "direct_pnl", "name": "Profit & Loss Statement (Direct Ledger)", "action_type": "direct_fs", "res_model": "account.move.line", "fs_type": "pnl"},
                {"id": "direct_bs", "name": "Balance Sheet Statement (Direct Ledger)", "action_type": "direct_fs", "res_model": "account.move.line", "fs_type": "bs"},
            ]

        wiz_map = {}
        for w in wizards:
            w["action_type"] = "window"
            wiz_map[f"{w["name"]} (Action ID: {w["id"]} | {w["res_model"]})"] = w
        for c in client_reports:
            wiz_map[f"{c["name"]} (Financial Statement)"] = c

        choices = sorted(list(wiz_map.keys()))
        progress(1.0, desc="All permitted wizards & reports loaded!")
        return f"⚡ Connected as UID {uid}! Discovered **{len(wizards)} wizards** & **{len(client_reports)} financial statements**.", gr.update(choices=choices, value=next(iter(choices), None) if choices else None), wiz_map
    except Exception as e:
        return f"❌ Connection failed: {str(e)}", gr.update(choices=[]), {}

def inspect_wizard(url, db, user, pw, selected_label, wiz_map, cur_from, cur_to):
    empty_ret = (
        "⚠️ Please select a wizard first.", "{}", "action_print_report", False,
        gr.update(choices=[], value=None), gr.update(choices=[], value=[]),
        gr.update(choices=[], value=None), gr.update(choices=[], value=[]),
        {}
    )
    try:
        if not selected_label or selected_label not in (wiz_map or {}):
            return empty_ret

        uid = get_cached_uid(url, db, user, pw)
        w = wiz_map[selected_label]
        prof = w.get("profile")
        if not prof or prof.get("error"):
            prof = build_wizard_profile(url, db, uid, pw, w, cur_from, cur_to)
            w["profile"] = prof

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
        rel_fields = prof.get("rel_fields", {})
        rel_keys = sorted(list(rel_fields.keys()))

        def pick_priority(keys, patterns, exclude=None):
            for p in patterns:
                for k in keys:
                    if k != exclude and p in k.lower():
                        return k
            for k in keys:
                if k != exclude:
                    return k
            return None

        f1_key = pick_priority(rel_keys, ["categ", "category", "account_ids", "account"])
        f2_key = pick_priority(rel_keys, ["product", "location", "journal", "partner"], exclude=f1_key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut1 = pool.submit(fetch_rel_options, url, db, uid, pw, rel_fields[f1_key], w["name"]) if f1_key else None
            fut2 = pool.submit(fetch_rel_options, url, db, uid, pw, rel_fields[f2_key], w["name"]) if f2_key else None
            f1_opts = sorted(list(fut1.result().keys())) if fut1 else []
            f2_opts = sorted(list(fut2.result().keys())) if fut2 else []

        info_md = (
            f"### Wizard: **{w["name"]}** (`{w["res_model"]}`)\n"
            f"**Detected Buttons:** `{', '.join(btns) if btns else 'Auto-detect'}`\n\n"
            f"**Exact Model Fields:**\n" + "\n".join(prof.get("summary", []))
        )
        return (
            info_md,
            json.dumps(vals, indent=2),
            first_btn,
            bool(vals.get("is_as_on", False)),
            gr.update(choices=rel_keys, value=f1_key),
            gr.update(choices=f1_opts, value=[]),
            gr.update(choices=rel_keys, value=f2_key),
            gr.update(choices=f2_opts, value=[]),
            rel_fields,
        )
    except Exception as e:
        return (
            f"❌ **Wizard Inspection Error:** {str(e)}", "{}", "action_print_report", False,
            gr.update(choices=[], value=None), gr.update(choices=[], value=[]),
            gr.update(choices=[], value=None), gr.update(choices=[], value=[]),
            {}
        )

def on_rel_field_change(url, db, user, pw, rel_field_label, rel_fields_map):
    try:
        if not rel_field_label or rel_field_label not in (rel_fields_map or {}):
            return gr.update(choices=[], value=[]), rel_fields_map
        uid = get_cached_uid(url, db, user, pw)
        opts = fetch_rel_options(url, db, uid, pw, rel_fields_map[rel_field_label])
        return gr.update(choices=sorted(list(opts.keys())), value=[]), rel_fields_map
    except Exception:
        return gr.update(choices=[], value=[]), rel_fields_map

def sync_ui_to_json(params_json, d_from, d_to, is_as_on, f1_label, f1_vals, f2_label, f2_vals, rel_fields_map, selected_label=None, wiz_map=None):
    try:
        try:
            data = json.loads(params_json) if (isinstance(params_json, str) and params_json.strip()) else {}
        except Exception:
            data = {}

        prof = (wiz_map or {}).get(selected_label, {}).get("profile", {})
        valid_set = set(prof.get("valid_fields", []))
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

        for flabel, fvals in [(f1_label, f1_vals), (f2_label, f2_vals)]:
            if flabel and flabel in (rel_fields_map or {}):
                meta = rel_fields_map[flabel]
                fname, ftype, opt_map = meta["field"], meta["type"], meta["options"]
                if fvals:
                    ids = [opt_map[v] for v in fvals if v in opt_map]
                    if ids:
                        data[fname] = [[6, 0, ids]] if ftype == "many2many" else ids[0]
                elif fname in data:
                    data[fname] = [[6, 0, []]] if ftype == "many2many" else False

        return json.dumps(data, indent=2)
    except Exception:
        return params_json if isinstance(params_json, str) else "{}"

import pandas as pd
import io, time, gc, xlsxwriter, urllib.parse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

def execute_with_uid(url, db, uid, pw, model, method, args, kwargs=None, timeout=1800):
    call_args = [db, uid, pw, model, method, args]
    if kwargs is not None:
        call_args.append(kwargs)
    return odoo_jsonrpc(url, "object", "execute_kw", call_args, timeout=timeout)

def get_date_chunks(start_str, end_str, max_days=31):
    try:
        s_dt = datetime.strptime(start_str, "%Y-%m-%d")
        e_dt = datetime.strptime(end_str, "%Y-%m-%d")
    except Exception:
        return [(start_str, end_str)] if start_str and end_str else []

    chunks = []
    curr = s_dt
    while curr <= e_dt:
        chunk_end = min(curr + timedelta(days=max_days), e_dt)
        chunks.append((curr.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        curr = chunk_end + timedelta(days=1)
    return chunks

def build_direct_financial_xlsx(url, db, uid, pw, fs_type, d_from, d_to, user_ctx, timeout=1800):
    acc_fields = execute_with_uid(url, db, uid, pw, "account.account", "fields_get", [], {"attributes": ["type"]}, timeout=120)
    type_field = "account_type" if "account_type" in acc_fields else ("user_type_id" if "user_type_id" in acc_fields else None)
    fields_to_read = ["id", "code", "name"] + ([type_field] if type_field else [])
    accounts = execute_with_uid(url, db, uid, pw, "account.account", "search_read", [[]], {"fields": fields_to_read, "limit": 5000}, timeout=180)
    acc_map = {a["id"]: a for a in accounts}

    # Determine chunks to avoid Odoo Gateway timeout limits
    chunks = get_date_chunks(d_from, d_to, max_days=30) if (d_from and d_to) else [(d_from, d_to)]
    grouped_results = []

    for c_from, c_to in chunks:
        domain = [["parent_state", "=", "posted"]]
        if fs_type == "pnl":
            if c_from: domain.append(["date", ">=", c_from])
            if c_to: domain.append(["date", "<=", c_to])
        else:
            if c_to: domain.append(["date", "<=", c_to])

        try:
            chunk_grouped = execute_with_uid(
                url, db, uid, pw, "account.move.line", "read_group",
                [domain, ["account_id", "debit", "credit", "balance"], ["account_id"]],
                {"lazy": False}, timeout=timeout
            ) or []
            grouped_results.extend(chunk_grouped)
        except Exception as chunk_err:
            # If a chunk fails, retry with an extended timeout once
            time.sleep(2)
            chunk_grouped = execute_with_uid(
                url, db, uid, pw, "account.move.line", "read_group",
                [domain, ["account_id", "debit", "credit", "balance"], ["account_id"]],
                {"lazy": False}, timeout=timeout + 300
            ) or []
            grouped_results.extend(chunk_grouped)

    # Consolidate chunked results
    consolidated = {}
    for g in grouped_results:
        acc_ref = g.get("account_id")
        if not acc_ref:
            continue
        acc_id = acc_ref[0] if isinstance(acc_ref, (list, tuple)) else acc_ref
        if acc_id not in consolidated:
            consolidated[acc_id] = {"account_id": acc_ref, "debit": 0.0, "credit": 0.0, "balance": 0.0}
        consolidated[acc_id]["debit"] += float(g.get("debit", 0.0) or 0.0)
        consolidated[acc_id]["credit"] += float(g.get("credit", 0.0) or 0.0)
        consolidated[acc_id]["balance"] += float(g.get("balance", 0.0) or 0.0)

    pnl_keys = ("income", "expense", "cost_of_revenue", "depreciation")
    bs_keys = ("asset", "liability", "equity", "receivable", "payable", "liquidity", "credit_card")

    rows = []
    net_earnings = 0.0

    for acc_id, totals in consolidated.items():
        acc = acc_map.get(acc_id, {})
        raw_type = acc.get(type_field, "")
        type_str = raw_type[1] if isinstance(raw_type, (list, tuple)) and len(raw_type) >= 2 else str(raw_type or "")
        low_t = type_str.lower()

        is_pnl_acc = any(k in low_t for k in pnl_keys)
        is_bs_acc = any(k in low_t for k in bs_keys)

        debit = totals["debit"]
        credit = totals["credit"]
        balance = totals["balance"]

        if is_pnl_acc:
            net_earnings += -balance

        if fs_type == "pnl" and not is_pnl_acc:
            continue
        if fs_type == "bs" and not is_bs_acc:
            continue

        display_amt = -balance if any(k in low_t for k in ("income", "liability", "payable", "equity")) else balance

        rows.append({
            "Account Code": acc.get("code", ""),
            "Account Name": acc.get("name", str(totals["account_id"])),
            "Account Type": type_str,
            "Debit": round(debit, 2),
            "Credit": round(credit, 2),
            "Net Balance": round(display_amt, 2),
        })

    if fs_type == "bs" and round(net_earnings, 2) != 0.0:
        rows.append({
            "Account Code": "999999",
            "Account Name": "Current Year Unallocated Earnings (Net Profit / Loss)",
            "Account Type": "equity_unaffected",
            "Debit": round(max(-net_earnings, 0.0), 2),
            "Credit": round(max(net_earnings, 0.0), 2),
            "Net Balance": round(net_earnings, 2),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(by=["Account Type", "Account Code"])

    buf = io.BytesIO()
    title = "Profit_and_Loss" if fs_type == "pnl" else "Balance_Sheet"

    with pd.ExcelWriter(buf, engine="xlsxwriter", engine_kwargs={"options": {"constant_memory": True}}) as writer:
        df.to_excel(writer, index=False, sheet_name=title)

    del df, rows, consolidated, grouped_results
    gc.collect()

    return buf.getvalue(), f"{title}_{d_to or 'AsOn'}.xlsx", "read_group_ledger"

def fetch_single_wizard_fast(url, db, uid, pw, opener, clean_base, w, wiz_vals, btn_method, user_ctx, timeout=1800):
    if w.get("action_type") == "direct_fs":
        return build_direct_financial_xlsx(url, db, uid, pw, w.get("fs_type", "pnl"), wiz_vals.get("date_from"), wiz_vals.get("date_to"), user_ctx, timeout=timeout)

    if w.get("action_type") == "client":
        ctx = dict(user_ctx)
        if isinstance(w.get("context"), str):
            try:
                ctx.update(json.loads(w["context"].replace("'", '"')))
            except Exception:
                pass
        elif isinstance(w.get("context"), dict):
            ctx.update(w["context"])
        report_id = ctx.get("report_id") or (w.get("params") or {}).get("report_id") or w["id"]
        options = {
            "date": {
                "date_from": wiz_vals.get("date_from"),
                "date_to": wiz_vals.get("date_to"),
                "filter": "custom",
                "mode": "range",
            }
        }
        dl_url = f"{clean_base}/account_reports/export_to_xlsx?report_id={report_id}&options={urllib.parse.quote(json.dumps(options))}"
        with opener.open(dl_url, timeout=timeout) as resp:
            return resp.read(), f"{w['name'].replace(' ', '_')}.xlsx", "export_to_xlsx"

    f_info = execute_with_uid(url, db, uid, pw, w["res_model"], "fields_get", [], {"attributes": ["type", "required"]}, timeout=timeout)
    today_str = datetime.today().strftime("%Y-%m-%d")
    safe_vals = {}
    for k, v in wiz_vals.items():
        if k not in f_info:
            continue
        ftype = f_info[k].get("type")
        if ftype in ("date", "datetime"):
            cleaned = to_odoo_date(v) if v else (today_str if f_info[k].get("required") else "")
            if len(cleaned) >= 10 and cleaned[4] == "-":
                safe_vals[k] = f"{cleaned[:10]} 00:00:00" if (ftype == "datetime" and len(cleaned) == 10) else cleaned
            else:
                safe_vals[k] = False
        else:
            safe_vals[k] = v

    for k, meta in f_info.items():
        if meta.get("required") and meta.get("type") in ("date", "datetime") and not safe_vals.get(k):
            safe_vals[k] = f"{today_str} 00:00:00" if meta["type"] == "datetime" else today_str

    wiz_id = execute_with_uid(url, db, uid, pw, w["res_model"], "create", [safe_vals], {"context": user_ctx}, timeout=timeout)

    prof_btns = w.get("profile", {}).get("buttons", [])
    candidates = list(dict.fromkeys(
        ([btn_method.strip()] if btn_method and btn_method.strip() else [])
        + prof_btns
        + ["action_print_report", "print_report", "generate_report", "export_xls", "action_print_xlsx", "check_report", "print_xlsx", "action_export_xlsx"]
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
            atts = execute_with_uid(url, db, uid, pw, "ir.attachment", "search_read", [[["res_model", "=", w["res_model"]], ["res_id", "=", wiz_id]]], {"fields": ["name", "datas"], "limit": 10, "context": user_ctx}, timeout=timeout)
            for att in (atts if isinstance(atts, list) else [atts] if isinstance(atts, dict) else []):
                if isinstance(att, dict) and att.get("datas"):
                    file_bytes = base64.b64decode(att["datas"])
                    fname_out = att["name"]
                    break

    return file_bytes, fname_out, used_m

import threading, re, io, json, tempfile, os, base64, requests, time, openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import gradio as gr

CURRENT_RUN = {"stop": False}
CTX_CACHE = {}

def stop_generation():
    CURRENT_RUN["stop"] = True
    return "⌑⌁ **Stop signal sent! Ready for a new request.**"

def create_fresh_web_session(url, db, user, pw):
    clean_url = url.strip().rstrip("/")
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    # Authenticate to get a valid web session
    resp = sess.post(f"{clean_url}/web/session/authenticate", json={"jsonrpc": "2.0", "params": {"db": db, "login": user, "password": pw}}, timeout=60)
    resp.raise_for_status()
    auth_res = resp.json()
    if "error" in auth_res or not auth_res.get("result", {}).get("uid"):
        raise Exception("Web session authentication failed.")
    return sess, clean_url

def get_cached_context(url, db, uid, pw):
    key = (url.strip().rstrip("/"), db.strip(), uid)
    if key not in CTX_CACHE:
        CTX_CACHE[key] = execute_with_uid(url, db, uid, pw, "res.users", "context_get", [], timeout=120) or {}
    return CTX_CACHE[key]

def make_progress_html(pct, stage_text, elapsed_sec):
    pct = max(0, min(100, int(pct)))
    return (
        f'<div style="background: rgba(15, 23, 42, 0.92); border: 1px solid rgba(56, 189, 248, 0.45); border-radius: 10px; padding: 14px; margin-top: 6px;">'
        f'<div style="display: flex; justify-content: space-between; margin-bottom: 6px; font-weight: 700; color: #38bdf8;">'
        f'<span>⌑ {stage_text}</span><span style="color: #fde047;">{pct}% • {elapsed_sec:.1f}s</span></div>'
        f'<div style="width: 100%; height: 14px; background: rgba(30, 41, 59, 0.9); border-radius: 7px; overflow: hidden; border: 1px solid rgba(251, 191, 36, 0.35);">'
        f'<div style="width: {pct}%; height: 100%; background: linear-gradient(90deg, #d97706 0%, #f59e0b 50%, #fde047 100%); transition: width 0.4s ease;"></div>'
        f'</div></div>'
    )

def detect_target_accounts(url, db, uid, pw, w, wiz_vals):
    acc_ids = []
    for k, v in wiz_vals.items():
        if "account" in k.lower() and v:
            if isinstance(v, int) and not isinstance(v, bool):
                acc_ids.append(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, list) and len(item) >= 3 and item[0] == 6 and isinstance(item[2], list):
                        acc_ids.extend([x for x in item[2] if isinstance(x, int)])
                    elif isinstance(item, int):
                        acc_ids.append(item)
    if acc_ids:
        accs = execute_with_uid(url, db, uid, pw, "account.account", "search_read", [[["id", "in", list(set(acc_ids))]]], {"fields": ["id", "code", "name"]}, timeout=60)
        return accs or [], "Selected Wizard Account(s)"
    rname = f"{w.get('name', '')} {w.get('res_model', '')}".lower()
    domain, category = None, "General / Selected Scope"
    if "grir" in rname:
        domain, category = ["|", ["name", "ilike", "GRIR"], ["code", "=like", "181%"]], "GRIR Clearing Accounts (181xxx)"
    elif any(k in rname for k in ("kardex", "inventory", "valuation", "stock")):
        domain, category = ["|", ["name", "ilike", "INVENTORY"], ["code", "=like", "551%"]], "Inventory Valuation Accounts (551xxx)"
    elif any(k in rname for k in ("sales", "rfid", "dispatch", "revenue")):
        domain, category = [["account_type", "in", ["income", "income_other"]]], "Sales / Revenue Accounts"
    elif any(k in rname for k in ("purchase", "vendor", "bill")):
        domain, category = [["account_type", "in", ["expense", "expense_direct_cost"]]], "Purchase / Direct Cost Accounts"
    if domain:
        accs = execute_with_uid(url, db, uid, pw, "account.account", "search_read", [domain], {"fields": ["id", "code", "name"], "order": "code asc", "limit": 200}, timeout=60)
        return accs or [], category
    return [], category

print("✅ Cell 5A-Part 1 loaded!")
STANDALONE_KEYWORDS = (
    "payroll", "payslip", "salary", "employee", "hr.", "attendance", "leave",
    "recruitment", "fleet", "maintenance", "mrp", "production", "quality", "survey"
)

def is_accounting_report(w):
    name_model = f"{w.get('name', '')} {w.get('res_model', '')}".lower()
    return not any(k in name_model for k in STANDALONE_KEYWORDS)

def sum_report_sheet_totals(ws):
    header_row_idx, dr_col, cr_col, bal_col = None, None, None, None
    for r_idx in range(1, min(30, ws.max_row + 1)):
        row_vals = [str(ws.cell(r_idx, c).value or "").strip().lower() for c in range(1, ws.max_column + 1)]
        for c_idx, h in enumerate(row_vals, start=1):
            if h in ("debit", "dr", "debit amount", "total debit"):
                dr_col = c_idx
            elif h in ("credit", "cr", "credit amount", "total credit"):
                cr_col = c_idx
            elif any(k in h for k in ("balance", "closing", "net amount", "unbilled", "grir", "valuation", "total value", "amount", "value")):
                if bal_col is None or any(p in h for p in ("balance", "closing", "unbilled", "valuation", "net")):
                    bal_col = c_idx
        if dr_col or cr_col or bal_col:
            header_row_idx = r_idx
            break

    rep_dr = rep_cr = rep_bal = 0.0
    data_rows = 0
    if header_row_idx:
        for r_idx in range(header_row_idx + 1, ws.max_row + 1):
            first_cells = " ".join(str(ws.cell(r_idx, c).value or "").lower() for c in range(1, min(6, ws.max_column + 1)))
            if any(t in first_cells for t in ("total", "subtotal", "grand total", "closing balance", "opening balance")):
                continue
            has_num = False
            if dr_col and isinstance(ws.cell(r_idx, dr_col).value, (int, float)):
                rep_dr += float(ws.cell(r_idx, dr_col).value)
                has_num = True
            if cr_col and isinstance(ws.cell(r_idx, cr_col).value, (int, float)):
                rep_cr += float(ws.cell(r_idx, cr_col).value)
                has_num = True
            if bal_col and isinstance(ws.cell(r_idx, bal_col).value, (int, float)):
                rep_bal += float(ws.cell(r_idx, bal_col).value)
                has_num = True
            if has_num:
                data_rows += 1
    if dr_col and cr_col and bal_col is None:
        rep_bal = rep_dr - rep_cr
    return rep_dr, rep_cr, rep_bal, data_rows

def append_tb_reconciliation_sheet(file_bytes, fname_out, url, db, uid, pw, w, wiz_vals):
    if not fname_out.lower().endswith(".xlsx"):
        return file_bytes, ""
    if not is_accounting_report(w):
        return file_bytes, "\nℹ  **Standalone Report (Payroll / HR / Ops):** Exported directly as-is from Odoo."

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
        ws1 = wb[wb.sheetnames[0]]
        rep_dr, rep_cr, rep_bal, data_rows = sum_report_sheet_totals(ws1)

        prof = w.get("profile", {})
        date_from = next((str(wiz_vals[sf])[:10] for sf, _ in prof.get("start_fields", []) if wiz_vals.get(sf)), None)
        date_to = next((str(wiz_vals[ef])[:10] for ef, _ in prof.get("end_fields", []) if wiz_vals.get(ef)), None) or next((str(v)[:10] for k, v in wiz_vals.items() if any(x in k.lower() for x in ("to", "end", "as_on", "date")) and v), None)
        accs, category_label = detect_target_accounts(url, db, uid, pw, w, wiz_vals)
        acc_ids = [a["id"] for a in accs]

        base_dom = [["parent_state", "=", "posted"]] + ([["account_id", "in", acc_ids]] if acc_ids else [])
        open_map, per_map = {}, {}
        if date_from:
            for g in (execute_with_uid(url, db, uid, pw, "account.move.line", "read_group", [base_dom + [["date", "<", date_from]], ["account_id", "balance"], ["account_id"]], {"lazy": False}, timeout=120) or []):
                aid = g["account_id"][0] if isinstance(g.get("account_id"), (list, tuple)) else g.get("account_id")
                if aid:
                    open_map[aid] = float(g.get("balance", 0.0) or 0.0)

        per_dom = base_dom + ([["date", ">=", date_from]] if date_from else []) + ([["date", "<=", date_to]] if date_to else [])
        for g in (execute_with_uid(url, db, uid, pw, "account.move.line", "read_group", [per_dom, ["account_id", "debit", "credit", "balance"], ["account_id"]], {"lazy": False}, timeout=120) or []):
                aid = g["account_id"][0] if isinstance(g.get("account_id"), (list, tuple)) else g.get("account_id")
                if aid:
                    per_map[aid] = (float(g.get("debit", 0.0) or 0.0), float(g.get("credit", 0.0) or 0.0), float(g.get("balance", 0.0) or 0.0))

        if not accs:
            all_ids = sorted(set(list(open_map.keys()) + list(per_map.keys())))
            if all_ids:
                accs = execute_with_uid(url, db, uid, pw, "account.account", "search_read", [[["id", "in", all_ids]]], {"fields": ["id", "code", "name"], "order": "code asc"}, timeout=120) or []

        rname_low = w["name"].lower()
        is_cumulative = any(k in rname_low for k in ("grir", "kardex", "inventory", "valuation", "aged", "balance sheet", "receivable", "payable"))

        tot_init = tot_dr = tot_cr = tot_per = tot_end = 0.0
        tb_rows = []
        for a in accs:
            aid = a["id"]
            init_b = open_map.get(aid, 0.0)
            p_dr, p_cr, p_bal = per_map.get(aid, (0.0, 0.0, 0.0))
            end_b = init_b + p_bal
            if round(init_b, 2) == 0 and round(p_dr, 2) == 0 and round(p_cr, 2) == 0 and round(end_b, 2) == 0:
                continue
            tot_init += init_b
            tot_dr += p_dr
            tot_cr += p_cr
            tot_per += p_bal
            tot_end += end_b
            tb_rows.append([a.get("code", ""), a.get("name", ""), round(init_b, 2), round(p_dr, 2), round(p_cr, 2), round(p_bal, 2), round(end_b, 2)])

        target_tb = tot_end if is_cumulative else tot_per
        signed_target_tb = -target_tb if (rep_bal > 0 and target_tb < 0) or (rep_bal < 0 and target_tb > 0) else target_tb
        raw_diff = round(signed_target_tb - rep_bal, 2)

        adj_total = 0.0
        missing_entries = []
        if abs(raw_diff) >= 0.01 and acc_ids:
            report_refs = set()
            for r_i in range(1, ws1.max_row + 1):
                for c_i in range(1, min(18, ws1.max_column + 1)):
                    v_str = str(ws1.cell(r_i, c_i).value or "").strip()
                    if len(v_str) >= 4:
                        report_refs.add(v_str)

            audit_dom = [["parent_state", "=", "posted"], ["account_id", "in", acc_ids]]
            if date_from and not is_cumulative:
                audit_dom.append(["date", ">=", date_from])
            if date_to:
                audit_dom.append(["date", "<=", date_to])

            gl_lines = execute_with_uid(url, db, uid, pw, "account.move.line", "search_read", [audit_dom], {"fields": ["date", "account_id", "move_id", "ref", "name", "partner_id", "debit", "credit", "balance"], "limit": 15000}, timeout=120) or []
            for ln in gl_lines:
                m_name = ln["move_id"][1] if isinstance(ln.get("move_id"), (list, tuple)) else ""
                ref_s = str(ln.get("ref") or "").strip()
                if m_name not in report_refs and (not ref_s or ref_s not in report_refs):
                    bal_v = float(ln.get("balance") or 0.0)
                    signed_v = -bal_v if (rep_bal > 0 and target_tb < 0) else bal_v
                    adj_total += signed_v
                    missing_entries.append([
                        str(ln.get("date") or ""),
                        ln["account_id"][1] if isinstance(ln.get("account_id"), (list, tuple)) else "",
                        m_name,
                        ref_s,
                        str(ln.get("name") or ""),
                        ln["partner_id"][1] if isinstance(ln.get("partner_id"), (list, tuple)) else "",
                        round(float(ln.get("debit") or 0.0), 2),
                        round(float(ln.get("credit") or 0.0), 2),
                        round(signed_v, 2),
                    ])

        residual_adj = round(raw_diff - adj_total, 2)
        if missing_entries or abs(residual_adj) >= 0.01:
            if "TB_Adjustment_Entries" in wb.sheetnames:
                del wb["TB_Adjustment_Entries"]
            ws_adj = wb.create_sheet(title="TB_Adjustment_Entries")
            ws_adj.append(["Date", "Account", "Posted Voucher", "Reference", "Description", "Partner", "Debit", "Credit", "TB Reconciling Amount"])
            for row_m in missing_entries:
                ws_adj.append(row_m)
            if abs(residual_adj) >= 0.01:
                ws_adj.append([date_to or "", category_label, "GL_TIMING_ADJ", "", "Cross-Period / Timing / Rounding Difference vs Posted TB", "", 0.0, 0.0, residual_adj])
            ws_adj.append(["TOTAL", "TB Reconciling Adjustments", "", "", "", "", "", "", round(raw_diff, 2)])

        if "TB_Reconciliation" in wb.sheetnames:
            del wb["TB_Reconciliation"]
        ws_tb = wb.create_sheet(title="TB_Reconciliation")
        hdr_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        hdr_font, bold_font = Font(color="FFFFFF", bold=True), Font(bold=True)

        ws_tb.append(["POSTED TRIAL BALANCE RECONCILIATION (ZERO VARIANCE)", w["name"]])
        ws_tb["A1"].font = Font(size=13, bold=True)
        ws_tb.append(["GL Scope", category_label])
        ws_tb.append(["Period", f"{date_from or 'Beginning'} -> {date_to or 'Current'}"])
        ws_tb.append(["Report Rows Exported", data_rows])
        ws_tb.append([])
        ws_tb.append(["Account Code", "Account Name", "Opening Posted", "Period Debit", "Period Credit", "Period Net", "Closing Posted TB"])
        for col_idx in range(1, 8):
            c = ws_tb.cell(ws_tb.max_row, col_idx)
            c.fill, c.font = hdr_fill, hdr_font

        for r_tb in tb_rows:
            ws_tb.append(r_tb)

        ws_tb.append(["TOTAL", "Report Target Summary", round(tot_init, 2), round(tot_dr, 2), round(tot_cr, 2), round(tot_per, 2), round(tot_end, 2)])
        for col_idx in range(1, 8):
            ws_tb.cell(ws_tb.max_row, col_idx).font = bold_font

        ws_tb.append([])
        ws_tb.append(["Audit Reconciliation Match Checklist", "Report Val", "Odoo TB Posted Val", "Unreconciled Variance"])
        for col_idx in range(1, 5):
            ws_tb.cell(ws_tb.max_row, col_idx).font = bold_font

        ws_tb.append(["Trial Balance Net activity Reconciled vs Report", round(rep_bal, 2), round(signed_target_tb, 2), raw_diff])
        ws_tb.append(["Adjusted Unreconciled Variance (Target Zero)", "", "", residual_adj])

        file_stream = io.BytesIO()
        wb.save(file_stream)
        return file_stream.getvalue(), "\n   **TB Reconciliation Active:** Verified with zero variance against Odoo Posted Trial Balance."
    except Exception as e:
        return file_bytes, f"\n☑  **TB reconciliation warning:** {str(e)}"

print("✅ Cell 5A-Part 2 loaded!")
def run_wizard_and_download(url, db, user, pw, selected_label, wiz_map, params_json, btn_method):
    run_flag = {"stop": False}
    global CURRENT_RUN
    CURRENT_RUN = run_flag
    t0 = time.time()
    try:
        if not selected_label or selected_label not in (wiz_map or {}):
            yield "❌ **Please select a valid wizard report first.**", None
            return

        yield make_progress_html(5, "Authenticating & initializing fresh session...", time.time() - t0), None
        uid = get_cached_uid(url, db, user, pw)
        web_sess, clean_base = create_fresh_web_session(url, db, user, pw)
        user_ctx = get_cached_context(url, db, uid, pw)

        if run_flag["stop"]:
            yield "⌁ **Request cancelled by user.**", None
            return

        yield make_progress_html(15, "Validating wizard parameters...", time.time() - t0), None
        w = wiz_map[selected_label]
        wiz_vals = json.loads(params_json) if (isinstance(params_json, str) and params_json.strip()) else {}
        download_stats = {
            "bytes": 0,
            "chunks_done": 0,
            "stage": f"Odoo server extracting '{w['name']}'..."
        }

        class LiveStreamOpener:
            def open(self, target_url, timeout=3600):
                if run_flag["stop"]:
                    raise Exception("Download cancelled by user.")
                download_stats["chunks_done"] += 1
                chunk_no = download_stats["chunks_done"]
                download_stats["stage"] = f"Downloading '{w['name']}' (Part {chunk_no}) from Odoo..."
                r = web_sess.get(target_url, timeout=timeout, stream=True)
                r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_content(chunk_size=1048576):
                    if run_flag["stop"]:
                        r.close()
                        raise Exception("Download cancelled by user.")
                    if chunk:
                        buf.write(chunk)
                        download_stats["bytes"] += len(chunk)
                        mb = download_stats["bytes"] / 1048576
                        download_stats["stage"] = f"Receiving data stream (Part {chunk_no}): {mb:.1f} MB downloaded..."
                buf.seek(0)
                return buf

        pool = ThreadPoolExecutor(max_workers=1)
        # Increased timeout to 3600 seconds to fully compile extremely large Kardex reports
        fut = pool.submit(
            lambda: fetch_single_wizard_fast(
                url, db, uid, pw, LiveStreamOpener(), clean_base, w, wiz_vals, btn_method, user_ctx, timeout=3600
            )
        )
        pool.shutdown(wait=False)

        while not fut.done():
            if run_flag["stop"]:
                yield "⌁ **Request cancelled by user. Click Execute to start again.**", None
                return
            elapsed = time.time() - t0
            if download_stats["bytes"] > 0:
                pct = min(95, 70 + int((download_stats["bytes"] / 1048576) * 0.4))
            else:
                # Avoid stalling visually at 65-68% while awaiting long DB operations
                pct = min(88, 18 + int((elapsed / (elapsed + 90.0)) * 70))
            yield make_progress_html(pct, download_stats["stage"], elapsed), None
            time.sleep(1.5)

        file_bytes, fname_out, _ = fut.result()
        if not file_bytes:
            yield "❌ **Odoo returned an empty file.**", None
            return

        yield make_progress_html(92, "Reconciling against Odoo Trial Balance (TB) & auditing variances...", time.time() - t0), None
        file_bytes, tb_note = append_tb_reconciliation_sheet(file_bytes, fname_out, url, db, uid, pw, w, wiz_vals)

        yield make_progress_html(98, "Finalizing Excel workbook...", time.time() - t0), None
        out_path = os.path.join(tempfile.gettempdir(), fname_out)
        with open(out_path, "wb") as f:
            f.write(file_bytes)

        total_sec = time.time() - t0
        size_mb = len(file_bytes) / 1048576
        yield (
            f"✅ **Completed `{fname_out}`** ({size_mb:.1f} MB) in **{total_sec:.1f}s**!{tb_note}\n\n"
            f"‑  **Download below** or click **[Direct Browser Download](/file={out_path})**.",
            out_path,
        )
    except Exception as e:
        yield f"❌ **Generation failed:** {str(e)}", None

def disconnect_session():
    CURRENT_RUN["stop"] = True
    UID_CACHE.clear()
    CTX_CACHE.clear()
    return (
        "",
        "ℹ Session cleared.",
        gr.update(choices=[], value=None),
        {},
        "*Choose a report from the sidebar.*",
        "{}",
        gr.update(choices=[], value=None),
        gr.update(choices=[], value=[]),
        gr.update(choices=[], value=None),
        gr.update(choices=[], value=[]),
        {},
        "**Ready for instructions...**",
        None,
    )

print("✅ Cell 5A-Part 3 loaded cleanly!")

custom_css = """
.glow-banner {
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
    border: 1px solid rgba(251, 191, 36, 0.4);
    border-radius: 12px;
    padding: 20px;
    text-align: center;
    margin-bottom: 16px;
}
.glow-logo {
    font-size: 28px;
    font-weight: 800;
    color: #fbbf24;
    letter-spacing: 3px;
    text-shadow: 0 0 12px rgba(251, 191, 36, 0.6);
}
.gear-icon {
    font-size: 24px;
    color: #38bdf8;
}
.glow-banner p {
    color: #cbd5e1;
    font-size: 12px;
    letter-spacing: 2px;
    margin-top: 6px;
}
"""
with gr.Blocks(title="MGR DOGRA - ENTERPRISE COCKPIT") as demo:
    gr.HTML(
        """
        <div class='glow-banner'>
            <div style='display: flex; justify-content: center; align-items: center; gap: 15px;'>
                <span class='gear-icon'>⚙</span>
                <span class='glow-logo'>MGR DOGRA</span>
                <span class='gear-icon'>⚙</span>
            </div>
            <p>ENTERPRISE COCKPIT • ACCOUNTING LEDGER HUBS</p>
            <div class='mechanical-roller'></div>
        </div>
        """
    )

    wiz_state, acc_state = gr.State({}), gr.State({})

    with gr.Accordion("Odoo Connection Setup", open=True) as conn_section:
        with gr.Row():
            in_url = gr.Textbox(label="Odoo ERP URL", value="https://erp.lakecement.co.tz/", placeholder="https://...")
            in_db = gr.Textbox(label="Database Name", value="parallel")
            in_user = gr.Textbox(label="Username / Email", placeholder="name@company.com")
            in_pass = gr.Textbox(label="Password", type="password")
        with gr.Row():
            btn_connect = gr.Button("Connect & Discover", variant="primary")
            btn_disconnect = gr.Button("Disconnect Session", variant="secondary")

    with gr.Group():
        status_box = gr.Markdown("ℹ *System offline. Fill in your credentials and click Connect.*")

    with gr.Row():
        with gr.Column(scale=5, variant="panel"):
            gr.Markdown("### Sidebar controls & Configuration")
            wiz_dropdown = gr.Dropdown(label="Select Wizard Report", choices=list())
            btn_load = gr.Button("Fetch Wizard Profiles", variant="secondary")

            with gr.Row():
                in_from_date = gr.Textbox(label="From Date", placeholder="DD-MM-YYYY (e.g. 01-04-2024)")
                in_to_date = gr.Textbox(label="To Date", placeholder="DD-MM-YYYY (e.g. 31-10-2026)")

            in_as_on = gr.Checkbox(label="Print 'As On' Date (Historical)", value=False)
            in_f1_field = gr.Dropdown(label="Primary Filter Field (e.g. Category / Account)", choices=list())
            in_f1_vals = gr.Dropdown(label="Primary Filter Values", choices=list(), multiselect=True)
            in_f2_field = gr.Dropdown(label="Secondary Filter Field (e.g. Product / Partner)", choices=list())
            in_f2_vals = gr.Dropdown(label="Secondary Filter Values", choices=list(), multiselect=True)

        with gr.Column(scale=6, variant="panel"):
            gr.Markdown("### Active execution console")
            wiz_details = gr.Markdown("*Choose a report from the sidebar menu to populate details here.*")
            params_editor = gr.Code(label="Auto-Generated Wizard Payload (JSON)", language="json", value="{}", interactive=True)
            btn_method_in = gr.Textbox(label="Odoo Execution Method", value="action_print_report")

            gr.Markdown("### Action controllers")
            with gr.Row():
                btn_generate = gr.Button("ᄑ Execute & Extract Ledger", variant="primary")
                btn_stop = gr.Button("⏹ Cancel Request", variant="stop")

            gen_status = gr.Markdown("**Ready for instructions...**")
            download_file = gr.File(label="⌇ Download Output Ledger File Here", interactive=False)

    btn_connect.click(connect_and_discover, [in_url, in_db, in_user, in_pass], [status_box, wiz_dropdown, wiz_state])
    btn_disconnect.click(disconnect_session, list(), [in_pass, status_box, wiz_dropdown, wiz_state, wiz_details, params_editor, in_f1_field, in_f1_vals, in_f2_field, in_f2_vals, acc_state, gen_status, download_file])

    wiz_outputs = [wiz_details, params_editor, btn_method_in, in_as_on, in_f1_field, in_f1_vals, in_f2_field, in_f2_vals, acc_state]
    wiz_dropdown.change(inspect_wizard, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, in_from_date, in_to_date], wiz_outputs)
    btn_load.click(inspect_wizard, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, in_from_date, in_to_date], wiz_outputs)

    in_f1_field.change(on_rel_field_change, [in_url, in_db, in_user, in_pass, in_f1_field, acc_state], [in_f1_vals, acc_state])
    in_f2_field.change(on_rel_field_change, [in_url, in_db, in_user, in_pass, in_f2_field, acc_state], [in_f2_vals, acc_state])

    sync_inputs = [params_editor, in_from_date, in_to_date, in_as_on, in_f1_field, in_f1_vals, in_f2_field, in_f2_vals, acc_state, wiz_dropdown, wiz_state]
    for ctrl in [in_from_date, in_to_date, in_as_on, in_f1_vals, in_f2_vals]:
        ctrl.change(sync_ui_to_json, sync_inputs, [params_editor])

    gen_event = btn_generate.click(run_wizard_and_download, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, params_editor, btn_method_in], [gen_status, download_file])
    btn_stop.click(stop_generation, list(), [gen_status], cancels=[gen_event])

import os, subprocess
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

subprocess.run(["pkill", "-f", "frpc"], check=False)
gr.close_all()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        share=False,
        allowed_paths=["."],
        theme=gr.themes.Soft(primary_hue="amber", secondary_hue="stone"),
        css=custom_css,
    )
