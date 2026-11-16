import os

import gradio as gr
import requests, urllib.request, urllib.error, http.cookiejar, json, base64, tempfile, os
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urlparse

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
    resp = RPC_SESSION.post(target_url, json=payload, timeout=timeout, allow_redirects=True)
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

print("✅ Part 1 loaded with connection pooling!")

from datetime import datetime, timedelta
import urllib.parse

UID_CACHE = {}

def get_cached_uid(url, db, user, pw):
    key = (url.strip().rstrip("/"), db.strip(), user.strip(), pw)
    if key not in UID_CACHE:
        uid = odoo_jsonrpc(url, "common", "authenticate", [db, user, pw, {}], timeout=45)
        if not uid:
            raise Exception("Authentication failed.")
        UID_CACHE[key] = uid
    return UID_CACHE[key]

def execute_with_uid(url, db, uid, pw, model, method, args, kwargs=None, timeout=900):
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
        pairs = execute_with_uid(url, db, uid, pw, rel_model, "name_search", [], {"name": search_arg, "limit": 150}, timeout=20)
        if not pairs and search_arg:
            pairs = execute_with_uid(url, db, uid, pw, rel_model, "name_search", [], {"name": "", "limit": 150}, timeout=20)
        opt_map = {f"{p} (ID:{p})": p for p in (pairs or []) if isinstance(p, (list, tuple)) and len(p) >= 2}
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

    model = w["res_model"]
    f_info = execute_with_uid(url, db, uid, pw, model, "fields_get", [], {"attributes": ["string", "type", "relation", "selection", "required"]}, timeout=45)
    ignore = {"id", "create_uid", "create_date", "write_uid", "write_date", "__last_update", "display_name"}
    v_raw = w.get("view_id")
    v_id = next(iter(v_raw), False) if isinstance(v_raw, list) else False
    arch = ""
    try:
        arch = execute_with_uid(url, db, uid, pw, model, "get_views", [], {"views": [[v_id, "form"]]}, timeout=45).get("views", {}).get("form", {}).get("arch", "")
    except Exception:
        try:
            arch = execute_with_uid(url, db, uid, pw, model, "fields_view_get", [], {"view_id": v_id, "view_type": "form"}, timeout=45).get("arch", "")
        except Exception:
            pass

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
        default_vals = execute_with_uid(url, db, uid, pw, model, "default_get", [valid_fields], timeout=45) or {}
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
            opts = ", ".join(f"`{x}` ({x})" for x in meta["selection"] if isinstance(x, (list, tuple)) and len(x) >= 2)
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
            valid = [item for item in meta["selection"] if isinstance(item, (list, tuple)) and len(item) >= 2]
            if valid:
                default_vals[fname] = valid

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

    return {
        "valid_fields": valid_fields,
        "field_types": field_types,
        "start_fields": start_fields,
        "end_fields": end_fields,
        "defaults": default_vals,
        "buttons": btns,
        "summary": field_summary,
        "rel_fields": rel_fields,
    }

print("✅ Cell 2A-1 loaded cleanly!")

