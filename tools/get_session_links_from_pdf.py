import sys, io, re
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
KEY = r"C:\Users\user\OneDrive\Desktop\House of Edtech\attendance-suite\fresh-delight-500710-g6-215a58dfc236.json"
DRIVE_ID = "0ADZkkxHLwZa9Uk9PVA"
creds = service_account.Credentials.from_service_account_file(KEY, scopes=["https://www.googleapis.com/auth/drive.readonly"])
svc = build("drive", "v3", credentials=creds, cache_discovery=False)
def ls(q):
    return svc.files().list(q=q, corpora="drive", driveId=DRIVE_ID, includeItemsFromAllDrives=True, supportsAllDrives=True, pageSize=1000, fields="files(id,name,mimeType)").execute()["files"]
def dl(fid):
    b=io.BytesIO(); d=MediaIoBaseDownload(b, svc.files().get_media(fileId=fid, supportsAllDrives=True)); done=False
    while not done: _,done=d.next_chunk()
    return b.getvalue()
URL=re.compile(r"https?://[^\s<>\"')\]]+")
try:
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader
for fname in ["Film making Links.pdf", "AI_Image_Video_Tools_Reference (4).pdf"]:
    hits=[f for f in ls(f"name='{fname}' and trashed=false")]
    for f in hits:
        parents = svc.files().get(fileId=f["id"], supportsAllDrives=True, fields="parents").execute().get("parents",[])
        pname = svc.files().get(fileId=parents[0], supportsAllDrives=True, fields="name").execute()["name"] if parents else "?"
        if "2026-09-19" not in pname: continue
        data=dl(f["id"]); r=PdfReader(io.BytesIO(data)); links=[]
        for p in r.pages:
            links += URL.findall(p.extract_text() or "")
            for a in (p.get("/Annots") or []):
                try:
                    u=a.get_object().get("/A",{}).get("/URI")
                    if u: links.append(str(u))
                except Exception: pass
        print("###", pname, "|", fname)
        for l in dict.fromkeys(links): print("   ", l)
