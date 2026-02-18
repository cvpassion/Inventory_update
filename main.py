from dotenv import load_dotenv
load_dotenv()

import os, json
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import gspread
from google.oauth2.service_account import Credentials

from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth, OAuthError


app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
JSON_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
SHEET_NAME = os.getenv("SHEET_NAME", "Items")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret")
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "andrew.cmu.edu").lower()
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8001")
# --- sessions ---
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# --- oauth ---
oauth = OAuth()
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    client_kwargs={"scope": "openid email profile", "timeout": 10},
)



# -------------------------
# Helpers
# -------------------------

def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def get_ws():
    if not SPREADSHEET_ID:
        raise RuntimeError("Missing SPREADSHEET_ID (check .env)")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    if SERVICE_ACCOUNT_JSON:
        info = json.loads(SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        if not JSON_PATH:
            raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS (check .env)")
        creds = Credentials.from_service_account_file(JSON_PATH, scopes=scopes)

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(SHEET_NAME)

def find_row(ws, asset_id: str):
    asset_id = asset_id.strip()
    col = ws.col_values(1)[1:]  # skip header
    for i, v in enumerate(col, start=2):
        if v.strip() == asset_id:
            return i
    return None

def make_asset_id(primary_element, description_alloy, upper_size_um,
                  manufacturer, date_received, number_of_processes,
                  number_of_uses, condition_identifier,
                  print_date_recycling):
    parts = [
        primary_element, description_alloy, upper_size_um, manufacturer,
        date_received, number_of_processes, number_of_uses,
        condition_identifier, print_date_recycling
    ]
    cleaned = [" ".join(p.strip().split()).replace("_", "-") for p in parts]
    return "_".join(cleaned)

def require_user(request: Request):
    user = request.session.get("user")
    if not user:
        return None
    email = (user.get("email") or "").lower()
    if not email.endswith("@" + ALLOWED_DOMAIN):
        return None
    return user

def login_redirect():
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login")
async def login(request: Request):
    redirect_uri = f"{BASE_URL}/auth"
    return await oauth.google.authorize_redirect(
        request,
        redirect_uri,
        prompt="select_account"
    )



@app.get("/auth")
async def auth(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return HTMLResponse("Login failed", status_code=401)

    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").lower()

    domain = email.split("@")[-1]

    if domain != ALLOWED_DOMAIN:
        request.session.clear()
        return HTMLResponse("Not allowed (wrong email domain).", status_code=403)


    request.session["user"] = {"email": email, "name": userinfo.get("name", "")}
    return RedirectResponse(url="/", status_code=303)

def require_user(request: Request):
    user = request.session.get("user")
    if not user:
        return None

    email = (user.get("email") or "").lower()
    domain = email.split("@")[-1]

    if domain != ALLOWED_DOMAIN:
        return None

    return user


def login_redirect():
    return RedirectResponse(url="/login", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

# -------------------------
# Routes
# -------------------------

@app.get("/favicon.ico")
def favicon():
    return HTMLResponse(status_code=204)

@app.get("/", response_class=HTMLResponse)
def start(request: Request):
    user = require_user(request)
    return templates.TemplateResponse("start.html", {"request": request, "user": user})


@app.get("/new", response_class=HTMLResponse)
def new_entry(request: Request):
    if not require_user(request):
        return login_redirect()
    return templates.TemplateResponse("form.html", {"request": request, "mode": "new", "data": {}})



@app.get("/edit", response_class=HTMLResponse)
def edit_pick(request: Request):
    if not require_user(request):
        return login_redirect()
    ws = get_ws()
    asset_ids = ws.col_values(1)[1:]
    asset_ids = [a.strip() for a in asset_ids if a.strip()]
    return templates.TemplateResponse("edit_pick.html", {"request": request, "asset_ids": asset_ids})


@app.get("/u/", response_class=HTMLResponse)
def edit_form(request: Request, asset_id: str):
    if not require_user(request):
        return login_redirect()

    ws = get_ws()
    row = find_row(ws, asset_id)
    if row is None:
        return HTMLResponse("AssetID not found", status_code=404)

    vals = ws.row_values(row)
    vals += [""] * (12 - len(vals))

    data = {
        "primary_element": vals[1],
        "description_alloy": vals[2],
        "upper_size_um": vals[3],
        "manufacturer": vals[4],
        "date_received": vals[5],
        "number_of_processes": vals[6],
        "number_of_uses": vals[7],
        "condition_identifier": vals[8],
        "print_date_recycling": vals[9],
        "updated_by": vals[11],
    }

    return templates.TemplateResponse("form.html", {
        "request": request,
        "mode": "edit",
        "asset_id": asset_id.strip(),
        "data": data
    })

# -------------------------
# Create NEW entry
# -------------------------

@app.post("/u/new")
def submit_new(
    request: Request,
    primary_element: str = Form(""),
    description_alloy: str = Form(""),
    upper_size_um: str = Form(""),
    manufacturer: str = Form(""),
    date_received: str = Form(""),
    number_of_processes: str = Form(""),
    number_of_uses: str = Form(""),
    condition_identifier: str = Form(""),
    print_date_recycling: str = Form(""),
    updated_by: str = Form(""),
):
    if not require_user(request):
        return login_redirect()

    ws = get_ws()

    asset_id = make_asset_id(
        primary_element,
        description_alloy,
        upper_size_um,
        manufacturer,
        date_received,
        number_of_processes,
        number_of_uses,
        condition_identifier,
        print_date_recycling
    )

    if find_row(ws, asset_id):
        return HTMLResponse(f"AssetID already exists:<br><br>{asset_id}", status_code=400)


    ws.append_row([
        asset_id,
        primary_element,
        description_alloy,
        upper_size_um,
        manufacturer,
        date_received,
        number_of_processes,
        number_of_uses,
        condition_identifier,
        print_date_recycling,
        now_str(),
        updated_by,
    ])

    return RedirectResponse(url=f"/u/?asset_id={asset_id}&ok=1", status_code=303)


# -------------------------
# Update EXISTING entry
# -------------------------

@app.post("/u/{asset_id}")
def submit_update(
    request: Request,
    asset_id: str,
    primary_element: str = Form(""),
    description_alloy: str = Form(""),
    upper_size_um: str = Form(""),
    manufacturer: str = Form(""),
    date_received: str = Form(""),
    number_of_processes: str = Form(""),
    number_of_uses: str = Form(""),
    condition_identifier: str = Form(""),
    print_date_recycling: str = Form(""),
    updated_by: str = Form(""),
):
    if not require_user(request):
        return login_redirect()

    ws = get_ws()
    row = find_row(ws, asset_id)
    if row is None:
        return HTMLResponse("AssetID not found", status_code=404)

    current = ws.row_values(row)
    current += [""] * (12 - len(current))

    def pick(new, old):
        return new if new.strip() else old

    new_primary = pick(primary_element, current[1])
    new_desc = pick(description_alloy, current[2])
    new_upper = pick(upper_size_um, current[3])
    new_manu = pick(manufacturer, current[4])
    new_date = pick(date_received, current[5])
    new_proc = pick(number_of_processes, current[6])
    new_uses = pick(number_of_uses, current[7])
    new_cond = pick(condition_identifier, current[8])
    new_print = pick(print_date_recycling, current[9])

    new_asset_id = make_asset_id(
        new_primary, new_desc, new_upper, new_manu,
        new_date, new_proc, new_uses, new_cond, new_print
    )

    existing_row = find_row(ws, new_asset_id)
    if existing_row and existing_row != row:
        return HTMLResponse("Another entry already has this generated AssetID.", status_code=400)

    new_row_values = [
        new_asset_id,
        new_primary,
        new_desc,
        new_upper,
        new_manu,
        new_date,
        new_proc,
        new_uses,
        new_cond,
        new_print,
        now_str(),
        pick(updated_by, current[11]),
    ]

    # update cols 1-12
    for col_idx, value in enumerate(new_row_values, start=1):
        ws.update_cell(row, col_idx, value)

    return RedirectResponse(url=f"/u/?asset_id={new_asset_id}&ok=1", status_code=303)
