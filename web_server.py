from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from database import Database
import os

app = FastAPI()
db = Database()

# Setup templates directory
base_path = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(base_path, "templates"))

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    try:
        # Get latest 30 tweets
        tweets = db.get_latest_tweets(30)
    except Exception as e:
        print(f"Web Error: {e}")
        tweets = [] # Return empty list if table is not ready
        
    return templates.TemplateResponse("index.html", {"request": request, "tweets": tweets})

# For PWA manifest
@app.get("/manifest.json")
async def get_manifest():
    return {
        "name": "Twitter Monitor App",
        "short_name": "TweetApp",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#1da1f2",
        "icons": [{"src": "https://cdn-icons-png.flaticon.com/512/733/733579.png", "sizes": "512x512", "type": "image/png"}]
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
