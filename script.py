import base64
import json
import os
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

DOWNLOAD_FOLDER = "Downloads"
SOURCE_NAME = "US Naval Acad. Midshipman"

# Optimized single SOQL query pulling directly from ContentVersion
SOQL_QUERY = """
SELECT 
    ContentDocumentId, 
    VersionData, 
    FileExtension,
    ContentDocument.Owner.FirstName, 
    ContentDocument.Owner.LastName, 
    ContentDocument.Owner.Contact.MIDS_Alpha__c,
    Owner.Contact.hed__Social_Security_Number__c,
    Owner.Contact.Formatted_Birthdate__c,
    Owner.MiddleName
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
  LIMIT 10
"""

def xml_escape(v):
    return str(v).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;")


def format_birth_date(value):
    """Return a date in the IDSWS-required YYYYMMDD format when possible."""
    if not value:
        return None

    digits = "".join(char for char in str(value) if char.isdigit())
    if len(digits) != 8:
        return str(value)

    # Salesforce date fields are normally returned as YYYY-MM-DD. Support the
    # formatted MM/DD/YYYY value selected by the query as well.
    if str(value).lstrip().startswith(("19", "20")):
        return digits
    return digits[4:] + digits[:4]


def encode_pdf_document(file_extension, content):
    """Return IDSWS's Base64 document value, or null for a non-PDF file."""
    if str(file_extension).lower() != "pdf":
        return None
    return base64.b64encode(content).decode("ascii")


def soap_fault_message(response):
    """Extract a readable Salesforce SOAP fault without exposing credentials."""
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        return response.reason

    for element in root.iter():
        tag_name = element.tag.rsplit("}", 1)[-1]
        if tag_name == "faultstring" and element.text:
            return element.text.strip()
    return response.reason


def build_document_payload(content_owner, version_owner, document):
    """Build the IDSWS sidecar metadata for one downloaded document."""
    version_contact = version_owner.get("Contact") or {}

    return {
        "source_nm": SOURCE_NAME,
        "pn_id": version_contact.get("hed__Social_Security_Number__c"),
        "pn_id_typ_cd": "S",
        "dod_edi_pn_id": None,
        "pn_lst_nm": content_owner.get("LastName"),
        "pn_frst_nm": content_owner.get("FirstName"),
        "pn_mid_nm": version_owner.get("MiddleName"),
        "pn_cdncy_nm": None,
        "pn_brth_dt": format_birth_date(
            version_contact.get("Formatted_Birthdate__c")
        ),
        "doc_typ_cd": None,
        "pn_doc_id": None,
        "pn_doc_iss_dt": None,
        "pn_doc_exp_dt": None,
        "pn_doc_st_cd": None,
        "pn_doc_ctry_cd": None,
        "pn_doc_cnty_nm": None,
        "document": document,
        "pn_doc_prstn_dt": None,
    }

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
if not r.ok:
    raise RuntimeError(
        f"Salesforce login failed (HTTP {r.status_code}): {soap_fault_message(r)}"
    )

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
    content_owner = content_doc.get("Owner") or {}
    version_owner = v.get("Owner") or content_owner
    
    alpha = (content_owner.get("Contact") or {}).get("MIDS_Alpha__c", "Unknown")
    first_name = content_owner.get('FirstName', 'Unknown')
    last_name = content_owner.get('LastName', 'Unknown')

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

    # IDSWS expects PDF document images as Base64. Preserve a null value for
    # non-PDF downloads because their contents cannot be submitted as a PDF.
    document = encode_pdf_document(ext, resp.content)

    # Write metadata next to the corresponding document, retaining its base
    # name and changing only the extension to .json.
    json_path = os.path.splitext(path)[0] + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            build_document_payload(content_owner, version_owner, document),
            f,
            indent=2,
        )
        f.write("\n")
    print("Saved", json_path)

print("All downloads complete.")
