import sys, io, re, hashlib, json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

KEY = r"C:\Users\user\OneDrive\Desktop\House of Edtech\attendance-suite\fresh-delight-500710-g6-215a58dfc236.json"
DRIVE_ID = "0ADZkkxHLwZa9Uk9PVA"
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-08-19"

creds = service_account.Credentials.from_service_account_file(KEY, scopes=["https://www.googleapis.com/auth/drive.readonly"])
svc = build("drive", "v3", credentials=creds, cache_discovery=False)

def list_all(q, fields="files(id,name,mimeType,size,parents)"):
    out, tok = [], None
    while True:
        r = svc.files().list(q=q, corpora="drive", driveId=DRIVE_ID, includeItemsFromAllDrives=True,
                             supportsAllDrives=True, pageSize=1000, pageToken=tok, fields="nextPageToken,"+fields).execute()
        out += r.get("files", []); tok = r.get("nextPageToken")
        if not tok: return out

def download(fid):
    buf = io.BytesIO(); dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=fid, supportsAllDrives=True))
    done = False
    while not done: _, done = dl.next_chunk()
    return buf.getvalue()

# folders whose name starts with the date (handles "-" and en-dash variants)
folders = list_all(f"mimeType='application/vnd.google-apps.folder' and name contains '{DATE}' and trashed=false")
folders = [f for f in folders if f["name"].startswith(DATE)]
folders.sort(key=lambda f: f["name"])
print(f"Found {len(folders)} folders for {DATE}\n", file=sys.stderr)

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
results = {}  # session name -> dict
seen_hash = set()
for f in folders:
    name = re.sub(r"^%s\s*[-–]\s*" % re.escape(DATE), "", f["name"]).strip()
    kids = list_all(f"'{f['id']}' in parents and trashed=false")
    links_files = [k for k in kids if k["name"].lower().startswith("session_links")]
    chat_files  = [k for k in kids if "chat" in k["name"].lower() and k["name"].lower().endswith(".txt")]
    entry = results.setdefault(name, {"folders": [], "source": None, "links": [], "raw": ""})
    entry["folders"].append(f["name"])
    if entry["links"]: continue  # already got links from a duplicate folder
    if links_files:
        data = download(links_files[0]["id"]).decode("utf-8", "replace")
        h = hashlib.md5(data.encode()).hexdigest()
        entry["source"] = "Session_Links.txt"; entry["raw"] = data
        entry["links"] = list(dict.fromkeys(URL_RE.findall(data)))
    elif chat_files:
        data = download(chat_files[0]["id"]).decode("utf-8", "replace")
        entry["source"] = f"chat ({chat_files[0]['name']})"; entry["raw"] = data
        entry["links"] = list(dict.fromkeys(URL_RE.findall(data)))
    else:
        entry["source"] = "NONE (no Session_Links.txt, no chat)"
        entry["files"] = [k["name"] for k in kids]

print(json.dumps(results, indent=1, ensure_ascii=False))