def connect_and_discover(url, db, user, pw, progress=gr.Progress()):
    try:
        progress(0.2, desc="Authenticating & discovering wizards...")
        uid = get_cached_uid(url, db, user, pw)
        progress(0.6, desc="Loading all report wizards & financial statements...")
        wizards = execute_with_uid(
            url, db, uid, pw, "ir.actions.act_window", "search_read",
            [[["target", "=", "new"], ["res_model", "!=", False]]],
            {"fields": ["id", "name", "res_model", "view_id"], "limit": 1000},
            timeout=120
        )

        client_reports = []
        try:
            acct_reps = execute_with_uid(url, db, uid, pw, "account.report", "search_read", [[]], {"fields": ["id", "name"], "limit": 200}, timeout=60)
            for r in acct_reps:
                client_reports.append({"id": r["id"], "name": r["name"], "action_type": "client", "res_model": "account.report", "context": {"report_id": r["id"]}, "params": {"report_id": r["id"]}})
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
            wiz_map[f"{w['name']} (Action ID: {w['id']} | {w['res_model']})"] = w
        for c in client_reports:
            wiz_map[f"{c['name']} (Financial Statement)"] = c

        choices = sorted(list(wiz_map.keys()))
        progress(1.0, desc="All wizards & reports discovered!")
        return f"⚡ Connected! Discovered **{len(wizards)} wizards** & **{len(client_reports)} financial statements**.", gr.update(choices=choices, value=next(iter(choices), None)), wiz_map
    except Exception as e:
        return f"❌ Error: {str(e)}", gr.update(choices=[]), {}

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

        f1_opts = sorted(list(fetch_rel_options(url, db, uid, pw, rel_fields[f1_key], w["name"]).keys())) if f1_key else []
        f2_opts = sorted(list(fetch_rel_options(url, db, uid, pw, rel_fields[f2_key], w["name"]).keys())) if f2_key else []

        info_md = (
            f"### Wizard: **{w['name']}** (`{w['res_model']}`)\n"
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
                        data[fname] = [[6, 0, ids]] if ftype == "many2many" else ids
                elif fname in data:
                    data[fname] = [[6, 0, []]] if ftype == "many2many" else False

        return json.dumps(data, indent=2)
    except Exception:
        return params_json if isinstance(params_json, str) else "{}"

print("✅ Cell 2A-2 loaded!")

import pandas as pd
import io, time, gc, xlsxwriter, urllib.parse
from datetime import datetime, timedelta

def execute_with_uid(url, db, uid, pw, model, method, args, kwargs=None, timeout=900):
    call_args = [db, uid, pw, model, method, args]
    if kwargs is not None:
        call_args.append(kwargs)
    return odoo_jsonrpc(url, "object", "execute_kw", call_args, timeout=timeout)

def build_direct_financial_xlsx(url, db, uid, pw, fs_type, d_from, d_to, user_ctx, timeout=900):
    acc_fields = execute_with_uid(url, db, uid, pw, "account.account", "fields_get", [], {"attributes": ["type"]}, timeout=60)
    type_field = "account_type" if "account_type" in acc_fields else ("user_type_id" if "user_type_id" in acc_fields else None)
    fields_to_read = ["id", "code", "name"] + ([type_field] if type_field else [])
    accounts = execute_with_uid(url, db, uid, pw, "account.account", "search_read", [[]], {"fields": fields_to_read, "limit": 5000}, timeout=120)
    acc_map = {a["id"]: a for a in accounts}

    domain = [["parent_state", "=", "posted"]]
    if fs_type == "pnl":
        if d_from:
            domain.append(["date", ">=", d_from])
        if d_to:
            domain.append(["date", "<=", d_to])
    else:
        if d_to:
            domain.append(["date", "<=", d_to])

    grouped = execute_with_uid(
        url, db, uid, pw, "account.move.line", "read_group",
        [domain, ["account_id", "debit", "credit", "balance"], ["account_id"]],
        {"lazy": False}, timeout=timeout
    )

    pnl_keys = ("income", "expense", "cost_of_revenue", "depreciation")
    bs_keys = ("asset", "liability", "equity", "receivable", "payable", "liquidity", "credit_card")

    rows = []
    net_earnings = 0.0

    for g in grouped:
        acc_ref = g.get("account_id")
        if not acc_ref:
            continue
        acc_id = acc_ref[0] if isinstance(acc_ref, (list, tuple)) else acc_ref
        acc = acc_map.get(acc_id, {})
        raw_type = acc.get(type_field, "")
        type_str = raw_type[1] if isinstance(raw_type, (list, tuple)) and len(raw_type) >= 2 else str(raw_type or "")
        low_t = type_str.lower()

        is_pnl_acc = any(k in low_t for k in pnl_keys)
        is_bs_acc = any(k in low_t for k in bs_keys)

        debit = float(g.get("debit", 0.0) or 0.0)
        credit = float(g.get("credit", 0.0) or 0.0)
        balance = float(g.get("balance", debit - credit) or 0.0)

        if is_pnl_acc:
            net_earnings += -balance

        if fs_type == "pnl" and not is_pnl_acc:
            continue
        if fs_type == "bs" and not is_bs_acc:
            continue

        display_amt = -balance if any(k in low_t for k in ("income", "liability", "payable", "equity")) else balance

        rows.append({
            "Account Code": acc.get("code", ""),
            "Account Name": acc.get("name", str(acc_ref)),
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
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name=title)
    return buf.getvalue(), f"{title}_{d_to or 'AsOn'}.xlsx", "read_group_ledger"

def fetch_single_wizard_fast(url, db, uid, pw, opener, clean_base, w, wiz_vals, btn_method, user_ctx, timeout=900):
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

print("✅ Cell 3 loaded successfully!")

import threading, re, io, json, tempfile, os, base64, openpyxl, xlsxwriter
from datetime import datetime
import gradio as gr

STOP_EVENT = threading.Event()

def stop_generation():
    STOP_EVENT.set()
    return "🛑 Stop signal sent!"

def optimize_large_xlsx(file_bytes, fname_out, threshold_mb=15.0):
    """Strips heavy Odoo XML styles only from massive >15MB .xlsx files."""
    if not fname_out.lower().endswith(".xlsx") or len(file_bytes) < threshold_mb * 1024 * 1024:
        return file_bytes, fname_out
    try:
        wb_in = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        wb_out = openpyxl.Workbook(write_only=True)
        for sheet_name in wb_in.sheetnames:
            ws_in = wb_in[sheet_name]
            ws_out = wb_out.create_sheet(title=sheet_name)
            for row in ws_in.iter_rows(values_only=True):
                ws_out.append(list(row))
        wb_in.close()
        out_buf = io.BytesIO()
        wb_out.save(out_buf)
        slim_bytes = out_buf.getvalue()
        if len(slim_bytes) < len(file_bytes):
            return slim_bytes, fname_out
    except Exception:
        pass
    return file_bytes, fname_out

def run_wizard_and_download(url, db, user, pw, selected_label, wiz_map, params_json, btn_method, progress=gr.Progress()):
    try:
        if not selected_label or selected_label not in wiz_map:
            raise Exception("Please select a valid wizard report first.")
        progress(0.10, desc="Authenticating with Odoo...")
        uid = get_cached_uid(url, db, user, pw)
        opener, clean_base = get_auth_opener(url, db, user, pw)
        progress(0.30, desc="Loading wizard context & parameters...")
        w = wiz_map[selected_label]
        wiz_vals = json.loads(params_json) if params_json.strip() else {}
        user_ctx = execute_with_uid(url, db, uid, pw, "res.users", "context_get", [], timeout=45) or {}
        progress(0.55, desc=f"Executing '{w['name']}' on Odoo server (please wait)...")
        file_bytes, fname_out, used_method = fetch_single_wizard_fast(url, db, uid, pw, opener, clean_base, w, wiz_vals, btn_method, user_ctx, timeout=900)
        if not file_bytes:
            raise Exception("Odoo returned an empty file. Check the wizard parameters or required fields.")

        raw_mb = len(file_bytes) / (1024 * 1024)
        progress(0.85, desc=f"Preparing {raw_mb:.1f} MB report file...")
        file_bytes, fname_out = optimize_large_xlsx(file_bytes, fname_out, threshold_mb=15.0)
        final_mb = len(file_bytes) / (1024 * 1024)

        out_path = os.path.join(tempfile.gettempdir(), fname_out)
        with open(out_path, "wb") as f:
            f.write(file_bytes)
        progress(1.0, desc="Done!")
        size_note = f"{raw_mb:.1f} MB → {final_mb:.1f} MB" if raw_mb > final_mb + 0.5 else f"{final_mb:.1f} MB"
        return f"✅ Successfully generated **{fname_out}** ({size_note}, all rows & columns preserved)!", out_path
    except Exception as e:
        return f"❌ Generation failed: {str(e)}", None

def disconnect_session():
    UID_CACHE.clear()
    return (
        "",
        "ℹ Session cleared successfully.",
        gr.update(choices=[], value=None),
        {},
        "*Choose a report from the sidebar menu to populate details here.*",
        "{}",
        gr.update(choices=[], value=None),
        gr.update(choices=[], value=[]),
        gr.update(choices=[], value=None),
        gr.update(choices=[], value=[]),
        {},
        "**Ready for instructions...**",
        None,
    )

# Paste your existing custom_css string right below here

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
