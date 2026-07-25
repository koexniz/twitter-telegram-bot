from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from database import Database
import uvicorn
import os

app = FastAPI()
db = Database()

# Setup for PWA files
base_path = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(base_path, "templates"))

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    tweets = db.get_latest_tweets(30)
    return templates.TemplateResponse("index.html", {"request": request, "tweets": tweets})

@app.get("/api/tweets")
async def get_tweets():
    return db.get_latest_tweets(50)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
