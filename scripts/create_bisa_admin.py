"""Provision a BISA administrator without placing credentials in source control."""

from __future__ import annotations

import getpass

from bisa_domain import DomainError, clean_text, connect, hash_secret, init_db, new_id, normalize_phone, now_iso


def main():
    init_db()
    phone = normalize_phone(input("Omani admin phone: ").strip())
    name = clean_text(input("Admin name: ").strip(), 80, True)
    role = input("Role [admin/super_admin] (default admin): ").strip() or "admin"
    if role not in {"admin", "super_admin"}:
        raise DomainError("invalid_admin_role")
    pin = getpass.getpass("New 4-8 digit PIN: ")
    if not pin.isdigit() or not 4 <= len(pin) <= 8:
        raise DomainError("valid_pin_required")
    confirm = getpass.getpass("Confirm PIN: ")
    if pin != confirm:
        raise DomainError("pin_confirmation_mismatch")
    with connect(immediate=True) as con:
        row = con.execute("SELECT id FROM accounts WHERE phone=?", (phone,)).fetchone()
        account_id = row["id"] if row else new_id("acct")
        if row:
            con.execute("UPDATE accounts SET name=?,pin_hash=?,status='active' WHERE id=?", (name, hash_secret(pin), account_id))
        else:
            con.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?)", (account_id, phone, name, hash_secret(pin), "active", now_iso()))
        con.execute("INSERT INTO account_roles(account_id,role,merchant_id,active) VALUES(?,?, '',1) ON CONFLICT(account_id,role,merchant_id) DO UPDATE SET active=1", (account_id, role))
    print(f"BISA {role} provisioned: {account_id}")


if __name__ == "__main__":
    main()
