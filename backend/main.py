from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import httpx
import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration path
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", str(Path(__file__).parent.parent / "config")))
CONFIG_FILE = CONFIG_DIR / "settings.json"

def load_settings():
    # 1. Try loading from config file
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config file: {e}")

    # 2. Fallback to environment variables
    return {
        "registry_url": os.getenv("REGISTRY_URL", "http://localhost:5000"),
        "registry_user": os.getenv("REGISTRY_USER"),
        "registry_password": os.getenv("REGISTRY_PASSWORD")
    }

def save_settings(settings: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(settings, f, indent=4)
        return True
    except Exception as e:
        print(f"Error saving config file: {e}")
        return False

def format_size(size_bytes):
    if not isinstance(size_bytes, (int, float)) or size_bytes < 0:
        return "Unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024

@app.get("/api/settings")
async def get_settings():
    settings = load_settings()
    return {
        "registry_url": settings.get("registry_url"),
        "registry_user": settings.get("registry_user"),
        "registry_password": settings.get("registry_password")
    }

@app.post("/api/settings")
async def update_settings(settings: dict):
    if save_settings(settings):
        return {"status": "success", "message": "Settings updated and persisted"}
    else:
        raise HTTPException(status_code=500, detail="Failed to save settings")

async def get_registry_client():
    settings = load_settings()
    auth = None
    if settings.get("registry_user") and settings.get("registry_password"):
        auth = (settings["registry_user"], settings["registry_password"])
    
    client = httpx.AsyncClient(
        base_url=settings.get("registry_url"),
        auth=auth,
        verify=False # In a real scenario, you'd handle SSL properly
    )
    try:
        yield client
    finally:
        await client.aclose()

@app.get("/api/catalog")
async def get_catalog(client: httpx.AsyncClient = Depends(get_registry_client)):
    try:
        response = await client.get("/v2/_catalog")
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tags/{name}")
async def get_tags(name: str, client: httpx.AsyncClient = Depends(get_registry_client)):
    try:
        response = await client.get(f"/v2/{name}/tags/list")
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

ACCEPT_HEADERS = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


async def _resolve_manifest(client, name, tag):
    manifest_response = await client.get(
        f"/v2/{name}/manifests/{tag}",
        headers={"Accept": ACCEPT_HEADERS}
    )
    manifest_response.raise_for_status()
    data = manifest_response.json()

    media_type = data.get("mediaType", "")

    if media_type == "application/vnd.oci.image.index.v1+json":
        manifests = data.get("manifests", [])
        target = None
        for m in manifests:
            p = m.get("platform", {})
            if p.get("architecture") == "amd64" and p.get("os") == "linux":
                target = m
                break
        if target is None:
            target = manifests[0]
        digest = target["digest"]
        platform = f"{target['platform']['os']}/{target['platform']['architecture']}"
        print(f"OCI index - resolving to {platform} ({digest})")
        resp2 = await client.get(
            f"/v2/{name}/manifests/{digest}",
            headers={"Accept": ACCEPT_HEADERS}
        )
        resp2.raise_for_status()
        return resp2.json(), resp2.headers
    elif media_type in ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"):
        return data, manifest_response.headers
    else:
        raise HTTPException(status_code=415, detail=f"Unsupported mediaType: {media_type}")


@app.get("/api/tag-details")
async def get_tag_details(name: str, tag: str, client: httpx.AsyncClient = Depends(get_registry_client)):
    try:
        manifest, _headers = await _resolve_manifest(client, name, tag)

        size_bytes = 0
        for layer in manifest.get("layers", []):
            layer_size = layer.get("size")
            if isinstance(layer_size, int):
                size_bytes += layer_size

        config = manifest.get("config", {})
        config_size = config.get("size")
        if isinstance(config_size, int):
            size_bytes += config_size

        created_at = None
        config_digest = config.get("digest")
        if config_digest:
            config_response = await client.get(f"/v2/{name}/blobs/{config_digest}")
            config_response.raise_for_status()
            config_blob = config_response.json()
            created_at = config_blob.get("created")
            if not created_at:
                history = config_blob.get("history", [])
                for item in history:
                    created_value = item.get("created")
                    if created_value:
                        created_at = created_value
                        break

        return {
            "name": name,
            "tag": tag,
            "created_at": created_at,
            "size_bytes": size_bytes if size_bytes > 0 else None,
            "size_human": format_size(size_bytes) if size_bytes > 0 else "Unknown",
        }
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/manifest-digest")
async def get_manifest_digest(name: str, tag: str, client: httpx.AsyncClient = Depends(get_registry_client)):
    try:
        manifest, headers = await _resolve_manifest(client, name, tag)
        digest = headers.get("docker-content-digest")
        if not digest:
            raise HTTPException(status_code=500, detail="Registry did not return manifest digest")
        return {"digest": digest}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/delete-tag")
async def delete_tag(request: Request, client: httpx.AsyncClient = Depends(get_registry_client)):
    data = await request.json()
    name = data.get("name")
    digest = data.get("digest")
    if not name or not digest:
        raise HTTPException(status_code=400, detail="name and digest are required")
    try:
        response = await client.delete(f"/v2/{name}/manifests/{digest}")
        if response.status_code == 202:
            return {"status": "success", "message": f"Tag deleted from {name}"}
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail="Manifest not found. It may have already been deleted.")
        else:
            raise HTTPException(status_code=response.status_code, detail=response.text)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Serve static files from the frontend directory
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
