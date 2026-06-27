"""
make_secrets.py — build .streamlit/secrets.toml from your downloaded service-account
JSON key, so you never have to copy/paste any key text by hand.

Usage (from the project folder):
    python make_secrets.py "C:\\path\\to\\your-key.json"

The Drive IDs below are pre-filled for this project. The private_key is written
using a TOML triple-quoted string (the safe form). This script prints only
NON-secret confirmation — it never displays the private key.
"""
import json
import os
import sys

# ── Drive items (pre-filled) ─────────────────────────────────────────────────
ROSTER_ID = "1CzASgM5yrbASE5vIxSrKBB-yQSRtQxh_iCVS9GTpfgg"
L2_ID = "1nlQPkg1l_cNHMtN2HKvqinmU_n0bLsC9RkkMfeqvPlM"
ATTENDEE_FOLDER_ID = "0ADZkkxHLwZa9Uk9PVA"   # Shared Drive / folder of Zoom exports
ATTENDEE_ZIP_ID = ""                          # leave "" when using a folder


def _basic(s: str) -> str:
    """A TOML basic (double-quoted) string with the few needed escapes."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python make_secrets.py "PATH\\to\\your-key.json"')
        sys.exit(1)

    key_path = sys.argv[1]
    if not os.path.isfile(key_path):
        print(f"ERROR: Key file not found: {key_path}")
        sys.exit(1)

    with open(key_path, "r", encoding="utf-8") as f:
        k = json.load(f)

    if "private_key" not in k or "client_email" not in k:
        print("ERROR: That file doesn't look like a service-account key "
              "(missing private_key / client_email).")
        sys.exit(1)

    lines = ["[gcp_service_account]"]
    for field in ("type", "project_id", "private_key_id"):
        lines.append(f"{field} = {_basic(k.get(field, ''))}")
    # triple-quoted: the JSON's private_key already has real newlines after json.load,
    # which is exactly what google-auth expects.
    lines.append('private_key = """' + k["private_key"] + '"""')
    for field in ("client_email", "client_id", "auth_uri", "token_uri",
                  "auth_provider_x509_cert_url", "client_x509_cert_url", "universe_domain"):
        if k.get(field):
            lines.append(f"{field} = {_basic(k[field])}")

    lines += ["", "[drive]",
              f"roster_id = {_basic(ROSTER_ID)}",
              f"l2_id = {_basic(L2_ID)}"]
    if ATTENDEE_ZIP_ID:
        lines.append(f"attendee_zip_id = {_basic(ATTENDEE_ZIP_ID)}")
    if ATTENDEE_FOLDER_ID:
        lines.append(f"attendee_folder_id = {_basic(ATTENDEE_FOLDER_ID)}")

    os.makedirs(".streamlit", exist_ok=True)
    with open(os.path.join(".streamlit", "secrets.toml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("OK - wrote .streamlit/secrets.toml")
    print("   robot email :", k.get("client_email"))
    print("   roster_id   :", ROSTER_ID)
    print("   l2_id       :", L2_ID)
    print("   attendee    :", ATTENDEE_FOLDER_ID or ATTENDEE_ZIP_ID)
    print("   (private_key written to the file but intentionally NOT shown here)")


if __name__ == "__main__":
    main()
