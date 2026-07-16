"""Local runner. Usage: python app.py  ->  http://localhost:8765"""
import uvicorn
from api.index import app  # noqa: F401

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765)
