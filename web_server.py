from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from database import Database
import os

app = FastAPI()
db = Database()

# Setup templates directory properly
base_path = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(base_path, "templates"))

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    try:
        tweets = db.get_latest_tweets(30)
    except Exception as e:
        print(f"Database Error: {e}")
        tweets = []
    
    return templates.TemplateResponse("index.html", {"request": request, "tweets": tweets})

# Serve PWA essential files from root
@app.get("/manifest.json")
async def get_manifest():
    return FileResponse(os.path.join(base_path, "manifest.json"))

@app.get("/sw.js")
async def get_sw():
    return FileResponse(os.path.join(base_path, "sw.js"))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
