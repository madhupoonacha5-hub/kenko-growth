"""Import Assistant tools for the Go-Live Assurance MCP server.
Add to the server, above `if __name__ == "__main__":`
    from import_assistant_tools import register
    register(mcp)
The canonical columns and rules are ILLUSTRATIVE: replace with Kenko's real import spec."""
import re

COLS = ("id", "first", "last", "email", "phone", "status", "plan", "plan_end", "pack_balance", "card", "future_bookings")
STATUS = {"active": "active", "suspended": "paused"}   # anything else archives


def _counts(rows):
    """Five record types used by audit_cutover_readiness."""
    return {"members": len(rows),
            "active_memberships": sum(1 for r in rows if r.get("plan") and r.get("status") == "Active"),
            "class_packs": sum(1 for r in rows if (r.get("pack_balance") or 0) > 0),
            "future_bookings": sum(int(r.get("future_bookings") or 0) for r in rows),
            "payment_methods": sum(1 for r in rows if r.get("card"))}


def validate_rows(rows):
    issues, norm, kept, dropped, seen = [], [], [], [], {}
    for r in rows:
        mid, add = r.get("id"), lambda c, m: issues.append({"member": r.get("id"), "class": c, "message": m})
        if r.get("status") == "Inactive" and not r.get("plan"):
            dropped.append(r); add("auto", "Archived member with no plan, excluded"); continue
        d = re.sub(r"\D", "", r.get("phone") or "")
        d = d[1:] if len(d) == 11 and d[0] == "1" else d
        phone = "+1" + d if len(d) == 10 else ""
        if phone and phone != r.get("phone"): add("auto", "Phone normalized to +1 format")
        if not phone: add("warn", f"Phone has {len(d)} digits")
        end, m = r.get("plan_end") or "", re.fullmatch(r"(\d\d)/(\d\d)/(\d{4})", r.get("plan_end") or "")
        if m: end = f"{m[3]}-{m[1]}-{m[2]}"; add("auto", "Date converted to ISO")
        em = r.get("email")
        if not em: add("warn", "No email on file")
        elif em in seen: add("block", f"Same email as member {seen[em]}: merge or correct?")
        else: seen[em] = mid
        if r.get("plan") and r.get("status") == "Active" and not r.get("card"):
            add("block", "No card on file, so the member would re-swipe at the front desk")
        kept.append(r)
        norm.append({"member_id": mid, "name": f"{r.get('first')} {r.get('last')}".strip(), "email": em, "phone_e164": phone,
                     "status": STATUS.get((r.get("status") or "").lower(), "archived"), "plan": r.get("plan"), "plan_end": end,
                     "pack_balance": r.get("pack_balance", 0), "card_token": ("tok_" + r["card"][-4:]) if r.get("card") else None,
                     "future_bookings": r.get("future_bookings", 0)})
    blockers = [i for i in issues if i["class"] == "block"]
    return {"decision": "not_usable" if blockers else "usable", "issues": issues, "normalized": norm,
            "counts": {"source": len(rows), "excluded": len(dropped), "blockers": len(blockers),
                       "warnings": sum(i["class"] == "warn" for i in issues), "auto_fixes": sum(i["class"] == "auto" for i in issues) - len(dropped)},
            "audit_inputs": {"source": _counts(rows), "excluded": _counts(dropped), "migrated": _counts(kept)}}


def register(mcp):
    @mcp.tool()
    def validate_export(rows: list[dict], source_system: str = "mindbody") -> dict:
        """First-file check: normalize an export to the canonical layout, classify issues (auto / warn / block), and return
        audit_inputs shaped for audit_cutover_readiness (source, excluded, migrated counts for the five record types)."""
        if not rows: return {"error": "rows must not be empty"}
        out = validate_rows(rows); out["source_system"] = source_system
        return out

    @mcp.tool()
    def draft_correction_request(account: str, issues: list[dict]) -> dict:
        """Plain-language request back to the studio listing only the blocking issues, with a committed next contact."""
        bl = [i for i in issues if i.get("class") == "block"]
        if not bl:
            return {"subject": f"{account}: your file is ready", "body": f"Hi {account} team, your file is ready to load. I will email you by noon your time tomorrow with the load window."}
        items = "; ".join(f"member {i['member']} ({i['message'].lower()})" for i in bl)
        return {"subject": f"{account}: {len(bl)} step{'s' if len(bl) > 1 else ''} to load your file",
                "body": f"Hi {account} team, thanks for the export. It is close. {len(bl)} thing{'s' if len(bl) > 1 else ''} stop{'' if len(bl) > 1 else 's'} us loading it: {items}. Reply with the corrected rows or tell us how you would like them handled, and we will load it the same day."}
