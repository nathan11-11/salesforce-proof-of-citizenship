import json
import os
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

DOWNLOAD_FOLDER = "Downloads"

# Optimized single SOQL query pulling directly from ContentVersion
SOQL_QUERY = """
SELECT 
    ContentDocumentId, 
    VersionData, 
    FileExtension,
    ContentDocument.Owner.FirstName, 
    ContentDocument.Owner.LastName, 
    ContentDocument.Owner.Contact.MIDS_Alpha__c 
FROM ContentVersion 
WHERE IsLatest = TRUE 
  AND ContentDocument.Owner.Contact.RecordType.Name = 'Midshipmen' 
  AND ContentDocument.Owner.Contact.AIS_Class_Year__c = '2030' 
  AND ContentDocument.Owner.Contact.MIDS_status_code__c = '00'
  AND ContentDocumentId IN (
      SELECT ContentDocumentId 
      FROM ContentDocumentLink 
      WHERE LinkedEntity.Name = 'Proof of Citizenship Instructions'
  )
"""

def xml_escape(v):
    return str(v).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;")

# Load configuration data
with open("config.json") as f:
    cfg = json.load(f)

api = cfg.get("api_version","63.0").lstrip("v")
login = cfg["login_url"].rstrip("/")
pw = cfg["password"] + cfg["security_token"]

# Build SOAP login payload
body = f"""<?xml version="1.0"?>
<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">
<env:Body>
<n1:login xmlns:n1="urn:partner.soap.sforce.com">
<n1:username>{xml_escape(cfg["username"])}</n1:username>
<n1:password>{xml_escape(pw)}</n1:password>
</n1:login>
</env:Body>
</env:Envelope>"""

print("Connecting...")
r = requests.post(f"{login}/services/Soap/u/{api}", data=body.encode(), headers={"Content-Type":"text/xml; charset=UTF-8","SOAPAction":"login"})
r.raise_for_status()

# Parse authentication details
root = ET.fromstring(r.text)
ns = {"sf":"urn:partner.soap.sforce.com"}
sid = root.findtext(".//sf:sessionId", namespaces=ns)
server = root.findtext(".//sf:serverUrl", namespaces=ns)
instance = server.split("/services/Soap/")[0]

# Setup REST session
session = requests.Session()
session.headers["Authorization"] = "Bearer " + sid
print("Connected:", instance)

# Ensure download folder exists before running the loop
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Fetch the combined query results
url = f"{instance}/services/data/v{api}/query/?q={quote(SOQL_QUERY,safe='')}"
query_response = session.get(url).json()
records = query_response.get("records", [])

print(f"Found {len(records)} total file records to process.")

# Dictionary to keep track of document counts per alpha ID
# Example structure: {"M123456": 1, "M789012": 2}
alpha_document_counts = {}

# Loop through every ContentVersion record returned by the query
for v in records:
    content_doc = v.get("ContentDocument") or {}
    owner = content_doc.get("Owner") or {}
    
    alpha = (owner.get("Contact") or {}).get("MIDS_Alpha__c", "Unknown")
    first_name = owner.get('FirstName', 'Unknown')
    last_name = owner.get('LastName', 'Unknown')

    # Increment the document counter for this specific Alpha ID
    # If the Alpha isn't in the dictionary yet, it defaults to 0 and becomes 1
    alpha_document_counts[alpha] = alpha_document_counts.get(alpha, 0) + 1
    doc_number = alpha_document_counts[alpha]

    print(f"Processing document #{doc_number} for {first_name} {last_name} ({alpha})...")

    ext = v.get("FileExtension", "bin")
    dl_url = instance + v["VersionData"]
    
    # Generate unique file name incorporating the sequential document number
    name = f"{alpha} - {first_name} {last_name} - ID Document{doc_number}.{ext}"
    path = os.path.join(DOWNLOAD_FOLDER, name)
    
    # Download and save the file payload
    resp = session.get(dl_url)
    resp.raise_for_status()
    with open(path, "wb") as f:
        f.write(resp.content)
    print("Saved", path)

print("All downloads complete.")