import uvicorn

from testmart.config import load

if __name__ == "__main__":
    cfg = load()
    uvicorn.run("testmart.app:app", host=cfg.host, port=cfg.port)
